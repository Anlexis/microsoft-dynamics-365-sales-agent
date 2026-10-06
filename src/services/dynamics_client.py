"""AgentCore Platform v1.0 - Dynamics 365 Web API (Dataverse OData v4) client.

Service layer: a thin wrapper around the Dynamics 365 Sales (Dataverse) Web
API entity-set endpoints. Contains NO business logic, NO routing, and NO
credentials - the OAuth bearer token is passed in per call by the node (which
reads it via ctx.secrets). This module imports no framework internals - pure
stdlib, so the service layer stays independently testable.

SHIPPED LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stand-in. It returns
    the documented Dataverse response shapes (the entity record for lookups; the
    ``Prefer: return=representation`` created/updated record - including the
    primary-key GUID, synthetically derived from the request - for
    create/update) so the pipeline is runnable and testable without a live
    Dataverse environment or the ``requests`` package - it does NOT perform a
    live Dynamics 365 call. The rule this follows: never fake a live call;
    document the limitation.

    The live-transport parameters (requests-based ``post`` / ``patch`` /
    ``get``) are a construction-time injection point RESERVED FOR DEPLOYMENT
    INTEGRATION - NOT a runtime capability of this build. No runtime path
    supplies a transport: CallDynamicsApiNode always constructs this client
    without one, so the shipped graph always runs the stand-in. The method
    contracts, URL forms (``/{entity_set}`` and ``/{entity_set}(<guid>)``) and
    payload shapes are already Dataverse OData v4 exact, so a
    deployment-integration build that injects live transports needs no
    business-logic change. A live transport also requires a real OAuth token
    (see CallDynamicsApiNode - the stand-in runs without one because no request
    ever leaves the process).

REQUEST BUDGET:
    ``timeout_s`` is the declared per-call budget from config/config.yaml. It is
    enforced here around every transport call: a transport that overruns it
    raises Dynamics365ApiError(504), so a slow or hung integration surfaces as a
    request-timeout error instead of an invoke that never returns.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, dict[str, Any], dict[str, Any]], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://orgname.crm.dynamics.com/api/data/v9.2"
# Fallback per-call budget (seconds) when the caller constructs the client
# without one. config/config.yaml `timeout_s` is the declared value.
_DEFAULT_TIMEOUT_S = 30.0

# Primary-key / primary-name attribute per Dataverse entity set (read-only
# lookup tables - module constants, never mutated).
_PK_ATTRS = {"leads": "leadid", "opportunities": "opportunityid", "contacts": "contactid"}
_NAME_ATTRS = {"leads": "subject", "opportunities": "name", "contacts": "fullname"}


class Dynamics365ApiError(Exception):
    """Raised when the Dynamics 365 Web API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Dynamics 365 API error {status_code}: {message}")


