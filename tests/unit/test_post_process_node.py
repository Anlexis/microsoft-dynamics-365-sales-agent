# CMN-C2-231 - Unit tests: PostProcessNode (outer backbone, domain output gate)
#
# Canon: invoked via node(state), so BaseNode.__call__ routes the full security
# pipeline; this backbone formatter declares ANONYMOUS -> the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value. The domain output gate is the
# MODULE-LEVEL _security_gate_output() helper (the framework gate methods are
# @final and the SDK auto-wraps the _extra_ hooks), so the helper is also
# unit-tested directly as a plain function.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    ERROR_REASONS,
    _OUTPUT_BEARING_FIELDS,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    PostProcessNode,
    _security_gate_output,
)
from src.schemas.state import to_json

_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": _GUID,
        "record_ref": f"dynamics://leads/{_GUID}",
        "record_name": "Record 3f2504e0",
        "entity_type": "lead",
        "intent": "lookup_record",
        "confirmation": f"Retrieved Dynamics 365 record (lead) 'Record 3f2504e0' - ref=dynamics://leads/{_GUID} - id={_GUID}",
        "dynamics_payload": to_json({"entity_set": "leads", "record_id": _GUID}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _bearer() -> str:
    # Built at runtime so no credential-shaped literal is committed.
    return "Bearer " + "a" * 24


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. The token is
    assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


# Fragments of the sentinel that must survive nowhere in a returned mapping.
_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


def _leaves(value):
    """Every key and scalar inside `value`, rendered as text, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
    else:
        yield str(value)


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: AgentStatus is a str-Enum, so == passes for the bare
        # enum too - only an exact type check catches a bare-enum write.
        # isinstance() would pass for the str-Enum itself, which is the bug
        # this guards against - the exact type check is deliberate.
        assert type(result["status"]) is str  # noqa: E721
        out = result["formatted_output"]
        assert out["record_id"] == _GUID
        assert out["record_ref"] == f"dynamics://leads/{_GUID}"
        assert out["entity_type"] == "lead"
        assert out["intent"] == "lookup_record"
        assert out["confirmation"].startswith("Retrieved Dynamics 365 record")
        # Round-trip: the JSON dynamics_payload string surfaces parsed.
        assert out["dynamics_payload"] == {"entity_set": "leads", "record_id": _GUID}

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallDynamicsApiNode: Dynamics 365 API error 403"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "Dynamics 365 API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": _GUID, "record_ref": f"dynamics://leads/{_GUID}", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        violations = _security_gate_output(
            {"record_id": _GUID, "note": _bearer()},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []


class TestOutputInvariantEveryRepresentation:
    """The stated invariant holds for every representation, not the convenient ones."""

    def setup_method(self):
        self.node = PostProcessNode()

    def test_credential_nested_in_the_request_descriptor_is_blocked(self):
        """dynamics_payload echoes the assembled attribute body: a token one
        level down must not sail past a top-level-only scan."""
        result = self.node(
            _state(
                dynamics_payload=to_json(
                    {"entity_set": "leads", "record_id": _GUID, "data": {"description": _bearer()}}
                )
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any("credential-like" in entry for entry in result["error_log"])

    def test_credential_inside_a_list_is_blocked(self):
        violations = _security_gate_output(
            {"record_id": _GUID, "notes": ["fine", {"deep": [_bearer()]}]},
            is_success=True,
        )
        assert any("notes" in v for v in violations)

    def test_credential_in_a_non_string_scalar_is_blocked(self):
        class _Rendered:
            def __str__(self) -> str:
                return "Bearer " + "a" * 24

        violations = _security_gate_output({"record_id": _GUID, "obj": _Rendered()}, is_success=True)
        assert any("obj" in v for v in violations)

    def test_credential_in_the_error_path_never_reaches_the_caller(self):
        """A remote 4xx body would arrive in error_log. The error path publishes
        the reason code only, so the body cannot reach the caller through it.

        execute() is called DIRECTLY here on purpose: BaseNode.__call__
        short-circuits on an already-errored state and the backbone route()
        skips post_process on error, so this branch is defensive and cannot be
        reached through the node call - which is exactly why it is tested."""
        result = self.node.execute(
            _state(
                status=AgentStatus.ERROR.value,
                error_log=[f"CallDynamicsApiNode: rejected {_bearer()}"],
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert _bearer() not in json.dumps(result, default=str)

    def test_ordinary_error_text_is_not_projected_to_the_caller(self):
        """Even an innocuous-looking error line stays internal: the envelope is
        a closed set, not a redaction of error_log."""
        message = "CallDynamicsApiNode: Dynamics 365 API error 403"
        result = self.node.execute(
            _state(status=AgentStatus.ERROR.value, record_id="", record_ref="", error_log=[message])
        )
        assert result["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert message not in json.dumps(result, default=str)

    def test_identifiers_and_references_are_rendered_verbatim(self):
        """This agent renders no monetary aggregate, so no rounding pass exists
        - and must not: it would rewrite the record identifiers and references
        that ARE the evidence the success contract depends on."""
        result = self.node(_state())
        out = result["formatted_output"]
        assert out["record_id"] == _GUID
        assert out["record_ref"] == f"dynamics://leads/{_GUID}"

    def test_deeply_nested_structure_is_refused_rather_than_walked_forever(self):
        payload = current = {}
        for _ in range(30):
            current["next"] = {}
            current = current["next"]
        violations = _security_gate_output({"record_id": _GUID, "deep": payload}, is_success=True)
        assert violations


class TestRefusalClearsTheResponse:
    """A refusal returns error status AND empties every output-bearing field.

    The envelope reads `formatted_output or result` without checking the status,
    and `result` already holds the inner answer when this node runs - so an
    error return that leaves the state populated publishes what it just refused.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _refused(self) -> dict:
        return self.node(
            _state(
                result={"record_id": _GUID, "confirmation": "Retrieved Dynamics 365 record"},
                dynamics_payload=to_json({"entity_set": "leads", "data": {"description": _bearer()}}),
            )
        )

    def test_every_output_bearing_field_is_emptied(self):
        result = self._refused()

        assert result["status"] == AgentStatus.ERROR.value
        for field in _OUTPUT_BEARING_FIELDS:
            assert field in result, f"{field} left untouched - it would keep its refused value"
        emptied = [f for f in _OUTPUT_BEARING_FIELDS if f != "formatted_output"]
        assert all(not result[field] for field in emptied), {f: result[f] for f in emptied}

    def test_formatted_output_is_replaced_by_a_non_empty_reason_code(self):
        """`formatted_output` is the one cleared field that must stay truthy:
        a falsy value falls through to `result` and restores the answer."""
        result = self._refused()

        assert result["formatted_output"], "a falsy formatted_output re-opens the `result` fallback"
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_the_error_path_clears_the_same_fields(self):
        """execute() directly: the node call short-circuits on an already
        errored state, so this branch is only reachable from inside."""
        result = self.node.execute(
            _state(
                status=AgentStatus.ERROR.value,
                result={"record_id": _GUID},
                error_log=[f"CallDynamicsApiNode: rejected {_bearer()}"],
            )
        )

        assert result["status"] == AgentStatus.ERROR.value
        assert result["result"] is None
        emptied = [f for f in _OUTPUT_BEARING_FIELDS if f != "formatted_output"]
        assert all(not result[field] for field in emptied), {f: result[f] for f in emptied}
        assert result["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert _bearer() not in str(result)

    def test_a_credential_shaped_location_label_is_withheld_too(self):
        """The label is built from the response's own keys, and the assembled
        attribute body carries caller-supplied field names. A label that is
        itself credential-shaped cannot be quoted: it would ride in this result,
        the framework credential scan would raise on it, and the raise replaces
        the cleared result - restoring the leak the clearing just closed."""
        credential_shaped_key = "sk-" + "a" * 20
        result = self.node(_state(dynamics_payload=to_json({"entity_set": "leads", credential_shaped_key: _bearer()})))

        assert result["status"] == AgentStatus.ERROR.value
        # The clearing survived, i.e. the framework scan found nothing to raise on.
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert credential_shaped_key not in str(result)
        assert any("<withheld>" in entry for entry in result["error_log"])

    def test_a_clean_response_is_not_cleared(self):
        """Control: a gate that refused everything would pass every assertion
        above, so the unrefused path is pinned here."""
        result = self.node(_state())

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"]["record_id"] == _GUID
        assert "reason" not in result["formatted_output"]
        assert "result" not in result


# Every non-success path this node has. `via` says how the path is reached:
# node(state) runs the framework pipeline; execute() is used for an errored
# state because BaseNode.__call__ short-circuits on it (and the backbone routes
# an error straight to finalize), so that branch is only reachable from inside.
_ERROR_PATHS = [
    pytest.param(
        {"status": AgentStatus.ERROR.value, "record_id": "", "record_ref": ""},
        "execute",
        id="inner-workflow-error",
    ),
    pytest.param(
        {"status": AgentStatus.ERROR.value, "result": {"record_id": _GUID}},
        "execute",
        id="inner-workflow-error-with-answer-in-result",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "error_log": [_sentinel(), "CallDynamicsApiNode: rejected " + _bearer()],
        },
        "execute",
        id="inner-workflow-error-with-credential-in-error-log",
    ),
    pytest.param({"record_id": "", "record_ref": ""}, "call", id="gate-missing-record-evidence"),
    pytest.param(
        {"dynamics_payload": to_json({"entity_set": "leads", "record_id": _GUID, "data": {"description": _bearer()}})},
        "call",
        id="gate-credential-in-request-descriptor",
    ),
    pytest.param(
        {"dynamics_payload": to_json({"entity_set": "leads", "record_id": _GUID, "sk-" + "a" * 20: _bearer()})},
        "call",
        id="gate-credential-shaped-field-name",
    ),
]


class TestErrorEnvelopeIsClosedSet:
    """Whatever the non-success path, the caller-visible envelope is made of
    this module's own constants - never of node-authored text.

    error_log is seeded with a recognisable sentinel on every path: a name and
    a credential-shaped token inside an echoed upstream body, which is exactly
    what an API-call failure can put there. Truncating or redacting such a line
    is not a closed set, so it must appear nowhere in what the node returns.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _drive(self, overrides: dict, via: str) -> dict:
        state = _state(**{"error_log": [_sentinel()], **overrides})
        return self.node.execute(state) if via == "execute" else self.node(state)

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_values_are_declared_constants(self, overrides, via):
        result = self._drive(overrides, via)

        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == {"reason"}, envelope
        assert set(envelope.values()) <= ERROR_REASONS, envelope

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_envelope_stays_truthy(self, overrides, via):
        """`formatted_output or result`: a falsy envelope re-opens the fallback."""
        result = self._drive(overrides, via)

        assert result["formatted_output"], "a falsy formatted_output re-opens the `result` fallback"
        assert result["result"] is None

    @pytest.mark.parametrize(("overrides", "via"), _ERROR_PATHS)
    def test_seeded_error_text_appears_nowhere_in_the_returned_mapping(self, overrides, via):
        result = self._drive(overrides, via)

        leaves = list(_leaves(result))
        for fragment in _SENTINEL_FRAGMENTS:
            assert not any(fragment in leaf for leaf in leaves), (fragment, result)
        rendered = json.dumps(result, default=str)
        assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered

    def test_the_reason_names_the_path_taken(self):
        errored = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))
        withheld = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert errored["formatted_output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert withheld["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_inner_error_entries_are_not_re_emitted(self):
        """The state reducer appends error_log; re-emitting the inner entries
        would duplicate every line, and the caller never sees them anyway."""
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))

        assert "error_log" not in result

    def test_gate_violations_travel_in_error_log_only(self):
        result = self.node(_state(record_id="", record_ref="", error_log=[_sentinel()]))

        assert any("output gate" in entry for entry in result["error_log"])
        assert "output gate" not in json.dumps(result["formatted_output"])
