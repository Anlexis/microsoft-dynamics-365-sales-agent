"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Unpacks the request envelope the outer backbone produced, re-checks the bounds
the caller-data contract promises (defence in depth - the envelope crosses a
graph boundary and a re-validated field cannot arrive out of contract), and runs
a deterministic (regex, NOT LLM) scan of every caller-supplied string for email
addresses and access-token-like values, which are flagged and redacted before
anything is logged.

The redaction covers BOTH caller channels: the free-text request AND the
structured attribute values. The platform's own masking applies to the request
fields it scans; a caller-supplied structured value must not slip past a
sanitizer just because it arrived through the other channel.

A CRM write request legitimately names a person or company, so the email/token
scan is flag-and-redact for safe logging rather than a hard reject. The
deterministic auto-rejects are the empty / non-request guard and an out-of-
contract structured field.
"""

import json
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED

from src.nodes.pre_process_node import (
    MAX_ATTRIBUTES,
    VALID_ENTITY_TYPES,
    VALID_INTENTS,
)
from src.schemas.state import to_json
from src.services.security import finite_in_range, is_record_id

# Deterministic patterns: email addresses and bearer/JWT/API-token-like strings
# that might appear in a pasted request. Flagged + redacted before logging.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,})\b")
_REDACTION = "[REDACTED]"

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3

# Same ceiling the caller contract applies to a numeric attribute value.
_MAX_ATTRIBUTE_NUMBER = 1e12


def _redact(text: str) -> "tuple[str, list[str]]":
    """Redact email/token-shaped substrings; return (redacted, flags)."""
    flags: list[str] = []
    redacted = text
    if _EMAIL_RE.search(redacted):
        flags.append("email")
        redacted = _EMAIL_RE.sub(_REDACTION, redacted)
    if _TOKEN_RE.search(redacted):
        flags.append("token")
        redacted = _TOKEN_RE.sub(_REDACTION, redacted)
    return redacted, flags


class ValidateInputNode(FunctionNode):
    """Unpack, re-validate and flag-and-redact the inbound CRM write request."""

    # Inner domain node - the external gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON envelope; a bare
        # string is also accepted so the node can be exercised directly.
        text: Any = raw
        record_hint = state.get("record_hint", "")
        entity_type = ""
        intent = ""
        attributes: dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                envelope = json.loads(raw)
            except (ValueError, TypeError):
                envelope = None
            if isinstance(envelope, dict):
                text = envelope.get("text", "")
                record_hint = envelope.get("record_hint", record_hint)
                entity_type = envelope.get("entity_type", "") or ""
                intent = envelope.get("intent", "") or ""
                raw_attributes = envelope.get("attributes")
                if isinstance(raw_attributes, dict):
                    attributes = raw_attributes

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return self._reject("empty or non-request input")

        # Re-check the contract across the graph boundary: an out-of-contract
        # value fails closed here rather than reaching the CRM request builder.
        if record_hint and not is_record_id(record_hint):
            return self._reject("record identifier is out of contract")
        if entity_type and entity_type not in VALID_ENTITY_TYPES:
            return self._reject("entity override is out of contract")
        if intent and intent not in VALID_INTENTS:
            return self._reject("operation override is out of contract")
        if len(attributes) > MAX_ATTRIBUTES:
            return self._reject("attribute mapping is out of contract")

        # Deterministic flag-and-redact across BOTH caller channels, before any
        # logging. Local lists per invocation - never a module-global.
        redacted, flags = _redact(text)

        clean_attributes: dict[str, Any] = {}
        for key, value in attributes.items():
            if isinstance(value, bool):
                return self._reject("attribute mapping is out of contract")
            if isinstance(value, (int, float)):
                number = finite_in_range(value, -_MAX_ATTRIBUTE_NUMBER, _MAX_ATTRIBUTE_NUMBER)
                if number is None:
                    return self._reject("attribute mapping carries a non-finite value")
                clean_attributes[key] = number
                continue
            if not isinstance(value, str):
                return self._reject("attribute mapping is out of contract")
            clean_value, value_flags = _redact(value)
            clean_attributes[key] = clean_value
            for flag in value_flags:
                if flag not in flags:
                    flags.append(flag)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {
                "has_record_hint": bool(record_hint),
                "redaction_flags": flags,
                "n_attributes": len(clean_attributes),
            },
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "record_hint": record_hint,
            "caller_entity_type": entity_type,
            "caller_intent": intent,
            "caller_attributes": to_json(clean_attributes),
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _reject(reason: str) -> dict[str, Any]:
        """Refuse the request: name the problem, never echo the rejected value.

        Every call site here is a value the caller can correct, so the run
        COMPLETES carrying the reason rather than terminating: the caller
        receives it and can resend on the same conversation.
        """
        code = "EMPTY_INPUT" if "empty" in reason else "INVALID_REQUEST"
        emit_progress(EMPTY_INPUT if code == "EMPTY_INPUT" else INPUT_REJECTED)
        return {
            "status": AgentStatus.SUCCESS.value,
            "error_code": code,
            "error_log": [f"ValidateInputNode: {reason}"],
        }
