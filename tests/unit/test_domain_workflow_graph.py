# CMN-C2-231 - Unit tests: inner Dynamics365WorkflowGraph (BaseGraph) contract.
# The compiled outer path is
# exercised end-to-end by tests/proof_of_boundary/test_pb_invoke_order.py; this
# module unit-checks the inner graph's identity, config forwarding, routing,
# output contract, and a direct inner invoke on the network-free transport.


from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import Dynamics365WorkflowGraph
from src.schemas.state import State, from_json

_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
_BASE = "https://orgname.crm.dynamics.com/api/data/v9.2"


def _graph(config=None):
    return Dynamics365WorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "dynamics365_sales_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_dynamics_config_as_json():
    g = _graph({"configurable": {"dynamics": {"base_url": _BASE}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict.
    assert isinstance(extra["dynamics_config"], str)
    assert from_json(extra["dynamics_config"], {}) == {"base_url": _BASE}


def test_extra_initial_state_carries_the_request_budget():
    """The declared budget travels with the connector settings, so the no-arg
    API node can read it - a setting that never arrives is dead config."""
    g = _graph({"configurable": {"dynamics": {"base_url": _BASE}, "timeout_s": 12.0}})
    settings = from_json(g._extra_initial_state()["dynamics_config"], {})
    assert settings == {"base_url": _BASE, "timeout_s": 12.0}


def test_extra_initial_state_carries_the_budget_without_a_connector_section():
    g = _graph({"configurable": {"timeout_s": 12.0}})
    settings = from_json(g._extra_initial_state()["dynamics_config"], {})
    assert settings == {"timeout_s": 12.0}


def test_extra_initial_state_empty_without_any_forwarded_settings():
    assert _graph()._extra_initial_state() == {}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": _GUID, "record_ref": f"dynamics://leads/{_GUID}", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_record",
            "entity_type": "lead",
            "record_id": _GUID,
            "record_ref": f"dynamics://leads/{_GUID}",
            "record_name": "Record 3f2504e0",
            "confirmation": "ok",
            "dynamics_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_record"
    assert out["entity_type"] == "lead"
    assert out["record_ref"] == f"dynamics://leads/{_GUID}"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": _GUID, "record_ref": f"dynamics://leads/{_GUID}", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_default_transport():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node declares
    ANONYMOUS): validate -> classify -> infer -> call -> confirm."""
    g = _graph({"configurable": {"dynamics": {"base_url": _BASE}}})
    g.compile()
    result = g.invoke(user_input=f"Look up the lead record {_GUID} and summarize it.")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == _GUID
    assert result["record_ref"] == f"dynamics://leads/{_GUID}"
    assert result["intent"] == "lookup_record"
    assert result["entity_type"] == "lead"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferDynamicsFieldsNode",
        "CallDynamicsApiNode",
        "ConfirmNode",
    ]


def test_caller_parameters_reach_the_inner_pipeline_end_to_end():
    """The subgraph invoke carries only the string extract_input() returns, so
    the validated caller parameters ride inside it: prove they arrive by the
    OUTCOME of a full inner run, not by inspecting a node in isolation."""
    import json

    from src.nodes.pre_process_node import PreProcessNode

    envelope = json.loads(
        PreProcessNode().execute(
            {
                "user_input": "Register the platform rollout",
                "input_context": {
                    "record_id": _GUID,
                    "entity_type": "opportunity",
                    "intent": "update_record",
                    "attributes": {"customer need": "agent platform"},
                },
            }
        )["validated_input"]
    )

    g = _graph({"configurable": {"dynamics": {"base_url": _BASE}, "timeout_s": 30.0}})
    g.compile()
    result = g.invoke(user_input=json.dumps(envelope))

    assert result["status"] == AgentStatus.SUCCESS.value
    # Every caller parameter changed the outcome: entity, operation and body.
    assert result["entity_type"] == "opportunity"
    assert result["intent"] == "update_record"
    assert result["record_id"] == _GUID
    assert result["record_ref"] == f"dynamics://opportunities/{_GUID}"
    assert from_json(result["dynamics_payload"], {})["data"]["customerneed"] == "agent platform"
