"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner Dynamics 365
workflow graph has run. GraphNode.merge_output() maps the inner result into the
outer state; this node shapes the caller-facing `formatted_output`.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT the
framework `_extra_security_gate_output` hook - the framework gate methods are
@final on FunctionNode and the real SDK auto-wraps `_extra_` hooks (which breaks
the .invoke() chain), so domain checks live in a module-level helper invoked
inline.

The stated output invariant, and what "completely" means for it
---------------------------------------------------------------
This agent's caller-facing contract has exactly two promises:

  1. a SUCCESS response always carries record evidence (record_id/record_ref) -
     a success without it would misrepresent what happened in the CRM;
  2. no credential-shaped string ever reaches the caller.

Both are enforced over EVERY representation of the response, not the convenient
ones: nested mappings and lists inside `dynamics_payload` (which echoes the
assembled Dataverse attribute body) and non-string scalars. A gate that walked
only the top-level string values would pass a token nested one level down.

What a refusal must do
----------------------
Refusing is not the same as containing. The response envelope this agent hands
back resolves its payload as `formatted_output or result`, with no look at the
status, and `result` holds the inner workflow's own answer by the time this node
runs. So a gate that merely returns an error status still publishes the answer
it just refused, wrapped in an envelope that calls itself an error. A refusal
here therefore clears EVERY output-bearing field and replaces `formatted_output`
with a non-empty error envelope - non-empty because a falsy value falls through
to `result` and re-opens the same hole.

What the ERROR envelope may say
-------------------------------
The caller-visible ERROR envelope carries closed-set labels only: a constant
reason code chosen by this module (one of `ERROR_REASONS`). It never carries
`error_log`, the gate's violation entries, or any other node-authored text.
Those lines can embed upstream response text, identifiers, names or
caller-supplied field names, and truncating or redacting them is not a closed
set. `error_log` stays the INTERNAL channel - the state reducer appends to it
and the audit trail needs it - it is simply never projected to the caller. The
same helper produces the envelope on every non-success path (inner workflow
error, gate refusal), so no path can publish more than the reason code.

A violation entry written to `error_log` names the LOCATION of the problem and
never quotes what was found there. Quoting it would copy the offending text into
the node result, where the framework's credential scan raises on it; the node
result is then replaced wholesale by a generic error and every field cleared
above comes back. The gate would report a leak while causing one.

Note on numeric rendering: this agent renders no monetary aggregate. The values
that appear in the response are the caller's own attribute values echoed back
verbatim inside the assembled request descriptor, plus Dataverse identifiers -
so there is no rounding grid to enforce, and imposing one would be actively
harmful: a rounding pass over the response would rewrite record identifiers and
`dynamics://` references, which are the evidence the contract above is built on.
The invariant enforced here is the repo's own, applied everywhere.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework credential scan in FunctionNode also runs on every result).
_CREDENTIAL_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9._-]{10,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}")
# How deep the walk goes before it refuses to keep descending. A structure
# deeper than this is itself out of contract.
_MAX_DEPTH = 12

# Every state field that can reach the caller, directly or through the response
# envelope's `formatted_output or result` fallback. A refusal empties all of
# them; leaving any one populated publishes what the gate withheld.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "formatted_output",
    "record_id",
    "record_ref",
    "record_name",
    "entity_type",
    "intent",
    "confirmation",
    "dynamics_payload",
)

# Reason codes - the ONLY values the caller-visible ERROR envelope may carry.
# Chosen here, never derived from state, so the envelope is a closed set.
_REASON_WORKFLOW_FAILED = "workflow_failed"  # the inner workflow reported an error
_REASON_OUTPUT_WITHHELD = "output_withheld"  # the output gate refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})

# Stand-in for a location label that cannot itself be written to error_log.
_UNNAMEABLE_LOCATION = "<withheld>"


def _safe_location(location: str) -> str:
    """A location label that is safe to write into the node's error_log entry.

    The label is composed from the response's own key names, and the assembled
    attribute body carries caller-supplied field names - so a label can itself
    be credential-shaped. Such a label is replaced rather than quoted: it would
    otherwise travel in the node result, where the framework credential scan
    raises on it and discards the cleared fields that make the refusal safe.

    The framework's own detector decides, so the labels held back here are
    exactly the ones it blocks; a local approximation would drift from it.
    """
    return _UNNAMEABLE_LOCATION if detect_credentials(location) else location


def _contain(reason: str, new_errors: "list[str] | None" = None) -> dict[str, Any]:
    """The node result for any non-success outcome.

    Error status, every output-bearing field emptied, and an envelope made of
    closed-set labels only: `reason` is one of ERROR_REASONS. `new_errors` are
    appended to `error_log` - the internal channel, accumulated by the state
    reducer - and never enter the envelope.
    """
    contained: dict[str, Any] = {field: "" for field in _OUTPUT_BEARING_FIELDS}
    contained["result"] = None
    # A constant key keeps the envelope truthy: the response resolves
    # `formatted_output or result`, so an empty mapping here would fall through
    # to `result` and defeat the clearing above.
    contained["formatted_output"] = {"reason": reason}
    contained["status"] = AgentStatus.ERROR.value
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


def _credential_paths(value: Any, path: str = "", depth: int = 0) -> "list[str]":
    """Every location inside `value` whose text is credential-shaped.

    Walks mappings, sequences and scalars alike: the response embeds the
    assembled Dataverse attribute body, so a credential can sit at any depth,
    and a non-string scalar is rendered as text to the caller just the same.
    """
    if depth > _MAX_DEPTH:
        return [path or "<root>"]
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            found.extend(_credential_paths(item, f"{path}.{key}" if path else str(key), depth + 1))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        for index, item in enumerate(value):
            found.extend(_credential_paths(item, f"{path}[{index}]", depth + 1))
        return found
    if value is None or isinstance(value, bool):
        return []
    text = value if isinstance(value, str) else str(value)
    return [path or "<root>"] if _CREDENTIAL_LIKE_RE.search(text) else []


def _security_gate_output(formatted_output: dict[str, Any], is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Returns a violation for:
      - a SUCCESS response with no record evidence (record_id/record_ref), which
        would misrepresent the Dynamics 365 action outcome to the caller;
      - a credential-shaped string ANYWHERE in the caller-facing output,
        including nested mappings and lists.

    Violations are error_log entries (internal); they never reach the caller.
    """
    problems: list[str] = []
    if is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing record_id/record_ref evidence")
    for location in _credential_paths(formatted_output):
        where = _safe_location(location)
        problems.append(f"PostProcess output gate: credential-like value in formatted_output['{where}']")
    return problems


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "output": message,
                "formatted_output": message,
                "result": message,
            }
        # If the inner workflow errored, preserve the error status (do not mask
        # it) and publish nothing of it: error_log already carries the inner
        # entries, and the caller receives the reason code only.
        if state.get("status") == AgentStatus.ERROR.value:
            return _contain(_REASON_WORKFLOW_FAILED)

        formatted_output = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "record_name": state.get("record_name", ""),
            "entity_type": state.get("entity_type", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "dynamics_payload": from_json(state.get("dynamics_payload"), {}),
        }

        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_record_id": bool(state.get("record_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
