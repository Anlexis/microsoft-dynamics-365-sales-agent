# PB (containment): what the caller-facing envelope carries after the output
# gate refuses a response, or after the inner workflow fails.
#
# The envelope resolves its payload as `formatted_output or result` and never
# looks at the status; by the time post_process runs, `result` already holds the
# inner workflow's own answer (the GraphNode merged it into the outer state). So
# "the gate returned an error" is not evidence that anything was withheld - the
# refused answer can ship inside an envelope that calls itself an error.
#
# These tests assert the property at the boundary that matters: after a refusal
# the envelope carries no released text, no traceback and no source paths - and
# on every non-success path it carries closed-set labels only, never error_log
# or the gate's own violation entries. The clean-path control is deliberate - it
# fails if the gate ever starts refusing everything, so containment cannot be
# "achieved" by returning nothing at all.
#
# The node is driven through its real call path (so the framework trust, input
# and credential gates all run) and its returned partial state is merged the way
# the graph runtime merges it: node_history and error_log accumulate, every
# other key is replaced.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Dynamics365SalesAgent
from src.nodes.post_process_node import (
    ERROR_REASONS,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    PostProcessNode,
)
from src.schemas.state import to_json

_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
_REF = f"dynamics://leads/{_GUID}"
_CONFIRMATION = f"Retrieved Dynamics 365 record (lead) 'Record 3f2504e0' - ref={_REF} - id={_GUID}"

# Keys the graph runtime accumulates rather than replaces.
_ACCUMULATED = ("node_history", "error_log")


