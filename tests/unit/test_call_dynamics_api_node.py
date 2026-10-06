# CMN-C2-231 - Unit tests: CallDynamicsApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state), so BaseNode.__call__ routes the full
# security pipeline (trust gate -> input gate -> execute() -> output gate);
# inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, trust gate unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's Dynamics365Client
# symbol (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_dynamics_api_node import CallDynamicsApiNode
from src.services.dynamics_client import Dynamics365ApiError
from src.schemas.state import to_json

_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_dynamics_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "dynamics_payload": to_json({"entity_set": "leads", "record_id": _GUID}),
        "intent": "lookup_record",
        "entity_type": "lead",
        "record_id": _GUID,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-dynamics-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for Dynamics365Client: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def get_record(self, entity_set, record_id, api_token):
        raise Dynamics365ApiError(403, "forbidden by Dataverse privileges")


class _FakeLiveClient:
    """Stands in for Dynamics365Client with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def get_record(self, entity_set, record_id, api_token):
        _FakeLiveClient.captured = {
            "entity_set": entity_set,
            "record_id": record_id,
            "api_token": api_token,
        }
        return {"leadid": record_id, "subject": f"Record {record_id[:8]}"}


class TestCallDynamicsApiNode:
    def setup_method(self):
        self.node = CallDynamicsApiNode()

    def test_lookup_success_via_default_transport(self):
        # Default transport = the deterministic, network-free stand-in; no
        # secret provider bound -> the node runs on the documented placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == _GUID
        assert result["record_ref"] == f"dynamics://leads/{_GUID}"
        assert result["record_name"] == "Record 3f2504e0"

    def test_create_success_via_default_transport(self):
        state = _state(
            intent="create_record",
            record_id="",
            dynamics_payload=to_json(
                {
                    "entity_set": "leads",
                    "record_id": "",
                    "data": {"subject": "platform inquiry", "companyname": "contoso"},
                }
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # The stand-in returns the Prefer: return=representation record with a
        # synthetic primary-key GUID - presence, never a fabricated live id.
        assert result["record_id"]
        assert result["record_ref"].startswith("dynamics://leads/")

    def test_update_success_via_default_transport(self):
        state = _state(
            intent="update_record",
            dynamics_payload=to_json(
                {
                    "entity_set": "leads",
                    "record_id": _GUID,
                    "data": {"subject": "revised topic"},
                }
            ),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == _GUID

    def test_dynamics_config_state_field_sets_base_url(self):
        # The inner graph injects the connector settings as the JSON
        # dynamics_config state field; the default transport still serves the call.
        state = _state(dynamics_config=to_json({"base_url": "https://contoso.crm7.dynamics.com/api/data/v9.2"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == f"dynamics://leads/{_GUID}"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"dynamics": {"base_url": "https://contoso.crm7.dynamics.com/api/data/v9.2"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == _GUID

    def test_missing_payload_errors(self):
        result = self.node(_state(dynamics_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_missing_entity_set_errors(self):
        result = self.node(_state(dynamics_payload=to_json({"record_id": _GUID})))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("entity_set" in entry for entry in result["error_log"])

    def test_lookup_with_unresolved_guid_errors(self):
        state = _state(record_id="", dynamics_payload=to_json({"entity_set": "leads", "record_id": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved record GUID" in entry for entry in result["error_log"])

    def test_update_with_unresolved_guid_errors(self):
        state = _state(
            intent="update_record",
            record_id="",
            dynamics_payload=to_json({"entity_set": "leads", "record_id": "", "data": {"subject": "x"}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_record"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_dynamics_api_node.Dynamics365Client", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing DYNAMICS_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_dynamics_api_node.Dynamics365Client", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_dynamics_api_node.Dynamics365Client", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"DYNAMICS_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["entity_set"] == "leads"
        assert _FakeLiveClient.captured["record_id"] == _GUID

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_dynamics_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_dynamics_api_complete"]
        assert payload["intent"] == "lookup_record"
        assert payload["entity_type"] == "lead"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True


class TestRequestBudgetPlumbing:
    """The declared budget must reach the client, not stop at the state field."""

    def setup_method(self):
        self.node = CallDynamicsApiNode()

    def test_declared_budget_reaches_the_client(self, monkeypatch):
        captured = {}

        class _RecordingClient:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

            uses_stub_transport = True

            def get_record(self, entity_set, record_id, api_token):
                return {"leadid": record_id, "subject": "Record"}

        monkeypatch.setattr("src.nodes.call_dynamics_api_node.Dynamics365Client", _RecordingClient)
        self.node(
            _state(
                dynamics_config=to_json({"base_url": "https://contoso.crm7.dynamics.com/api/data/v9.2", "timeout_s": 7})
            )
        )
        assert captured["timeout_s"] == 7.0

    @pytest.mark.parametrize(
        "declared",
        ["NaN", float("nan"), float("inf"), -5, 0, 100_000, True, "soon"],
        ids=["str-nan", "raw-nan", "raw-inf", "negative", "zero", "over-range", "bool", "text"],
    )
    def test_unusable_budget_falls_back_to_the_default(self, monkeypatch, declared):
        """A NaN budget compares as 'never exceeded' - it must never be trusted."""
        captured = {}

        class _RecordingClient:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

            uses_stub_transport = True

            def get_record(self, entity_set, record_id, api_token):
                return {"leadid": record_id, "subject": "Record"}

        monkeypatch.setattr("src.nodes.call_dynamics_api_node.Dynamics365Client", _RecordingClient)
        self.node(_state(dynamics_config=to_json({"timeout_s": declared})))
        assert captured["timeout_s"] == 30.0

    def test_transport_failure_never_echoes_the_transport_message(self, monkeypatch):
        """A transport exception can carry the request URL, headers or a bearer
        token, so only the failure class is reported."""
        leaky = "Bearer " + "a" * 24

        class _LeakyClient:
            def __init__(self, *args, **kwargs):
                pass

            uses_stub_transport = True

            def get_record(self, entity_set, record_id, api_token):
                raise RuntimeError(f"connection to {leaky} refused")

        monkeypatch.setattr("src.nodes.call_dynamics_api_node.Dynamics365Client", _LeakyClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        joined = "\n".join(result["error_log"])
        assert leaky not in joined
        assert "transport error" in joined

    def test_api_error_body_is_not_echoed(self, monkeypatch):
        """A remote error body is upstream content - it can carry identifiers,
        names or echoed headers - so the log line carries the HTTP status only."""

        class _ShoutyClient:
            def __init__(self, *args, **kwargs):
                pass

            uses_stub_transport = True

            def get_record(self, entity_set, record_id, api_token):
                raise Dynamics365ApiError(400, "x" * 5000)

        monkeypatch.setattr("src.nodes.call_dynamics_api_node.Dynamics365Client", _ShoutyClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        entry = result["error_log"][0]
        assert "400" in entry
        assert "x" * 20 not in entry
        assert len(entry) < 300
