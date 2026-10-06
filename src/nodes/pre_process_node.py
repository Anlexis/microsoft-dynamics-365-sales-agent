"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone. This node owns the caller-data contract: it accepts the
free-text request plus the structured invocation parameters, validates every
caller-supplied field against explicit bounds, and serializes the accepted
values into a single JSON string in `validated_input`, which the GraphNode
(`main` slot) hands to the inner Dynamics 365 workflow graph. (The subgraph
invoke carries only that string, so the envelope IS the hand-off - and because
it travels in a scanned field, the platform's PII mask covers the structured
caller fields exactly as it covers the free text.)

Caller-data contract (`input_context`, every field optional):

    record_id | record_hint | record_guid   Dataverse record identifier (GUID
                                            form). The record a lookup/update
                                            targets; never inferred loosely.
    entity_type                             lead | opportunity | contact -
                                            overrides keyword entity detection.
    intent                                  lookup_record | create_record |
                                            update_record - overrides the
                                            keyword classifier.
    attributes                              mapping of Dataverse attribute
                                            fields (<= 20 entries) whose values
                                            are merged into the request body.
    channel                                 inert caller label, audit only.

Validation is FAIL-CLOSED: a field that is present but does not satisfy its
stated bound returns status=error naming the FIELD (never echoing the rejected
value), rather than being dropped and silently degrading. An absent field is
fine - the pipeline then works from the free text alone.

An instruction-override payload is refused here, before any classification or
CRM call. The platform input gate refuses these too, but the template must not
depend on that: where the gate is absent or configured off, an unchecked
payload would reach the CRM path and return success. The refusal is asserted as
behaviour - error status, nothing carried forward - never as a gate's wording.
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED

from src.services.security import (
    finite_in_range,
    is_inert_identifier,
    is_instruction_override,
    is_record_id,
    looks_numeric,
    sanitize_query,
    ATTRIBUTE_KEY_RE,
)

# The complete set of accepted structured fields. An unrecognised key is
# refused rather than ignored: silently dropping it would let a caller believe
# a constraint was applied when it never was.
_RECORD_ID_KEYS = ("record_id", "record_hint", "record_guid")
_CALLER_KEYS = _RECORD_ID_KEYS + ("entity_type", "intent", "attributes", "channel")

# Fields the hosting runtime puts on the context channel itself. They are not
# part of the caller contract and this agent reads none of them, but refusing
# them would refuse every invocation served that way — the caller cannot remove
# what it never added. They are accepted and ignored, which is sound precisely
# because no constraint is attached to them: there is nothing for the caller to
# be misled about.
_RUNTIME_KEYS = ("conversation_history",)

_ACCEPTED_KEYS = frozenset(_CALLER_KEYS + _RUNTIME_KEYS)

VALID_ENTITY_TYPES = ("lead", "opportunity", "contact")
VALID_INTENTS = ("lookup_record", "create_record", "update_record")

# Structural caps.
MAX_CONTEXT_KEYS = 8
MAX_ATTRIBUTES = 20
MAX_ATTRIBUTE_CHARS = 200
# Dataverse money attributes are stored as decimals; this bound is a sanity
# ceiling on a caller-supplied amount, not a business limit.
MAX_ATTRIBUTE_NUMBER = 1e12


