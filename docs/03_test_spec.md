# Test Specification — CMN-C2-231 Dynamics 365 Sales Agent

## Test strategy

- Test types: Unit (per node + service + config + inner graph) and
  Proof-of-Boundary (full outer-graph invoke, end-to-end `POST /invoke`, import
  isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/`. `tests/integration/` is
  an empty package; end-to-end coverage lives in the boundary suite, which
  drives the real compiled graph.
- The Dynamics 365 call is exercised through the deterministic, network-free
  default transport and through injected fake clients. No live Dataverse call is
  ever made.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` — `BaseNode.__call__` routes the full security pipeline (trust
  gate → PII mask → `execute()` → credential scan) — never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode (the single external
  gate) and `TrustLevel.ANONYMOUS.value` for every other node.

  Three documented exceptions call `execute()` **directly**, each for a reason
  the wrapper would defeat:
  1. `CallDynamicsApiNode.execute(state, config=...)` — a second argument
     `__call__` cannot forward;
  2. the template-owned refusal tests (instruction override, structured-channel
     redaction) — the whole point is that the guarantee holds with no framework
     wrapper in front of it;
  3. `PostProcessNode`'s error branch — `__call__` short-circuits on an
     already-errored state and the backbone route skips post_process on error,
     so the branch is defensive and only reachable directly.

  The trust-rejection test asserts on the RETURNED error dict
  (`status == AgentStatus.ERROR.value`, "trust gate denied" in `error_log`,
  execute-only keys absent) — `__call__` never raises for a trust denial.
- Assertion contract: the invoke surface is `result["output"]` / `status` /
  `trace_id` / `correlation_id` / `node_history` (never `formatted_output` at
  the invoke surface); status is compared to `AgentStatus.SUCCESS`/`.value`
  (lowercase `success`/`error`); the outer graph is called as
  `invoke(user_input=..., ctx=..., input_context=...)`; identifiers may be
  masked (`[MASKED]`) so record evidence is asserted by presence, not raw repr;
  audit spies assert on `call.args[1]` (the event payload), never the
  whole-call repr.
- Refusal contract in assertions: a refusal the caller can correct (empty
  request, out-of-contract structured field) is asserted as `status == success`
  with the request NOT carried out — no `validated_input` at the node, no
  `record_ref` at the invoke surface — plus a populated `error_log`. A refusal
  the caller cannot correct (instruction-override content, an unresolved
  identifier, an API/transport failure, a broken invariant) is asserted as
  `status == error`. The two are never asserted interchangeably: reading a
  terminated run as a completed one, or the reverse, is the defect these
  assertions exist to catch.
- Framework pipeline behaviours the suite encodes: `__call__` short-circuits on
  an incoming errored state (execute() is skipped; error status/error_log pass
  through); the framework's PII mask rewrites Title-Case bigrams (across
  newlines), emails, and digit groups in `user_input`/`validated_input` to
  `[MASKED]` before `execute()` sees the text — positive payloads are PII-free,
  intentional-PII tests assert the `[MASKED]` path. The invoke-order payload
  deliberately carries PII-shaped text (a person name, a phone number), so its
  assertions ride on non-scanned structured output fields only.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS an error dict ("trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | the caller-data contract: identifier aliases and conflicts, entity/operation/attribute/channel bounds, markup and control-character stripping, structural caps, the non-finite amount matrix, and the template-owned instruction-override refusal (via direct `execute()`) | accepted fields reach the request envelope; every out-of-contract field fails closed — the run completes (`status == success`) with no `validated_input`, and `error_log` names the field and never echoes the value; instruction-override payloads terminate the run (`status == error`) with nothing carried forward, while ordinary requests containing the same words pass |
| U-03 | test_validate_input_node.py | empty/short guard; envelope unpacking; re-validation of every structured field across the graph boundary; flag-and-redact of email/token shapes on BOTH caller channels | structured fields carried forward; an empty/short request and an out-of-contract envelope field both complete (`status == success`) carrying nothing forward — no `caller_attributes`, `error_log` populated; token → `[REDACTED]` + `redaction_flags=["token"]`; a structured-channel email is removed by the node's own scan with no framework wrapper in front |
| U-04 | test_classify_intent_node.py | operation = lookup_record / create_record / update_record (keyword, writes-first priority, read-only default) and the caller's explicit operation | correct operation per keyword; caller's explicit operation wins; an out-of-contract caller value never becomes the operation (heuristic decides); no signal → lookup_record with a non-fatal note; empty → error |
| U-05 | test_infer_dynamics_fields_node.py | identifier resolution (text > identifier-shaped hint; never invented); entity resolution and the caller's entity override; quoted name; "Key: value" mapping to Dataverse logical names; caller attributes merged over parsed text; numeric attribute bounds | lookup `{entity_set, record_id}`; create/update `data` with mapped logical names; caller attribute wins over a text line; unknown key folded into `description`; a non-finite or out-of-range amount that reached this node across the graph boundary terminates the run (`status == error`) — the two nodes ahead of it already decline such a value, so arriving here means the contract itself was broken; prose folds into description |
| U-06 | test_call_dynamics_api_node.py | lookup/create/update via the default transport; `dynamics_config` state field + `execute(state, config=...)` override; API error / unresolved identifier / missing entity_set / unknown operation / missing payload; secret posture; request-budget plumbing; error-text hygiene | record_id/record_ref (`dynamics://<entity_set>/<guid>`) on success; 403 surfaces in error_log; live+no-secret → error "unauthenticated"; live+bound secret → token passed to the client; declared budget reaches the client and an unusable one falls back to the default; a transport exception never echoes its own message; a remote error body is length-bounded |
| U-07 | test_confirm_node.py | human-readable confirmation per operation verb; entity + the reference filesidentifier formatting; name fallback | "Retrieved/Created/Updated Dynamics 365 record (entity) … ref=… id=…"; missing evidence → error |
| U-08 | test_post_process_node.py | `formatted_output` shaping (payload round-trip); errored state passes through `__call__` un-masked (short-circuit); the output gate over EVERY representation — nested mappings, lists, non-string scalars, the error path, and excessive depth | success shape with parsed `dynamics_payload`; error status/error_log preserved, no success shape fabricated; SUCCESS without record evidence blocked; a credential-shaped value blocked at any depth; identifiers and references rendered verbatim (no rounding pass); a refusal empties every output-bearing field and replaces `formatted_output` with the non-empty withheld notice, on both the success and the error branch; a credential-shaped location label is withheld rather than quoted; an unrefused response is left untouched |
| U-09 | test_dynamics_client.py | Dataverse OData v4 client: get/create/update record; `Bearer` + OData v4 headers; URL forms `/{entity_set}` and `/{entity_set}(<guid>)`; `Dynamics365ApiError` on non-2xx; default-transport shapes and `uses_stub_transport`; primary key/name tables; the per-call request budget | correct URLs/headers/bodies; 400 raises with the OData `error.message`; deterministic default shapes; an overrunning transport raises a 504 budget error on reads and writes alike |
| U-10 | test_config.py | flat manifest identity/entry point/security posture; runtime parameters in `config/config.yaml`; the secrets namespace matching the manifest; and the plumbing that makes the declared values LIVE | manifest is flat with no nested `agent:` block and no runtime keys; `requires` empty and justified; the runtime loader, the outer graph's `max_retry`, and `_parent_config()`'s connector + budget all read the runtime file; an unusable budget falls back to the built-in default |
| U-11 | test_domain_workflow_graph.py | inner `Dynamics365WorkflowGraph`: identity, `_extra_initial_state()` injection of connector settings and budget, routing short-circuit, output contract, compile, direct inner invoke, and the caller parameters arriving end-to-end | name/state_schema correct; settings forwarded as a JSON string; error → END; a full inner run shows the caller's entity, operation, identifier and attribute body in the outcome |
| U-12 | test_framework_compliance_tc06_tc07.py | TC-06/TC-07: the framework's `@final` input/output gates are not overridable | defining either override raises TypeError at class definition |

## Proof-of-Boundary tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: no platform-internal imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic models, no credential fields |
| PB-6 | Backbone invoke-order + external trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); a VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, Dynamics365WorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation + `intent=create_record` in `result["output"]`; an ANONYMOUS caller is denied at pre_process (error, no post_process, no output); blank input completes (`status=success`) instead of crashing or terminating, so the caller can correct it and send again |
| PB (invoke) | End-to-end through `POST /invoke` | test_invoke_e2e.py | The real compiled agent behind the real ASGI app with Bearer auth: a free-text request produces a real record operation with evidence; the caller's structured parameters change the entity, operation and request body; a wrong token is 401; every malformed caller field and the full non-finite amount matrix complete (`status=success`) with no record operation — `record_ref` absent — so the caller can correct the value on the same conversation; an instruction-override payload terminates the run (`status=error`) with no record evidence and no confirmation; an oversized `input_context` is 413 at the adapter; the response carries no credential-shaped value and renders identifiers verbatim |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Skip stub (canonical form)** — no graph class declares `propagate_hitl=True` and no backbone `interrupt()` checkpoint exists, so there is no propagation behaviour to assert; the stub body is a real AssertionError (never `assert True`), so enabling HITL propagation without implementing PB-7 fails loudly |
| PB (containment) | Caller envelope after an output-gate refusal | test_output_envelope_containment.py | The envelope built from the real graph over a refused response carries no released text (no record id, no `dynamics://` reference, no confirmation), no credential, no traceback and no source paths; the payload slot holds the non-empty withheld notice rather than falling through to `result`; the refusal names the location and never quotes the match. Control: an unrefused response still reaches the caller intact with the structured record keys, so a gate that refused everything cannot pass |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); the agent constructs + compiles via the supported path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite by the emit-spy tests
> (pre_process / validate / classify / call / confirm nodes assert on the event
> payload, `call.args[1]`). PB-3 (a live external service) is not exercised:
> the shipped transport is the documented network-free stand-in.

## Test execution summary

- Runner: the repository's pytest suite against the published framework wheel
  (`agenticstar-agentcore[anthropic]==1.0.1`), the version CI installs.
- Aggregate: 245 collected — 244 passed / 0 failed / 1 skipped
  (the PB-7 skip stub, HITL propagation not enabled).
- Reproduce locally:

  ```bash
  pip install -e ".[dev]"
  python -m pytest tests/ -v
  ```
