# CMN-C2-231 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state), so BaseNode.__call__ routes the full
# security pipeline (trust gate -> input gate -> execute() -> output gate);
# inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode

_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": _GUID,
        "record_ref": f"dynamics://leads/{_GUID}",
        "record_name": "Record 3f2504e0",
        "entity_type": "lead",
        "intent": "lookup_record",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved Dynamics 365 record" in result["confirmation"]
        assert "(lead)" in result["confirmation"]
        assert "Record 3f2504e0" in result["confirmation"]
        assert f"ref=dynamics://leads/{_GUID}" in result["confirmation"]
        assert f"id={_GUID}" in result["confirmation"]
        assert result["result"]["record_id"] == _GUID
        assert result["result"]["record_ref"] == f"dynamics://leads/{_GUID}"

    def test_create_verb(self):
        result = self.node(_state(intent="create_record", record_name="Aiko"))
        assert "Created Dynamics 365 record" in result["confirmation"]

    def test_update_verb(self):
        result = self.node(_state(intent="update_record"))
        assert "Updated Dynamics 365 record" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed Dynamics 365 record" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert f"id={_GUID}" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_name_missing(self):
        result = self.node(_state(record_name=""))
        assert f"'{_GUID}'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_reference_presence_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.confirm_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        assert payloads["confirm_complete"]["intent"] == "lookup_record"
        assert payloads["confirm_complete"]["entity_type"] == "lead"
        assert payloads["confirm_complete"]["has_record_ref"] is True