class PreProcessNode(FunctionNode):
    """Validate the caller-data contract and shape it for the inner workflow graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Dynamics 365 call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner API
    # node. An under-trusted (ANONYMOUS) caller is denied at this gate before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only [C1]

        if not isinstance(user_input, str) or not user_input.strip():
            return self._reject("user_input is empty or missing")

        # Strip markup and control characters, then cap length.
        sanitized_input = sanitize_query(user_input.strip())

        if is_instruction_override(sanitized_input):
            return self._reject("request refused - instruction-override content")

        if not isinstance(input_context, dict):
            return self._reject("input_context must be a mapping")
        if len(input_context) > MAX_CONTEXT_KEYS:
            return self._reject(f"input_context carries more than {MAX_CONTEXT_KEYS} fields")

        unknown = sorted(set(input_context) - _ACCEPTED_KEYS)
        if unknown:
            return self._reject(f"unsupported input_context field(s): {', '.join(unknown)}")

        parsed, error = self._parse_context(input_context)
        if error is not None:
            return self._reject(error)

        validated_input = json.dumps(
            {
                "text": sanitized_input,
                "record_hint": parsed["record_hint"],
                "entity_type": parsed["entity_type"],
                "intent": parsed["intent"],
                "attributes": parsed["attributes"],
            },
            ensure_ascii=False,
        )

        # Audit the shaped request - presence signals only, never the raw text
        # or any caller-supplied value.
        emit_trace_event(
            "pre_process_complete",
            {
                "has_record_hint": bool(parsed["record_hint"]),
                "entity_override": bool(parsed["entity_type"]),
                "intent_override": bool(parsed["intent"]),
                "n_attributes": len(parsed["attributes"]),
                "channel": parsed["channel"],
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "record_hint": parsed["record_hint"],
            "status": AgentStatus.SUCCESS.value,
        }

    # -- caller-data contract -------------------------------------------------

    def _parse_context(self, input_context: dict[str, Any]) -> "tuple[dict[str, Any], str | None]":
        """Validate every structured field; return (accepted, error-message-or-None)."""
        parsed: dict[str, Any] = {
            "record_hint": "",
            "entity_type": "",
            "intent": "",
            "attributes": {},
            "channel": "unknown",
        }

        for key in _RECORD_ID_KEYS:
            if key not in input_context:
                continue
            value = input_context[key]
            if not is_record_id(value):
                return parsed, f"input_context.{key} is not a valid record identifier"
            candidate = value.strip().lower()
            if parsed["record_hint"] and parsed["record_hint"] != candidate:
                return parsed, "input_context carries conflicting record identifiers"
            parsed["record_hint"] = candidate

        if "entity_type" in input_context:
            value = input_context["entity_type"]
            if not isinstance(value, str) or value.strip().lower() not in VALID_ENTITY_TYPES:
                return parsed, "input_context.entity_type is not a supported entity"
            parsed["entity_type"] = value.strip().lower()

        if "intent" in input_context:
            value = input_context["intent"]
            if not isinstance(value, str) or value.strip().lower() not in VALID_INTENTS:
                return parsed, "input_context.intent is not a supported operation"
            parsed["intent"] = value.strip().lower()

        if "channel" in input_context:
            value = input_context["channel"]
            if not is_inert_identifier(value):
                return parsed, "input_context.channel is not an inert identifier"
            parsed["channel"] = value

        if "attributes" in input_context:
            attributes, error = self._parse_attributes(input_context["attributes"])
            if error is not None:
                return parsed, error
            parsed["attributes"] = attributes

        return parsed, None

    def _parse_attributes(self, raw: Any) -> "tuple[dict[str, Any], str | None]":
        """Validate the caller's Dataverse attribute mapping, field by field."""
        if not isinstance(raw, dict):
            return {}, "input_context.attributes must be a mapping"
        if len(raw) > MAX_ATTRIBUTES:
            return {}, f"input_context.attributes carries more than {MAX_ATTRIBUTES} entries"

        accepted: dict[str, Any] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not ATTRIBUTE_KEY_RE.match(key.strip().lower()):
                return {}, "input_context.attributes carries an unsupported field name"
            name = key.strip().lower()
            if isinstance(value, bool):
                # A bool is never a Dataverse attribute value here, and
                # isinstance(True, int) would otherwise let it pass as a number.
                return {}, f"input_context.attributes.{name} is not a supported value"
            if isinstance(value, (int, float)):
                number = finite_in_range(value, -MAX_ATTRIBUTE_NUMBER, MAX_ATTRIBUTE_NUMBER)
                if number is None:
                    return {}, f"input_context.attributes.{name} is not a finite in-range number"
                accepted[name] = number
                continue
            if isinstance(value, str):
                if len(value) > MAX_ATTRIBUTE_CHARS:
                    return {}, f"input_context.attributes.{name} exceeds the maximum field length"
                # A numeric-looking STRING is held to the numeric contract: "NaN"
                # and "Infinity" are not int/float, so a type-based check waves
                # them through, and they become non-finite floats downstream.
                if looks_numeric(value) and finite_in_range(value, -MAX_ATTRIBUTE_NUMBER, MAX_ATTRIBUTE_NUMBER) is None:
                    return {}, f"input_context.attributes.{name} is not a finite in-range number"
                text = sanitize_query(value.strip(), MAX_ATTRIBUTE_CHARS)
                if is_instruction_override(text):
                    return {}, f"input_context.attributes.{name} refused - instruction-override content"
                accepted[name] = text
                continue
            return {}, f"input_context.attributes.{name} is not a supported value"
        return accepted, None

    @staticmethod
    def _reject(reason: str) -> dict[str, Any]:
        """Refuse the request: name the field, never echo the rejected value.

        A reason the caller can correct COMPLETES the run carrying that reason,
        so the caller receives it and can resend on the same conversation.
        Content the agent refuses outright still terminates - re-sending a
        reworded variant is not a correction.
        """
        if "instruction-override" in reason or "refused" in reason:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: {reason}"],
            }
        code = "EMPTY_INPUT" if "empty or missing" in reason else "INVALID_REQUEST"
        emit_progress(EMPTY_INPUT if code == "EMPTY_INPUT" else INPUT_REJECTED)
        return {
            "status": AgentStatus.SUCCESS.value,
            "error_code": code,
            "error_log": [f"PreProcessNode: {reason}"],
        }
