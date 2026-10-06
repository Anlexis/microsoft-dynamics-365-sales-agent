"""AgentCore Platform v1.0 - inner workflow Step 3: InferDynamicsFields.

Resolves the target Dataverse entity (lead / opportunity / contact), the record
identifier, the display name, and the attribute fields, then assembles a
validated Dynamics 365 Web API (Dataverse OData v4) request for the classified
operation.

Attribute fields arrive from two channels and are merged with the structured
channel winning: "Key: value" lines parsed out of the (redacted) request text,
and the caller-data contract's `attributes` mapping (already bounds-checked -
inert field names, capped length, finite in-range numbers). Every numeric
attribute is re-parsed here through the same finite+bounded parser, so a value
that crossed the graph boundary out of contract fails closed instead of
reaching a CRM write.

The record identifier is taken only from an explicit identifier in the text or
the caller-supplied hint - an unresolved identifier is left empty rather than
invented (never touch the wrong CRM record; the executor surfaces the miss as
status=error). Only known attribute keys are mapped to Dataverse logical names;
unknown pairs are folded into `description`, because a live Dataverse call 400s
on unknown attribute names. Deterministic - no LLM (see docs/02_design.md).
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json
from src.services.security import finite_in_range, looks_numeric

# A Dataverse record GUID (8-4-4-4-12 hex) - unambiguous, so an explicit GUID
# anywhere in the text / hint / "Id:" field is accepted; anything else is not.
_GUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# Quoted display name: named "Foo" / called "Foo". Curly quotes as \u escapes so
# the source stays pure ASCII (push-safe).
_NAME_QUOTED_RE = re.compile(r'(?:named|called|for)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# "Key: value" attribute lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as \u escapes (push-safe).
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Keys that are the GUID/name themselves, not attribute fields.
_ID_KEYS = ("record id", "id", "guid", "record guid")
_NAME_KEYS = ("name", "full name", "record name", "topic", "subject")
# Redaction placeholder (see ValidateInputNode) - never mapped to an attribute.
_REDACTION = "[REDACTED]"

# Entity detection keywords (checked in priority order; lead is the documented
# default when no entity word appears - docs/02 Design Decision Record).
_ENTITY_KEYWORDS = (
    ("opportunity", ("opportunity", "opportunities", "deal", "pipeline", "商談", "案件")),
    ("contact", ("contact", "contacts", "person record", "連絡先", "担当者")),
    ("lead", ("lead", "leads", "prospect", "inquiry", "リード", "見込み")),
)

# Dataverse entity sets + attribute logical-name maps (module constants -
# read-only lookup tables, never mutated at execute() time).
_ENTITY_SETS = {"lead": "leads", "opportunity": "opportunities", "contact": "contacts"}
_ATTR_MAPS = {
    "lead": {
        "topic": "subject",
        "subject": "subject",
        "first name": "firstname",
        "last name": "lastname",
        "company": "companyname",
        "company name": "companyname",
        "email": "emailaddress1",
        "e-mail": "emailaddress1",
        "phone": "telephone1",
        "telephone": "telephone1",
        "job title": "jobtitle",
        "website": "websiteurl",
        "description": "description",
    },
    "opportunity": {
        "name": "name",
        "topic": "name",
        "estimated value": "estimatedvalue",
        "value": "estimatedvalue",
        "amount": "estimatedvalue",
        "close date": "estimatedclosedate",
        "estimated close date": "estimatedclosedate",
        "customer need": "customerneed",
        "description": "description",
    },
    "contact": {
        "first name": "firstname",
        "last name": "lastname",
        "email": "emailaddress1",
        "e-mail": "emailaddress1",
        "phone": "telephone1",
        "telephone": "telephone1",
        "mobile": "mobilephone",
        "job title": "jobtitle",
        "title": "jobtitle",
        "description": "description",
    },
}
# Attributes that must carry a non-negative number (Dataverse money/decimal).
_NUMERIC_ATTRS = ("estimatedvalue",)
# Sanity ceiling on a Dataverse money/decimal attribute; matches the bound the
# caller-data contract applies at the entry point.
_MAX_ATTRIBUTE_NUMBER = 1e12


def _format_number(value: Any) -> str:
    """Render a validated caller number without a spurious trailing ".0"."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class InferDynamicsFieldsNode(FunctionNode):
    """Resolve entities and assemble the Dynamics 365 Web API request."""

    # Inner domain node - derives fields from already-validated text; the
    # external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_record") or "lookup_record"
        record_hint = state.get("record_hint", "") or ""
        caller_entity = state.get("caller_entity_type", "") or ""
        caller_attributes = from_json(state.get("caller_attributes"), {}) or {}

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferDynamicsFieldsNode: missing validated_input"],
            }
        if not isinstance(caller_attributes, dict):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferDynamicsFieldsNode: attribute mapping is out of contract"],
            }

        # An explicitly stated entity wins over keyword detection.
        if caller_entity in _ENTITY_SETS:
            entity_type, entity_defaulted = caller_entity, False
        else:
            entity_type, entity_defaulted = self._resolve_entity(text)

        fields = self._parse_fields(text)
        # Structured caller fields are appended last so they win over a
        # "Key: value" line parsed out of the free text.
        fields.extend(
            (str(key), value if isinstance(value, str) else _format_number(value))
            for key, value in caller_attributes.items()
        )
        record_id = self._resolve_guid(text, record_hint, fields)
        record_name = self._resolve_name(text, fields)

        entity_set = _ENTITY_SETS[entity_type]
        if intent in ("create_record", "update_record"):
            data = self._build_attributes(entity_type, record_name, fields)
            if data is None:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        "InferDynamicsFieldsNode: attribute mapping carries a " "non-finite or out-of-range number"
                    ],
                }
            payload = {"entity_set": entity_set, "record_id": record_id, "data": data}
        else:  # lookup_record (read-only default)
            payload = {"entity_set": entity_set, "record_id": record_id}

        # Audit the assembled request shape - field signals only, not content.
        emit_trace_event(
            "infer_dynamics_fields_complete",
            {
                "intent": intent,
                "entity_type": entity_type,
                "has_record_id": bool(record_id),
                "n_fields": len(fields),
            },
            state,
        )

        result = {
            "entity_type": entity_type,
            "record_id": record_id,
            "record_name": record_name,
            "dynamics_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }
        if entity_defaulted:
            result["error_log"] = [
                "InferDynamicsFieldsNode: no entity word in request, " "defaulted to lead (documented fallback)"
            ]  # non-fatal note; status stays SUCCESS
        return result

    # -- extraction -----------------------------------------------------------

    def _resolve_entity(self, text: str) -> "tuple[str, bool]":
        """Keyword entity resolution; (entity_type, defaulted) - default lead."""
        low = text.lower()
        for entity, words in _ENTITY_KEYWORDS:
            if any(w in low for w in words):
                return entity, False
        return "lead", True

    def _resolve_guid(self, text: str, record_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit GUID only: text mention > GUID-shaped hint > 'Id:' field. Never invented."""
        m = _GUID_RE.search(text)
        if m:
            return m.group(0).lower()
        hint = record_hint.strip()
        if hint and _GUID_RE.fullmatch(hint):
            return hint.lower()
        for key, value in fields:
            if key.strip().lower() in _ID_KEYS and _GUID_RE.fullmatch(value.strip()):
                return value.strip().lower()
        return ""  # unresolved - left empty, never invented

    def _resolve_name(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _NAME_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:100]
        for key, value in fields:
            if key.strip().lower() in _NAME_KEYS and value.strip() != _REDACTION:
                return value.strip()[:100]
        return ""

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] attribute fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields

    # -- attribute assembly (Dataverse OData v4 attribute body) ----------------

    def _build_attributes(
        self, entity_type: str, record_name: str, fields: "list[tuple[str, str]]"
    ) -> "dict[str, Any] | None":
        """Assemble the Dataverse attribute body, or None when a number is out of contract."""
        attr_map = _ATTR_MAPS[entity_type]
        attrs: dict[str, Any] = {}
        extras: list[str] = []
        for key, value in fields:
            key_low = key.strip().lower()
            if key_low in _ID_KEYS:
                continue  # GUID handled by _resolve_guid (URL part, not body)
            if value == _REDACTION:
                continue  # redacted value - never written to the CRM
            logical = attr_map.get(key_low)
            if logical is None:
                extras.append(f"{key.strip()}: {value}")
                continue
            if logical in _NUMERIC_ATTRS:
                number = self._parse_amount(value)
                if number is None:
                    # A value that reads as a number but is not finite or is out
                    # of range fails the request closed; text that is simply not
                    # a number at all is preserved as description prose.
                    if looks_numeric(value):
                        return None
                    extras.append(f"{key.strip()}: {value}")
                    continue
                if number < 0:
                    extras.append(f"{key.strip()}: {value}")  # negative -> description
                    continue
                attrs[logical] = number
            else:
                attrs[logical] = value
        # Primary label fallback from the resolved display name.
        if record_name:
            if entity_type == "lead":
                attrs.setdefault("subject", record_name)
            elif entity_type == "opportunity":
                attrs.setdefault("name", record_name)
            elif "firstname" not in attrs and "lastname" not in attrs:  # contact
                first, _, last = record_name.rpartition(" ")
                if first:
                    attrs["firstname"] = first
                attrs["lastname"] = last or record_name
        # Unknown pairs are preserved for the CRM user, never sent as attributes.
        if extras:
            existing = attrs.get("description", "")
            joined = "\n".join(extras)
            attrs["description"] = f"{existing}\n{joined}".strip() if existing else joined
        return attrs

    def _parse_amount(self, value: str) -> "float | None":
        """Finite + bounded numeric parse for money attributes.

        Currency symbols and digit grouping are tolerated; NaN, +/-Infinity and
        magnitudes beyond the Dataverse money ceiling return None so the caller
        fails closed rather than writing a value no comparison can order.
        """
        cleaned = re.sub(r"[^0-9eE.+\-]", "", value)
        if not cleaned:
            return None
        return finite_in_range(cleaned, -_MAX_ATTRIBUTE_NUMBER, _MAX_ATTRIBUTE_NUMBER)
