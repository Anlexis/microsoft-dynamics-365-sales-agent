"""AgentCore Platform v1.0 - CMN-C2-231 Microsoft Dynamics 365 Sales Agent state."""

# The framework wheel ships no py.typed marker, so AgentState resolves as Any and
# the type checker cannot see this class as a TypedDict - which makes every
# NotRequired[...] declaration below look misplaced to it. The declarations are
# correct and load-bearing (a domain field is absent until its producer node
# writes it), so the check is disabled for this file only.
# mypy: disable-error-code="valid-type"

# State must be a flat TypedDict - never a Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects (and
# nested dict/list containers) are not msgpack-safe. Extend AgentState with
# agent-specific fields only, and declare every domain field NotRequired[...]
# (fields are absent until their producer node writes them). dynamics_payload /
# dynamics_config / redaction_flags are dicts/lists at the point of use but are
# stored in State as JSON strings via to_json/from_json below. Do NOT add
# credentials, secrets, or Pydantic models. The Dynamics 365
# OAuth token is NEVER stored here - it is read via ctx.secrets in
# CallDynamicsApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (checkpoint-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Dynamics 365 Sales agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only Dynamics-365-workflow fields are added below, all NotRequired
    (state contract). All values are JSON/msgpack-serializable
    primitives - the Dynamics 365 OAuth token is NEVER stored here (accessed
    via ctx.secrets).
    """

    # Caller-supplied target hint (record GUID from input_context / the
    # request envelope). Never inferred; resolution to a Dataverse record GUID
    # is explicit-only (v1: pass-through when the hint or request text already
    # carries a GUID).
    record_hint: NotRequired[str]
    record_id: NotRequired[str]  # resolved Dataverse record GUID

    # ValidateInput (deterministic redaction scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # Caller-supplied structured overrides, validated by PreProcessNode against
    # the caller-data contract and carried forward by ValidateInputNode. Empty /
    # absent means "no override" - the deterministic heuristics then decide.
    caller_entity_type: NotRequired[str]
    caller_intent: NotRequired[str]
    # JSON mapping of caller-supplied Dataverse attribute fields (stored as a
    # JSON string; (de)serialize via to_json/from_json).
    caller_attributes: NotRequired[Optional[str]]

    # InferDynamicsFields
    entity_type: NotRequired[str]  # resolved Dataverse entity: lead / opportunity / contact
    record_name: NotRequired[str]  # record display label (lead topic / opportunity name / contact full name)
    # JSON - assembled Dynamics 365 Web API request descriptor
    # (entity_set + record_id + attribute body) (stored as a JSON string,
    # not a native dict; (de)serialize via to_json/from_json).
    dynamics_payload: NotRequired[Optional[str]]

    # Connector settings + request budget forwarded by _parent_config() and
    # injected by the inner graph's _extra_initial_state() (JSON string).
    dynamics_config: NotRequired[Optional[str]]

    # CallDynamicsApi
    record_ref: NotRequired[str]  # human-readable reference (dynamics://<entity_set>/<guid>)

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
    error_code: Optional[str]