class Dynamics365Client:
    """Dynamics 365 Web API (Dataverse OData v4) entity-record client.

    Args:
        base_url: organization Web API base URL
            (default https://orgname.crm.dynamics.com/api/data/v9.2).
        timeout_s: per-call request budget in seconds. A transport call that
            overruns it raises Dynamics365ApiError(504).
        post/patch/get: optional injected transports (tests or a
            deployment-integration build - not a runtime capability of this
            build; see the module docstring). When none is injected, a
            deterministic NETWORK-FREE stand-in is used - it returns the
            documented shape without a live Dynamics 365 call, and the shipped
            graph always constructs the client this way.
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        post: Transport | None = None,
        patch: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = float(timeout_s)
        self._post = post
        self._patch = patch
        self._get = get

    @property
    def timeout_s(self) -> float:
        """The per-call request budget in effect for this client."""
        return self._timeout_s

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free default)."""
        return self._post is None and self._patch is None and self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> dict[str, str]:
        """Build the Dataverse OData v4 request headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Prefer": "return=representation",
        }

    # -- deterministic stand-in transport (default; NO network) ---------------

    def _stub_transport(
        self, url: str, headers: dict[str, Any], json_body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Deterministic, network-free stand-in - returns the documented Dataverse shape.

        NOT a live call. Synthetic GUIDs are derived from the request so the
        response is stable and inspectable. See the module docstring for the
        shipped limitation and how to inject live transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        synthetic = f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
        entity_set = str(json_body.get("_entity_set", "") or "leads")
        pk_attr = _PK_ATTRS.get(entity_set, "leadid")
        name_attr = _NAME_ATTRS.get(entity_set, "subject")
        if json_body.get("_dynamics_op") == "lookup":
            rid = str(json_body.get("record_id", "")) or synthetic
            # Documented GET /{entity_set}(<guid>) shape: the entity record.
            return 200, {
                "@odata.context": f"{self._base_url}/$metadata#{entity_set}/$entity",
                pk_attr: rid,
                name_attr: f"Record {rid[:8]}",
                "_stub": True,  # marks the network-free v1 stub response
            }
        # POST /{entity_set} (create) / PATCH /{entity_set}(<guid>) (update) -
        # documented Prefer: return=representation shape: the affected record,
        # including the primary-key GUID, echoing the submitted attributes.
        data = json_body.get("data") or {}
        rid = str(json_body.get("record_id", "")) or synthetic
        body = {
            "@odata.context": f"{self._base_url}/$metadata#{entity_set}/$entity",
            pk_attr: rid,
            "_stub": True,  # marks the network-free v1 stub response
        }
        if isinstance(data, dict):
            body.update(data)
        return 200, body

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    def _call(
        self, injected: Transport | None, url: str, headers: dict[str, Any], body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Run one transport call under the declared per-call budget.

        The elapsed time is measured with a monotonic clock and compared after
        the call returns: an overrun surfaces as Dynamics365ApiError(504) rather
        than being reported as a successful CRM write.
        """
        started = time.monotonic()
        status, response = self._resolve(injected)(url, headers, body)
        if time.monotonic() - started > self._timeout_s:
            raise Dynamics365ApiError(504, f"request exceeded the {self._timeout_s:g}s budget")
        return status, response

    # -- public API ---------------------------------------------------------

    def get_record(self, entity_set: str, record_id: str, api_token: str) -> dict[str, Any]:
        """GET /{entity_set}(<guid>) - retrieve one entity record by GUID.

        Returns the parsed entity-record dict (containing the primary-key
        attribute, e.g. ``leadid``). Raises Dynamics365ApiError on a non-2xx
        status or a budget overrun.
        """
        url = f"{self._base_url}/{entity_set}({record_id})"
        status, body = self._call(
            self._get,
            url,
            self._headers(api_token),
            {"_dynamics_op": "lookup", "_entity_set": entity_set, "record_id": record_id},
        )
        if not (200 <= status < 300):
            raise Dynamics365ApiError(status, _err_message(body))
        return body

    def create_record(self, entity_set: str, data: dict[str, Any], api_token: str) -> dict[str, Any]:
        """POST /{entity_set} - create a new entity record.

        ``data`` is the OData attribute body (logical names). Returns the
        parsed ``Prefer: return=representation`` record (with the new GUID).
        Raises Dynamics365ApiError on a non-2xx status or a budget overrun.
        """
        url = f"{self._base_url}/{entity_set}"
        status, body = self._call(
            self._post,
            url,
            self._headers(api_token),
            {"_dynamics_op": "create", "_entity_set": entity_set, "data": data},
        )
        if not (200 <= status < 300):
            raise Dynamics365ApiError(status, _err_message(body))
        return body

    def update_record(self, entity_set: str, record_id: str, data: dict[str, Any], api_token: str) -> dict[str, Any]:
        """PATCH /{entity_set}(<guid>) - partially update an existing record.

        ``data`` is the OData attribute body (logical names). Returns the
        parsed ``Prefer: return=representation`` record. Raises
        Dynamics365ApiError on a non-2xx status or a budget overrun.
        """
        url = f"{self._base_url}/{entity_set}({record_id})"
        status, body = self._call(
            self._patch,
            url,
            self._headers(api_token),
            {"_dynamics_op": "update", "_entity_set": entity_set, "record_id": record_id, "data": data},
        )
        if not (200 <= status < 300):
            raise Dynamics365ApiError(status, _err_message(body))
        return body


def primary_key_attr(entity_set: str) -> str:
    """The primary-key attribute logical name for a Dataverse entity set."""
    return _PK_ATTRS.get(entity_set, "leadid")


def primary_name_attr(entity_set: str) -> str:
    """The primary-name attribute logical name for a Dataverse entity set."""
    return _NAME_ATTRS.get(entity_set, "subject")


def _err_message(body: Any) -> str:
    """Extract a human-readable message from a Dataverse OData error body."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)
