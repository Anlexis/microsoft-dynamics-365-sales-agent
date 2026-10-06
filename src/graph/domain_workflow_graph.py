"""AgentCore Platform v1.0 - inner Dynamics 365 workflow graph (Cat 2 domain workflow).

Instantiated by Dynamics365WorkflowGraphNode.get_subgraph() in graph.py.
Inherits BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_dynamics_fields
          -> call_dynamics_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"], read from config/config.yaml):
    dynamics  - connector section (base_url, ...)
    timeout_s - validated per-call request budget

Both are injected into State as the JSON `dynamics_config` field via
_extra_initial_state(), which is how the no-arg CallDynamicsApiNode reads them.

Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from langgraph.graph import END, START

from typing import Any

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_dynamics_fields_node import InferDynamicsFieldsNode
from src.nodes.call_dynamics_api_node import CallDynamicsApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State, to_json


class Dynamics365WorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "dynamics365_sales_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the dynamics section is optional (the client
        # falls back to the documented default base_url and the network-free
        # default transport), and a missing/unusable setting is handled at
        # CallDynamicsApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_dynamics_fields"] = InferDynamicsFieldsNode()
        self._nodes["call_dynamics_api"] = CallDynamicsApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_dynamics_fields")
        self._sg.add_edge("infer_dynamics_fields", "call_dynamics_api")
        self._sg.add_edge("call_dynamics_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: AgentState) -> str:
        # Required by BaseGraph ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> dict[str, Any]:
        # Forward the connector settings and the validated request budget
        # (arriving under config["configurable"] from _parent_config()) into
        # State as a JSON string - the state contract keeps dict payloads
        # serialization-safe - so the no-arg CallDynamicsApiNode can read them
        # via state.get("dynamics_config").
        configurable = self.config.get("configurable") or {}
        settings = dict(configurable.get("dynamics") or {})
        timeout_s = configurable.get("timeout_s")
        if timeout_s is not None:
            settings["timeout_s"] = timeout_s
        if not settings:
            return {}
        return {"dynamics_config": to_json(settings)}

    def get_output(self, state: AgentState) -> dict[str, Any]:
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "entity_type": state.get("entity_type", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "record_name": state.get("record_name", ""),
            "confirmation": state.get("confirmation", ""),
            "dynamics_payload": state.get("dynamics_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
