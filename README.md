# Dynamics 365 Sales Operations Agent

AI agent for working Microsoft Dynamics 365 Sales records and pipeline tasks, built with Agentic Star.

> **Category**: Cat 2 (domain-specific pipeline)
> **Industry**: Common
> **Template ID**: CMN-C2-231

## Overview

Turns a plain-language sales note into a structured Microsoft Dynamics 365 Sales operation.
A request such as *"Create a new lead named 'Aiko Tanaka' — Company: Contoso K.K., Topic: agent
platform inquiry"* is classified into one of three record operations (look up / create / update),
resolved to a Dataverse entity (lead, opportunity or contact), mapped field-by-field onto Dataverse
attribute logical names, and issued as a Dynamics 365 Web API (OData v4) request. The caller gets
back the affected record's identifier, a `dynamics://` reference and a human-readable confirmation,
so every write is reviewable.

Two rules shape the pipeline. A record is only ever addressed by an explicit identifier — supplied
by the caller or written verbatim in the request — never guessed, so the agent cannot write to the
wrong record. And a request the classifier is not confident about falls back to the read-only
look-up path, never to a write.

Structured parameters can be supplied alongside the free text (target record, entity, operation and
Dataverse attributes); every field is bounds-checked before it reaches the pipeline and a malformed
one is refused rather than ignored.

Out of the box the Dataverse transport is a deterministic, network-free stand-in that returns the
documented response shapes, so the pipeline runs and tests end-to-end without a Dynamics 365
environment. Pointing it at a real organisation is a transport swap, not a logic change — see
`docs/02_design.md`.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent
fails at graph compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent manifest (agent.yaml) and runtime parameters (config.yaml)
docs/         design and test documentation
```

See `docs/02_design.md` for the design specification and `docs/03_test_spec.md` for the test
specification.

## Customising

1. Point `config/config.yaml` `dynamics.base_url` at your own organisation's Web API endpoint
   and adjust `timeout_s` / `max_retry`.
2. Provide a `DYNAMICS_TOKEN` secret and inject a live transport into `Dynamics365Client`
   (`src/services/dynamics_client.py`) to replace the network-free default.
3. Extend the attribute maps in `src/nodes/infer_dynamics_fields_node.py` with the Dataverse
   logical names your organisation uses.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
