"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_record / create_record /
update_record. A caller who states the operation explicitly through the
caller-data contract gets exactly that operation - an explicit instruction is
never second-guessed by a heuristic. Otherwise a deterministic keyword heuristic
decides, so the template is testable and runnable without a live LLM (see "LLM
synthesis" in docs/02_design.md). Low-confidence / unknown falls back to the
read-only "lookup_record" default with a note - never a write.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("lookup_record", "create_record", "update_record")

# Deterministic keyword signals (checked in priority order, writes first so a
# "update the opportunity then show it" style request classifies as the write).
_KEYWORDS = (
    (
        "update_record",
        (
            "update",
            "change",
            "edit",
            "correct",
            "amend",
            "revise",
            "qualify",
            "disqualify",
            "reopen",
            "close the",
            "mark the",
            "set the",
            "set her",
            "set his",
            "reschedule",
            "更新",
            "変更",
            "修正",
            "訂正",
            "クローズ",
        ),
    ),
    (
        "create_record",
        (
            "create",
            "register",
            "log a",
            "log an",
            "add a",
            "add an",
            "new lead",
            "new opportunity",
            "new contact",
            "add a new",
            "登録",
            "追加",
            "新規",
            "作成",
            "起票",
        ),
    ),
    (
        "lookup_record",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "what is",
            "who is",
            "summarize",
            "record for",
            "record of",
            "details of",
            "照会",
            "検索",
            "参照",
            "確認",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a Dynamics 365 Sales record operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        # An explicit caller-supplied operation wins over the heuristic; it was
        # bounds-checked against the accepted set before it reached this node,
        # and is re-checked here so an out-of-contract value cannot slip
        # through the graph boundary into a write.
        caller_intent = state.get("caller_intent", "") or ""
        caller_override = caller_intent in _VALID_INTENTS
        intent = caller_intent if caller_override else self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_record (read-only)"]
            intent = "lookup_record"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note), "caller_specified": caller_override},
            state,
        )

        result = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
