# CMN-C2-231 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: lookup_record / create_record / update_record (deterministic
# keyword heuristic, no LLM; unknown falls back to the read-only lookup).
#
# Canon: invoked via node(state), so BaseNode.__call__ routes the full
# security pipeline (trust gate -> input gate -> execute() -> output gate);
# inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, **overrides) -> dict:
    state = {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_record(self):
        result = self.node(_state("Look up the lead record 3f2504e0-4f89-11d3-9a0c-0305e82c3301."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_record"

    def test_keyword_create_record(self):
        result = self.node(_state("Create a new lead for the platform rollout"))
        assert result["intent"] == "create_record"

    def test_keyword_update_record(self):
        result = self.node(_state("Update the opportunity with the revised amount"))
        assert result["intent"] == "update_record"

    def test_write_keyword_wins_over_lookup(self):
        # Priority order is writes-first: an "update ... then show it" style
        # request classifies as the write, never the read.
        result = self.node(_state("Update the flagged lead record and show the result"))
        assert result["intent"] == "update_record"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_record"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_record" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up the lead record 3f2504e0-4f89-11d3-9a0c-0305e82c3301."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_record"
        assert payloads["classify_intent_complete"]["defaulted"] is False


class TestCallerSuppliedIntent:
    """An explicitly stated operation is honoured; anything else is refused."""

    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_caller_intent_wins_over_the_keyword_heuristic(self):
        result = self.node(_state("Look up the flagged lead", caller_intent="update_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "update_record"

    def test_out_of_contract_caller_intent_falls_back_to_the_heuristic(self):
        """A value that crossed the graph boundary out of contract must never
        become the operation - the read-only heuristic decides instead."""
        result = self.node(_state("Look up the flagged lead", caller_intent="delete_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_record"

    def test_audit_records_whether_the_caller_specified_the_operation(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Register the new opportunity", caller_intent="create_record"))
        payload = {args[0]: args[1] for args in events}["classify_intent_complete"]
        assert payload["caller_specified"] is True
