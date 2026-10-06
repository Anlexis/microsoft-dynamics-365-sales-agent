# CMN-C2-231 - Unit tests: Dynamics365Client service (Dataverse OData v4 shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.dynamics_client import (
    Dynamics365ApiError,
    Dynamics365Client,
    primary_key_attr,
    primary_name_attr,
)

_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
_BASE = "https://contoso.crm7.dynamics.com/api/data/v9.2"


def test_get_record_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"leadid": _GUID, "subject": "platform inquiry"}

    client = Dynamics365Client(_BASE + "/", get=get)
    resp = client.get_record("leads", _GUID, "tok123")
    assert resp["leadid"] == _GUID
    # Dataverse OData v4: GET /{entity_set}(<guid>).
    assert captured["url"] == f"{_BASE}/leads({_GUID})"
    # Auth: the per-call token travels as a Bearer header + OData v4 headers.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["OData-Version"] == "4.0"
    assert captured["headers"]["Prefer"] == "return=representation"
    assert captured["body"]["record_id"] == _GUID


def test_create_record_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 201, {"leadid": _GUID, "subject": "platform inquiry"}

    client = Dynamics365Client(_BASE, post=post)
    data = {"subject": "platform inquiry", "companyname": "contoso"}
    resp = client.create_record("leads", data, "tok")
    assert resp["leadid"] == _GUID
    # Dataverse OData v4: POST /{entity_set}.
    assert captured["url"] == f"{_BASE}/leads"
    assert captured["body"]["data"] == data


def test_update_record_success_with_injected_patch():
    captured = {}

    def patch(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"leadid": _GUID, "subject": "revised topic"}

    client = Dynamics365Client(_BASE, patch=patch)
    data = {"subject": "revised topic"}
    resp = client.update_record("leads", _GUID, data, "tok")
    assert resp["leadid"] == _GUID
    # Dataverse OData v4: PATCH /{entity_set}(<guid>).
    assert captured["url"] == f"{_BASE}/leads({_GUID})"
    assert captured["body"]["data"] == data


def test_non_2xx_raises_dynamics_api_error():
    def post(url, headers, body):
        # Documented Dataverse OData error body shape.
        return 400, {"error": {"code": "0x80048d19", "message": "attribute name is invalid"}}

    client = Dynamics365Client(_BASE, post=post)
    with pytest.raises(Dynamics365ApiError) as exc:
        client.create_record("leads", {"bogus_attr": "x"}, "tok")
    assert exc.value.status_code == 400
    assert "attribute name is invalid" in str(exc.value)


def test_default_stub_transport_lookup_shape():
    # No transport injected -> the deterministic, network-free stand-in.
    client = Dynamics365Client()
    assert client.uses_stub_transport is True
    resp = client.get_record("leads", _GUID, "tok")
    assert resp.get("_stub") is True
    assert resp["leadid"] == _GUID
    assert resp["subject"] == f"Record {_GUID[:8]}"


def test_default_stub_transport_create_echoes_attributes():
    client = Dynamics365Client()
    resp = client.create_record("leads", {"subject": "platform inquiry", "companyname": "contoso"}, "tok")
    assert resp.get("_stub") is True
    # Prefer: return=representation shape - the submitted attributes echoed
    # plus a synthetic (deterministic) primary-key GUID.
    assert resp["subject"] == "platform inquiry"
    assert resp["companyname"] == "contoso"
    assert isinstance(resp["leadid"], str) and resp["leadid"]


def test_injected_transport_disables_stub_flag():
    client = Dynamics365Client(get=lambda url, headers, body: (200, {"leadid": _GUID}))
    assert client.uses_stub_transport is False


def test_primary_attr_lookup_tables():
    assert primary_key_attr("leads") == "leadid"
    assert primary_name_attr("leads") == "subject"
    assert primary_key_attr("opportunities") == "opportunityid"
    assert primary_name_attr("opportunities") == "name"
    assert primary_key_attr("contacts") == "contactid"
    assert primary_name_attr("contacts") == "fullname"
    # Unknown entity sets fall back to the lead attributes.
    assert primary_key_attr("unknown") == "leadid"
    assert primary_name_attr("unknown") == "subject"


class TestRequestBudget:
    """timeout_s is a live per-call budget, not a declared-but-unused setting."""

    def test_default_budget_is_exposed(self):
        assert Dynamics365Client().timeout_s == 30.0

    def test_declared_budget_is_used(self):
        assert Dynamics365Client(timeout_s=5).timeout_s == 5.0

    def test_overrunning_transport_raises_a_timeout_error(self, monkeypatch):
        """A slow or hung integration must surface as a request-timeout error,
        never as a successful CRM write. The clock is advanced rather than
        slept on, so the test stays fast and deterministic."""
        ticks = iter([0.0, 9.0])
        monkeypatch.setattr("src.services.dynamics_client.time.monotonic", lambda: next(ticks))
        client = Dynamics365Client(timeout_s=5, get=lambda url, headers, body: (200, {"leadid": _GUID}))
        with pytest.raises(Dynamics365ApiError) as exc:
            client.get_record("leads", _GUID, "tok")
        assert exc.value.status_code == 504
        assert "budget" in str(exc.value)

    def test_transport_inside_the_budget_succeeds(self, monkeypatch):
        ticks = iter([0.0, 1.0])
        monkeypatch.setattr("src.services.dynamics_client.time.monotonic", lambda: next(ticks))
        client = Dynamics365Client(timeout_s=5, get=lambda url, headers, body: (200, {"leadid": _GUID}))
        assert client.get_record("leads", _GUID, "tok")["leadid"] == _GUID

    def test_budget_applies_to_writes_too(self, monkeypatch):
        ticks = iter([0.0, 99.0])
        monkeypatch.setattr("src.services.dynamics_client.time.monotonic", lambda: next(ticks))
        client = Dynamics365Client(timeout_s=5, post=lambda url, headers, body: (200, {"leadid": _GUID}))
        with pytest.raises(Dynamics365ApiError) as exc:
            client.create_record("leads", {"subject": "x"}, "tok")
        assert exc.value.status_code == 504
