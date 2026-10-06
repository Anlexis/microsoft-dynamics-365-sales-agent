"""AgentCore Platform v1.0 - CMN-C2-231 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in Dynamics365WorkflowGraphNode
(`main` slot), which wraps the inner Dynamics365WorkflowGraph (validate ->
classify -> infer -> call -> confirm). add_edges() is NOT overridden - backbone
wiring is the framework's concern.

Runtime parameters come from config/config.yaml - the same file the platform
registry loads and passes as Graph(config=...). config/agent.yaml holds
registration identity only and carries no runtime block, so nothing here reads
it: a reader pointed at the manifest would return {} and every declared value
(max_retry, timeout_s, the connector base URL) would be silently dead.
"""

from pathlib import Path
from typing import Any, ClassVar, cast

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State
from src.services.security import finite_in_range

# Runtime-parameter file: src/graph/graph.py -> parents[2] = repo root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Bounds for the declared runtime parameters. A declared value outside its bound
# is discarded in favour of the built-in default rather than trusted: a
# non-finite or absurd timeout would disable the very budget it declares.
_MIN_TIMEOUT_S = 1.0
_MAX_TIMEOUT_S = 600.0
DEFAULT_TIMEOUT_S = 30.0


def _runtime_config() -> dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the same file the platform registry loads and passes as
    Graph(config=...); the standalone server (src/api/server.py) reads it here
    so a registry-loaded agent and a standalone one see identical configuration.
    Returns an empty dict - never raises - when the file is absent, unreadable,
    not valid YAML, or not a mapping (the graph then runs on its defaults).
    """
    try:
        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _timeout_seconds(config: dict[str, Any]) -> float:
    """Validated per-call request budget, or the built-in default.

    A declared value that is not a finite number inside the supported range is
    discarded: a NaN budget compares as "never exceeded" and would disable the
    very limit it declares.
    """
    parsed = finite_in_range(config.get("timeout_s"), _MIN_TIMEOUT_S, _MAX_TIMEOUT_S)
    return DEFAULT_TIMEOUT_S if parsed is None else parsed


class Dynamics365WorkflowGraphNode(GraphNode):
    """Wraps the inner Dynamics 365 workflow graph; assigned to the `main` slot.

    No constructor arguments (SDK v1 nodes are no-arg) - configuration reaches
    the subgraph via _parent_config(), which reads config/config.yaml.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> Any:
        from src.graph.domain_workflow_graph import Dynamics365WorkflowGraph

        return Dynamics365WorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the validated request into validated_input (a
        # JSON envelope); structured caller parameters travel inside it and the
        # first inner node parses them back. The subgraph invoke carries only
        # this string, so the envelope is the outer -> inner hand-off.
        return cast(str, state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        # Map only the keys this node changes back into the outer state.
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "entity_type": sub_result.get("entity_type", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "record_name": sub_result.get("record_name", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "dynamics_payload": sub_result.get("dynamics_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> dict[str, Any]:
        """Forward the runtime parameters to the inner graph under config["configurable"].

        Reads config/config.yaml and forwards the `dynamics:` connector section
        together with the validated per-call request budget - never an empty {},
        which is how a declared setting silently becomes dead configuration.
        """
        runtime = _runtime_config()
        configurable: dict[str, Any] = {"timeout_s": _timeout_seconds(runtime)}
        dynamics = runtime.get("dynamics")
        if isinstance(dynamics, dict):
            configurable["dynamics"] = dynamics
        return {"configurable": configurable}


class Dynamics365SalesAgent(AgentBaseGraph):
    """CMN-C2-231 outer graph - Microsoft Dynamics 365 Sales Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in Dynamics365WorkflowGraphNode (`main` slot); Dynamics
    settings flow from config/config.yaml via _parent_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_231"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = Dynamics365WorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Structured product envelope - EXTENDS super().get_output(), never replaces.

        The base envelope (output/status/trace_id/correlation_id/node_history)
        is preserved; the structured record keys are surfaced ONLY on SUCCESS
        and ONLY when the gated formatted_output carries record evidence
        (fail-closed: an errored or gate-blocked run exposes no structured keys).
        """
        output: dict[str, Any] = super().get_output(state)
        # A run that completed WITHOUT carrying out the request holds the
        # sentence saying what to correct, not a product: none of the
        # structured fields below were produced, so none is released.
        if state.get("error_code"):
            return output
        if state.get("status") != AgentStatus.SUCCESS.value:
            return output  # fail-closed: no structured keys on non-SUCCESS
        formatted = state.get("formatted_output")
        if not isinstance(formatted, dict):
            return output
        if not (formatted.get("record_id") or formatted.get("record_ref")):
            return output  # fail-closed: no record evidence -> no structured keys
        output["record_id"] = formatted.get("record_id", "")
        output["record_ref"] = formatted.get("record_ref", "")
        output["entity_type"] = formatted.get("entity_type", "")
        output["intent"] = formatted.get("intent", "")
        output["confirmation"] = formatted.get("confirmation", "")
        return output
