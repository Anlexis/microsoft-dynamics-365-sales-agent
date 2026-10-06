# CMN-C2-231 - Unit tests: config/agent.yaml (manifest) + config/config.yaml
# (runtime parameters), and the plumbing that carries the runtime values into
# the graph.
#
# The manifest is FLAT: every registration key sits at root level and the entry
# point is a single dotted `class:` path. Runtime parameters live in the
# separate config/config.yaml, which the registry passes as Graph(config=...) -
# a reader pointed at the manifest would find nothing and every declared value
# would be silently dead.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_ROOT = pathlib.Path(__file__).parents[2]
_MANIFEST_PATH = _ROOT / "config" / "agent.yaml"
_RUNTIME_PATH = _ROOT / "config" / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")

_BASE_URL = "https://orgname.crm.dynamics.com/api/data/v9.2"


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text(encoding="utf-8"))


class TestManifest:
    def test_identity_is_flat(self):
        data = _manifest()
        assert data["id"] == "CMN-C2-231"
        assert data["name"] == "Dynamics365SalesAgent"
        assert data["namespace"] == "cmn"
        assert data["category"] == "Cat 2"
        assert data["industry"] == "CMN"
        assert data["base_type"] == "ToolCallingAgent"
        assert data["enabled"] is True
        # No nested agent: block - the loader reads root level only.
        assert "agent" not in data

    def test_entry_point_is_a_single_dotted_path(self):
        data = _manifest()
        assert data["class"] == "src.graph.graph.Dynamics365SalesAgent"
        assert "module" not in data

    def test_generation_mode_is_deterministic(self):
        assert _manifest()["generation_mode"] == "deterministic"

    def test_security_posture(self):
        data = _manifest()
        assert data["required_trust_level"] == "VERIFIED_EXTERNAL"
        # The integration token is read with ctx.secrets.get() and the agent
        # degrades to the network-free transport without it, so declaring it as
        # a compile-time requirement would refuse deployments that never make a
        # live call.
        assert data["requires"]["secrets"] == []
        assert data["requires"]["extras"] == []

    def test_manifest_carries_no_runtime_parameters(self):
        data = _manifest()
        for key in ("max_retry", "timeout_s", "timeout_seconds", "dynamics", "config"):
            assert key not in data, f"{key} belongs in config/config.yaml"


class TestRuntimeConfig:
    def test_runtime_parameters_present_and_typed(self):
        data = _runtime()
        assert isinstance(data["max_retry"], int)
        assert isinstance(data["timeout_s"], (int, float))
        assert data["dynamics"]["base_url"] == _BASE_URL

    def test_secrets_namespace_matches_the_manifest(self):
        """The standalone entry point provisions secrets under the manifest namespace."""
        server_source = (_ROOT / "src" / "api" / "server.py").read_text(encoding="utf-8")
        assert f'namespace="{_manifest()["namespace"]}"' in server_source


class TestRuntimeConfigReachesTheGraph:
    """The declared values must be LIVE, not merely declared."""

    def test_runtime_config_loader_reads_the_runtime_file(self):
        from src.graph.graph import _runtime_config

        loaded = _runtime_config()
        assert loaded["max_retry"] == _runtime()["max_retry"]
        assert loaded["dynamics"]["base_url"] == _BASE_URL

    def test_outer_graph_receives_max_retry(self):
        from src.graph.graph import Dynamics365SalesAgent, _runtime_config

        agent = Dynamics365SalesAgent(config=_runtime_config())
        assert agent.config["max_retry"] == _runtime()["max_retry"]

    def test_parent_config_forwards_connector_and_budget(self):
        from src.graph.graph import Dynamics365WorkflowGraphNode

        configurable = Dynamics365WorkflowGraphNode()._parent_config()["configurable"]
        assert configurable["dynamics"]["base_url"] == _BASE_URL
        assert configurable["timeout_s"] == float(_runtime()["timeout_s"])

    @pytest.mark.parametrize(
        "declared",
        [None, "30", float("nan"), float("inf"), -1, 0, 10_000, True],
        ids=["absent", "string", "nan", "inf", "negative", "zero", "over-range", "bool"],
    )
    def test_unusable_timeout_falls_back_to_the_built_in_default(self, declared):
        """A budget that is not a finite in-range number must not be trusted:
        a NaN comparison is always False, i.e. "never exceeded"."""
        from src.graph.graph import DEFAULT_TIMEOUT_S, _timeout_seconds

        assert _timeout_seconds({"timeout_s": declared} if declared is not None else {}) == DEFAULT_TIMEOUT_S

    def test_declared_timeout_is_used_when_valid(self):
        from src.graph.graph import _timeout_seconds

        assert _timeout_seconds({"timeout_s": 12}) == 12.0
