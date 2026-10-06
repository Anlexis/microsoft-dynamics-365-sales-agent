# CMN-C2-231 - Unit tests: ValidateInputNode (inner Step 1, flag-and-redact)
#
# Canon: nodes are invoked via node(state) - through BaseNode.__call__ (trust
# gate -> input mask -> execute -> credential scan) - never bare
# node.execute(state). This inner domain
# node declares ANONYMOUS, so the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value.
#
# Redaction layering exercised here:
#   * the FRAMEWORK input mask in __call__ rewrites emails (any '@') in
#     validated_input to "[MASKED]" BEFORE execute() sees the text - the
#     intentional-PII test asserts that [MASKED] path;
#   * the NODE's own deterministic scan handles token-shaped strings the
#     framework mask does not cover (secret_* / sk-* / eyJ*) - flag + [REDACTED].

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import from_json

_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.validate_input_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "validated_input": "Look up the lead record 3f2504e0-4f89-11d3-9a0c-0305e82c3301.",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "validate-input-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestValidateInputNode:
    def setup_method(self):
        self.node = ValidateInputNode()

    def test_success_plain_text(self):
        result = self.node(_state(validated_input="Show the current status of the flagged lead"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "Show the current status of the flagged lead"
        assert from_json(result["redaction_flags"], None) == []

    def test_success_serialized_json_input(self):
        payload = json.dumps({"text": "summarize the lead record on file", "record_hint": _GUID})
        result = self.node(_state(validated_input=payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "summarize the lead record on file"
        assert result["record_hint"] == _GUID

    def test_empty_input_errors(self):
        result = self.node(_state(validated_input="  "))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["error_log"]

    def test_short_input_errors(self):
        result = self.node(_state(validated_input="ab"))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_framework_mask_rewrites_email_before_execute(self):
        """Intentional-PII path: the framework input mask in __call__ rewrites the
        email to [MASKED] before execute() runs, so no raw address survives."""
        result = self.node(
            _state(validated_input="send the summary of the flagged lead record to sales.ops@example.com")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "sales.ops@example.com" not in result["validated_input"]
        assert "[MASKED]" in result["validated_input"]

    def test_node_redacts_token_shaped_string(self):
        """The node's own deterministic scan covers token shapes the framework
        PII mask does not (secret_*): flagged + [REDACTED] before logging."""
        text = "integration key secret_abcdef123456 for the flagged lead record"
        result = self.node(_state(validated_input=text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "secret_abcdef123456" not in result["validated_input"]
        assert "[REDACTED]" in result["validated_input"]
        assert "token" in from_json(result["redaction_flags"], [])

    def test_audit_emits_scan_outcome_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.validate_input_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state(validated_input="summarize the flagged lead record on file"))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - flags only, never the text.
        assert payloads["validate_input_complete"]["redaction_flags"] == []
        assert "text" not in payloads["validate_input_complete"]


class TestEnvelopeContract:
    """The envelope crosses a graph boundary, so its bounds are re-checked here."""

    def setup_method(self):
        self.node = ValidateInputNode()

    def _envelope(self, **fields) -> str:
        payload = {
            "text": "Register the opportunity for the platform rollout",
            "record_hint": "",
            "entity_type": "",
            "intent": "",
            "attributes": {},
        }
        payload.update(fields)
        return json.dumps(payload)

    def test_structured_fields_are_carried_forward(self):
        result = self.node(
            _state(
                validated_input=self._envelope(
                    record_hint=_GUID,
                    entity_type="opportunity",
                    intent="create_record",
                    attributes={"customer need": "agent platform", "estimated value": 1500000},
                )
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_hint"] == _GUID
        assert result["caller_entity_type"] == "opportunity"
        assert result["caller_intent"] == "create_record"
        assert from_json(result["caller_attributes"], {}) == {
            "customer need": "agent platform",
            "estimated value": 1500000,
        }

    @pytest.mark.parametrize(
        "fields",
        [
            {"record_hint": "not-a-guid"},
            {"entity_type": "account"},
            {"intent": "delete_record"},
            {"attributes": {"a": float("nan")}},
            {"attributes": {"a": True}},
            {"attributes": {"a": ["list"]}},
            {"attributes": {f"f{i}": "v" for i in range(25)}},
        ],
        ids=["record", "entity", "intent", "nan", "bool", "list", "too-many"],
    )
    def test_out_of_contract_envelope_field_fails_closed(self, fields):
        result = self.node(_state(validated_input=self._envelope(**fields)))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "caller_attributes" not in result

    def test_redaction_covers_the_structured_channel(self):
        """A sanitizer that only ran on the free text would let a token through
        on the structured channel; the scan must cover both."""
        result = self.node(
            _state(
                validated_input=self._envelope(
                    attributes={"description": "integration key secret_abcdef123456"},
                )
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        attributes = from_json(result["caller_attributes"], {})
        assert "secret_abcdef123456" not in attributes["description"]
        assert "[REDACTED]" in attributes["description"]
        assert "token" in from_json(result["redaction_flags"], [])

    def test_email_in_a_structured_attribute_never_survives(self):
        """Two independent layers cover the structured channel: the framework
        mask (the envelope rides in a scanned field) and this node's own scan.
        Whichever fires, the address must be gone."""
        result = self.node(
            _state(
                validated_input=self._envelope(
                    attributes={"description": "reply to sales.ops@example.com"},
                )
            )
        )
        attributes = from_json(result["caller_attributes"], {})
        assert "sales.ops@example.com" not in attributes["description"]

    def test_node_redacts_a_structured_email_without_the_framework_layer(self):
        """execute() DIRECTLY, so no framework wrapper is in front: the node's
        own scan must still remove the address - the guarantee cannot depend on
        a gate the deployment might not run."""
        result = self.node.execute(
            _state(
                validated_input=self._envelope(
                    attributes={"description": "reply to sales.ops@example.com"},
                )
            )
        )
        attributes = from_json(result["caller_attributes"], {})
        assert "sales.ops@example.com" not in attributes["description"]
        assert "[REDACTED]" in attributes["description"]
        assert "email" in from_json(result["redaction_flags"], [])

    def test_malformed_envelope_is_treated_as_plain_text(self):
        result = self.node(_state(validated_input="{not json at all"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "{not json at all"
