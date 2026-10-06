"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the looked-up / created / updated Dynamics 365 record (entity + id +
reference + name) into a human-readable confirmation message, surfacing the
affected record for human review.

The display label is the one caller-derived string that renders into the
confirmation, so it is stripped of markup and control characters and capped
before it is composed into the message.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.security import sanitize_query

# Cap on the record label rendered into the confirmation message.
_MAX_LABEL_CHARS = 100

_VERBS = {
    "lookup_record": "Retrieved Dynamics 365 record",
    "create_record": "Created Dynamics 365 record",
    "update_record": "Updated Dynamics 365 record",
}


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # Inner domain node, read-only formatting of already-fetched data - the
    # external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        record_id = state.get("record_id", "")
        record_ref = state.get("record_ref", "")
        record_name = state.get("record_name", "")
        entity_type = state.get("entity_type", "")
        intent = state.get("intent", "lookup_record")

        if not record_id and not record_ref:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no record_id/record_ref to confirm"],
            }

        verb = _VERBS.get(intent, "Processed Dynamics 365 record")
        label = sanitize_query(str(record_name or record_id), _MAX_LABEL_CHARS)
        head = f"{verb} ({entity_type}) '{label}'" if entity_type else f"{verb} '{label}'"
        parts = [head]
        if record_ref:
            parts.append(f"ref={record_ref}")
        if record_id:
            parts.append(f"id={record_id}")
        confirmation = " - ".join(parts)

        # Audit the confirmed action - intent + reference presence (no content).
        emit_trace_event(
            "confirm_complete",
            {"intent": intent, "entity_type": entity_type, "has_record_ref": bool(record_ref)},
            state,
        )

        return {
            "confirmation": confirmation,
            "result": {
                "record_id": record_id,
                "record_ref": record_ref,
                "confirmation": confirmation,
            },
            "status": AgentStatus.SUCCESS.value,
        }