def _bearer() -> str:
    # Built at runtime so no credential-shaped literal is committed.
    return "Bearer " + "a" * 24


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body. The token is
    assembled at runtime so no credential-shaped literal is committed."""
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state_after_a_successful_inner_run(**overrides) -> dict:
    """The outer state as the backbone hands it to post_process.

    `result` is what the workflow node merged back: the inner answer, ungated.
    """
    state = {
        "user_input": "Look up the lead record and summarize it.",
        "status": AgentStatus.SUCCESS.value,
        "result": {"record_id": _GUID, "record_ref": _REF, "confirmation": _CONFIRMATION},
        "record_id": _GUID,
        "record_ref": _REF,
        "record_name": "Record 3f2504e0",
        "entity_type": "lead",
        "intent": "lookup_record",
        "confirmation": _CONFIRMATION,
        "dynamics_payload": to_json({"entity_set": "leads", "record_id": _GUID}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "pb-containment",
        "trace_id": "pb-containment",
        "node_history": ["InitializeNode", "PreProcessNode", "Dynamics365WorkflowGraphNode"],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _state_after_a_failed_inner_run(**overrides) -> dict:
    """The outer state when the inner workflow reported an error: the answer
    the inner graph did produce is still merged into `result`, and error_log
    carries the upstream failure text."""
    return _state_after_a_successful_inner_run(
        status=AgentStatus.ERROR.value,
        error_log=[_sentinel()],
        **overrides,
    )


def _merge(state: dict, partial: dict) -> dict:
    merged = dict(state)
    for key, value in partial.items():
        if key in _ACCUMULATED:
            merged[key] = list(merged.get(key, [])) + list(value)
        else:
            merged[key] = value
    return merged


def _run(state: dict) -> tuple[dict, dict]:
    """Run post_process over `state` and build the envelope the caller receives.

    Returns (merged state, envelope). An errored state is driven through
    execute(): BaseNode.__call__ short-circuits on it and the backbone routes an
    error straight to finalize, so that branch is only reachable from inside.
    """
    node = PostProcessNode()
    errored = state.get("status") == AgentStatus.ERROR.value
    partial = node.execute(state) if errored else node(state)
    merged = _merge(state, partial)
    return merged, Dynamics365SalesAgent().get_output(merged)


def _envelope(state: dict) -> dict:
    return _run(state)[1]


_REFUSED_STATES = [
    pytest.param(
        _state_after_a_successful_inner_run(
            dynamics_payload=to_json({"entity_set": "leads", "record_id": _GUID, "data": {"description": _bearer()}})
        ),
        id="credential-nested-in-request-descriptor",
    ),
    pytest.param(
        _state_after_a_successful_inner_run(record_id="", record_ref=""),
        id="success-without-record-evidence",
    ),
]


class TestRefusedResponseIsContained:
    """A refusal must publish nothing of what it refused."""

    @pytest.mark.parametrize("state", _REFUSED_STATES)
    def test_envelope_carries_none_of_the_refused_response(self, state):
        envelope = _envelope(state)
        rendered = json.dumps(envelope, default=str)

        assert envelope["status"] == AgentStatus.ERROR.value
        # No released text: neither the record evidence nor the confirmation.
        assert _GUID not in rendered
        assert "dynamics://" not in rendered
        assert "Retrieved Dynamics 365 record" not in rendered
        # No credential, no internals.
        assert _bearer() not in rendered
        assert "Traceback" not in rendered
        assert ".py" not in rendered
        # The structured record keys stay closed on a non-success envelope.
        assert "record_id" not in envelope
        assert "record_ref" not in envelope

    @pytest.mark.parametrize("state", _REFUSED_STATES)
    def test_reason_code_holds_the_payload_slot(self, state):
        """The payload must be non-empty.

        The envelope falls through to `result` on any falsy payload, so an empty
        payload would silently restore the answer this refusal exists to
        withhold - the failure mode looks identical to no fix at all.
        """
        envelope = _envelope(state)

        assert envelope["output"], "a falsy payload re-opens the `result` fallback"
        assert envelope["output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_refusal_names_the_location_in_error_log_and_never_in_the_envelope(self):
        """The violation entry names a place, not a value (a quoted match would
        travel in the node result, where the framework credential scan raises
        on it and replaces the whole cleared result). And it stays internal:
        the label is built from response keys, which include caller-supplied
        field names, so it is not a closed set and never enters the envelope."""
        state = _state_after_a_successful_inner_run(
            dynamics_payload=to_json({"entity_set": "leads", "record_id": _GUID, "data": {"description": _bearer()}})
        )
        merged, envelope = _run(state)
        reported = " ".join(merged["error_log"])

        assert _bearer() not in reported
        assert "dynamics_payload.data.description" in reported
        assert "dynamics_payload" not in json.dumps(envelope, default=str)


class TestErrorEnvelopeIsClosedSet:
    """On every non-success path the caller receives closed-set labels only."""

    @pytest.mark.parametrize(
        "state",
        [*_REFUSED_STATES, pytest.param(_state_after_a_failed_inner_run(), id="inner-workflow-error")],
    )
    def test_every_envelope_value_is_a_declared_constant(self, state):
        envelope = _envelope(state)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert set(envelope["output"]) == {"reason"}, envelope["output"]
        assert set(envelope["output"].values()) <= ERROR_REASONS, envelope["output"]

    def test_inner_error_envelope_carries_the_reason_code_only(self):
        """The inner workflow's error_log can carry upstream response text -
        names, identifiers, tokens. None of it, and none of the answer that was
        merged into `result`, reaches the caller."""
        envelope = _envelope(_state_after_a_failed_inner_run())
        rendered = json.dumps(envelope, default=str)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert envelope["output"] == {"reason": _REASON_WORKFLOW_FAILED}
        assert not any(fragment in rendered for fragment in _SENTINEL_FRAGMENTS), rendered
        assert _GUID not in rendered
        assert "Retrieved Dynamics 365 record" not in rendered

    def test_caller_supplied_field_name_never_reaches_the_envelope(self):
        """The assembled attribute body carries caller-supplied field names, and
        the gate's location label is built from them. A refusal that echoed the
        label would publish whatever the caller put in a key."""
        field_name = "A. Tanaka <a.tanaka@" + "example.com>"
        state = _state_after_a_successful_inner_run(
            dynamics_payload=to_json({"entity_set": "leads", "record_id": _GUID, "data": {field_name: _bearer()}})
        )
        envelope = _envelope(state)
        rendered = json.dumps(envelope, default=str)

        assert envelope["output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert "Tanaka" not in rendered
        assert "@example" not in rendered
        assert _bearer() not in rendered


class TestUnrefusedResponseStillShips:
    """Control: containment must not be reached by refusing everything."""

    def test_clean_response_reaches_the_caller_intact(self):
        envelope = _envelope(_state_after_a_successful_inner_run())

        assert envelope["status"] == AgentStatus.SUCCESS.value
        assert envelope["output"]["record_id"] == _GUID
        assert envelope["output"]["record_ref"] == _REF
        assert envelope["output"]["confirmation"] == _CONFIRMATION
        # The structured record keys are surfaced on a gated success.
        assert envelope["record_ref"] == _REF
        assert envelope["intent"] == "lookup_record"

    def test_clean_response_carries_no_reason_code(self):
        envelope = _envelope(_state_after_a_successful_inner_run())
        rendered = json.dumps(envelope, default=str)

        assert "reason" not in envelope["output"]
        assert not any(reason in rendered for reason in ERROR_REASONS)
