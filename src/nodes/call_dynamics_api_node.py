"""AgentCore Platform v1.0 - inner workflow Step 4: CallDynamicsApi (tool side-effect).

Performs the lookup/create/update call against the Dynamics 365 Web API
(Dataverse OData v4) entity-set endpoints via src/services/dynamics_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs (a first-invoke failure on a real deployment). The node
       therefore stays ANONYMOUS.
  Secrets: the integration token is read via ctx.secrets.get("DYNAMICS_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored in
       state. While the deterministic NETWORK-FREE default transport is active a
       missing token is tolerated (a sentinel placeholder is used - it is never
       sent anywhere because no request leaves the process); with a LIVE
       transport injected, a missing token is a hard status=error - a real API
       is never called unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external CRM system; HTTP 4xx/5xx and a request-budget
       overrun surface as status=error + error_log (no silent pass). The log
       line carries the HTTP status only - never the API's own message, which
       is remote content - and a transport failure is reported by class, never
       by its text, which could carry request URLs or headers.

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). Connector settings (base_url) and the per-call request budget
(timeout_s) arrive as the JSON `dynamics_config` state field - injected by the
inner graph's _extra_initial_state() from config/config.yaml, forwarded by
Dynamics365WorkflowGraphNode._parent_config() - or via the optional
`config["configurable"]["dynamics"]` argument for direct invocation. The client
is constructed locally per call (no module-global mutation) and is ALWAYS
constructed without transports, so the deterministic NETWORK-FREE stand-in is
always selected (documented limitation, docs/02_design.md). A live transport is
a construction-time injection point on Dynamics365Client reserved for
deployment integration - NOT a runtime capability of this build; no runtime path
(manifest, state, or config) supplies one.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.security import finite_in_range
from src.services.dynamics_client import (
    Dynamics365ApiError,
    Dynamics365Client,
    primary_key_attr,
    primary_name_attr,
)

_SECRET_KEY = "DYNAMICS_TOKEN"
# Placeholder handed to the network-free stand-in transport when no secret is
# provisioned. Never sent over any network (the stand-in performs no I/O) and
# never written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"
# Fallback per-call request budget when the state carries no validated value.
_DEFAULT_TIMEOUT_S = 30.0
_MIN_TIMEOUT_S = 1.0
_MAX_TIMEOUT_S = 600.0


class CallDynamicsApiNode(FunctionNode):
    """Look up / create / update a Dynamics 365 Sales record via the Web API."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        payload = from_json(state.get("dynamics_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallDynamicsApiNode: missing dynamics_payload"],
            }

        intent = state.get("intent", "lookup_record") or "lookup_record"

        # Settings: manifest section from state (graph-injected), overridable via
        # an explicit config["configurable"]["dynamics"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("dynamics_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("dynamics") or {}
        settings.update(override)

        # The declared per-call budget travels with the connector settings; a
        # value that is not a finite number in the supported range is discarded
        # in favour of the built-in default rather than trusted.
        timeout_s = finite_in_range(settings.get("timeout_s"), _MIN_TIMEOUT_S, _MAX_TIMEOUT_S)
        if timeout_s is None:
            timeout_s = _DEFAULT_TIMEOUT_S

        # Client built locally per call, ALWAYS without transports: the
        # deterministic NETWORK-FREE stand-in is always selected (documented
        # limitation, docs/02_design.md). A live transport is a
        # construction-time injection point reserved for deployment
        # integration - no runtime path supplies one.
        base_url = str(settings.get("base_url", "") or "").strip()
        client = (
            Dynamics365Client(base_url=base_url, timeout_s=timeout_s)
            if base_url
            else Dynamics365Client(timeout_s=timeout_s)
        )

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Documented stand-in limitation: no request leaves the process,
                # so run with a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallDynamicsApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        entity_set = str(payload.get("entity_set", "") or "")
        if not entity_set:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallDynamicsApiNode: dynamics_payload missing entity_set"],
            }
        record_id = state.get("record_id", "") or str(payload.get("record_id", "") or "")
        pk_attr = primary_key_attr(entity_set)
        name_attr = primary_name_attr(entity_set)

        try:
            if intent == "lookup_record":
                if not record_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallDynamicsApiNode: unresolved record GUID - cannot look up record"],
                    }
                resp = client.get_record(entity_set, record_id, api_token) or {}
                record_id = str(resp.get(pk_attr, "")) or record_id
                record_name = state.get("record_name", "") or str(resp.get(name_attr, ""))
            elif intent in ("create_record", "update_record"):
                if intent == "update_record" and not record_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallDynamicsApiNode: unresolved record GUID - cannot update record"],
                    }
                data = payload.get("data") or {}
                if intent == "create_record":
                    resp = client.create_record(entity_set, data, api_token) or {}
                else:
                    resp = client.update_record(entity_set, record_id, data, api_token) or {}
                record_id = str(resp.get(pk_attr, "")) or record_id
                record_name = state.get("record_name", "") or str(resp.get(name_attr, ""))
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallDynamicsApiNode: unknown intent '{intent}'"],
                }
        except Dynamics365ApiError as exc:
            # The HTTP status is the closed-set part of the failure and is all
            # that is logged. The API's message is remote content - an upstream
            # body can carry identifiers, names or echoed request headers - and
            # capping or stripping it would still leave arbitrary text in the
            # log line, so it is not echoed at all.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallDynamicsApiNode: Dynamics 365 API error {exc.status_code}"],
            }
        except Exception:  # transport failure - no silent pass
            # The transport's own message can carry the request URL, headers or
            # a bearer token, so the failure CLASS is reported, never its text.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallDynamicsApiNode: Dynamics 365 call failed (transport error)"],
            }

        record_ref = f"dynamics://{entity_set}/{record_id}" if record_id else ""

        # Audit the tool side-effect - intent + presence signals only, never
        # CRM record content or credentials.
        emit_trace_event(
            "call_dynamics_api_complete",
            {
                "intent": intent,
                "entity_type": state.get("entity_type", ""),
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "record_name": record_name,
            "status": AgentStatus.SUCCESS.value,
        }
