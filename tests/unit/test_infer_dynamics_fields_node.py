# CMN-C2-231 - Unit tests: InferDynamicsFieldsNode (inner Step 3)
#
# Canon: invoked via node(state), so BaseNode.__call__ routes the full
# security pipeline (trust gate -> input gate -> execute() -> output gate);
# inner domain node -> caller_trust_level = TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free: the framework input mask rewrites Title-Case
# bigrams in validated_input, so quoted display names use a single-word name
# and "Key: value" attribute values stay lower-case.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_dynamics_fields_node import InferDynamicsFieldsNode
from src.schemas.state import from_json, to_json

_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_dynamics_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_record", record_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "record_hint": record_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferDynamicsFieldsNode:
    def setup_method(self):
        self.node = InferDynamicsFieldsNode()

    def test_lookup_extracts_guid_from_text(self):
        result = self.node(_state(f"Look up the lead record {_GUID} and summarize it."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entity_type"] == "lead"
        assert result["record_id"] == _GUID
        # dynamics_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["dynamics_payload"], str)
        assert from_json(result["dynamics_payload"], {}) == {"entity_set": "leads", "record_id": _GUID}

    def test_guid_shaped_hint_used_when_text_has_no_guid(self):
        result = self.node(_state("Show the current status of the flagged lead", record_hint=_GUID))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == _GUID
        assert from_json(result["dynamics_payload"], {}) == {"entity_set": "leads", "record_id": _GUID}

    def test_create_builds_attribute_payload(self):
        # Field values stay lower-case: the framework name mask rewrites
        # Title-Case word pairs even ACROSS newlines before execute() sees the text.
        text = 'Create a new lead named "Aiko"\nCompany: contoso\nTopic: platform inquiry'
        result = self.node(_state(text, intent="create_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entity_type"] == "lead"
        assert result["record_name"] == "Aiko"
        payload = from_json(result["dynamics_payload"], {})
        assert payload["entity_set"] == "leads"
        assert payload["record_id"] == ""
        # Known keys map to Dataverse logical names (never sent unmapped).
        assert payload["data"] == {"companyname": "contoso", "subject": "platform inquiry"}

    def test_update_maps_numeric_attribute(self):
        text = f"Update the opportunity {_GUID}\nAmount: 5000"
        result = self.node(_state(text, intent="update_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entity_type"] == "opportunity"
        payload = from_json(result["dynamics_payload"], {})
        assert payload["entity_set"] == "opportunities"
        assert payload["record_id"] == _GUID
        assert payload["data"]["estimatedvalue"] == 5000.0

    def test_unknown_field_folds_into_description(self):
        # A live Dataverse call 400s on unknown attribute names, so unknown
        # "Key: value" pairs are preserved in description, never sent as attrs.
        text = 'Create a new lead named "Aiko"\nPriority: high'
        result = self.node(_state(text, intent="create_record"))
        payload = from_json(result["dynamics_payload"], {})
        assert payload["data"]["subject"] == "Aiko"
        assert payload["data"]["description"] == "Priority: high"
        assert "priority" not in payload["data"]

    def test_unresolved_guid_left_empty_never_invented(self):
        result = self.node(_state("Show the current status of the flagged lead"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == ""
        assert from_json(result["dynamics_payload"], {}) == {"entity_set": "leads", "record_id": ""}

    def test_non_guid_hint_left_unresolved(self):
        result = self.node(_state("Show the current status of the flagged lead", record_hint="not-a-guid"))
        assert result["record_id"] == ""

    def test_no_entity_word_defaults_to_lead_with_note(self):
        result = self.node(_state("Create a new entry for the quarterly rollout", intent="create_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entity_type"] == "lead"
        # Non-fatal documented-fallback note; status stays SUCCESS.
        assert any("defaulted to lead" in entry for entry in result.get("error_log", []))

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]


class TestCallerSuppliedFields:
    """Structured caller fields drive the request, under the same bounds."""

    def setup_method(self):
        self.node = InferDynamicsFieldsNode()

    def test_caller_entity_overrides_keyword_detection(self):
        result = self.node(
            _state("Register the platform rollout", intent="create_record", caller_entity_type="opportunity")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["entity_type"] == "opportunity"
        assert from_json(result["dynamics_payload"], {})["entity_set"] == "opportunities"
        # The documented lead fallback note must NOT appear: nothing defaulted.
        assert not any("defaulted to lead" in e for e in result.get("error_log", []))

    def test_caller_attributes_reach_the_request_body(self):
        result = self.node(
            _state(
                "Register the opportunity",
                intent="create_record",
                caller_attributes=to_json({"customer need": "agent platform", "estimated value": 1500000}),
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        data = from_json(result["dynamics_payload"], {})["data"]
        assert data["customerneed"] == "agent platform"
        assert data["estimatedvalue"] == 1500000.0

    def test_caller_attribute_wins_over_a_text_line(self):
        text = "Update the opportunity\nCustomer need: from the text"
        result = self.node(
            _state(
                text,
                intent="update_record",
                record_hint=_GUID,
                caller_attributes=to_json({"customer need": "from the contract"}),
            )
        )
        data = from_json(result["dynamics_payload"], {})["data"]
        assert data["customerneed"] == "from the contract"

    def test_unknown_caller_attribute_folds_into_description(self):
        result = self.node(
            _state("Register the opportunity", intent="create_record", caller_attributes=to_json({"priority": "high"}))
        )
        data = from_json(result["dynamics_payload"], {})["data"]
        assert data["description"] == "priority: high"
        assert "priority" not in data

    @pytest.mark.parametrize(
        "amount",
        ["NaN", "Infinity", "-Infinity", "1e400", "1e13"],
        ids=["nan", "inf", "neg-inf", "overflow", "over-range"],
    )
    def test_non_finite_amount_crossing_the_boundary_fails_closed(self, amount):
        """Defence in depth: a value that arrived out of contract must fail the
        request, never be written to the CRM as an unorderable number."""
        result = self.node(
            _state(
                "Update the opportunity",
                intent="update_record",
                record_hint=_GUID,
                caller_attributes=to_json({"estimated value": amount}),
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert "dynamics_payload" not in result

    def test_non_numeric_prose_amount_folds_into_description(self):
        """Prose is not a malformed number - it is preserved for the CRM user."""
        result = self.node(
            _state(
                "Update the opportunity",
                intent="update_record",
                record_hint=_GUID,
                caller_attributes=to_json({"estimated value": "to be confirmed"}),
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        data = from_json(result["dynamics_payload"], {})["data"]
        assert "estimatedvalue" not in data
        assert "to be confirmed" in data["description"]

    def test_non_mapping_caller_attributes_fail_closed(self):
        result = self.node(
            _state(
                "Register the opportunity",
                intent="create_record",
                caller_attributes=to_json(["customer need", "agent platform"]),
            )
        )
        assert result["status"] == AgentStatus.ERROR.value

    def test_grouped_amount_from_text_is_parsed(self):
        result = self.node(_state(f"Update the opportunity {_GUID}\nAmount: 1,500,000", intent="update_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["dynamics_payload"], {})["data"]["estimatedvalue"] == 1500000.0
