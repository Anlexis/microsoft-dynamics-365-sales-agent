# Design Specification — CMN-C2-231 Dynamics 365 Sales Agent

## Position in the AgentCore architecture

| Property | Value |
|---|---|
| Agent class | `Dynamics365SalesAgent` (`src/graph/graph.py`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Category | Cat 2 (multi-step domain workflow) |
| Compatibility metadata | `base_type: ToolCallingAgent` — classify the request, extract CRM record fields, build a Dynamics 365 Web API (Dataverse OData v4) request, call it, format the confirmation. No retrieval, no autonomous loop. |

The outer graph is the fixed five-node backbone; the domain pipeline is
encapsulated in a `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
(`src/graph/domain_workflow_graph.py`).

**Three-layer separation**

- State: flat TypedDict `State(AgentState)` — no Pydantic (checkpoints serialize
  with msgpack, which Pydantic objects and nested containers do not survive).
- Node: framework inheritance, Template Method — subclasses override
  `execute(self, state) -> dict` only.
- Graph: composition — `register_nodes()` calls `super().register_nodes()`;
  `add_edges()` is not overridden on the outer graph.

## Architecture overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input state | Output state | Trust | Inherits/overrides |
|------|---------------|-------------|--------------|-------|--------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | validate the caller-data contract; sanitize the request text; serialize the accepted values into `validated_input` (JSON envelope) | user_input, input_context | validated_input, record_hint — or `error_code` alone when the request is declined | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner Dynamics 365 workflow subgraph; stand down when the request was already declined | validated_input, error_code | result, intent, entity_type, record_id, record_ref, record_name, confirmation, dynamics_payload | GraphNode (caller context forwarded unchanged) | Dynamics365WorkflowGraphNode (GraphNode) |
| post_process | shape the caller-facing `formatted_output`; run the module-level `_security_gate_output()` scan; render the refusal sentence when the run carries a reason code | inner-result fields, error_code | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

The inner graph inherits `BaseGraph` (fully custom linear topology). The five
pipeline steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------|
| validate_input | 1 ValidateInput | unpack the request envelope; re-check every structured field against the caller contract; deterministic (regex) flag-and-redact of email/token-shaped values across BOTH caller channels | validated_input, record_hint, caller_entity_type, caller_intent, caller_attributes, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | honour an explicitly stated operation; otherwise deterministic keyword classification → lookup_record / create_record / update_record; low confidence → lookup_record (read-only default — never a write) | intent | ANONYMOUS |
| infer_dynamics_fields | 3 InferDynamicsFields | resolve the target entity (lead / opportunity / contact), record identifier, display name and attribute fields; merge the caller's structured attributes over the parsed text; assemble the Web API request per operation; an unresolved identifier is left empty (never invented) | entity_type, record_name, record_id, dynamics_payload | ANONYMOUS |
| call_dynamics_api | 4 CallDynamicsApi | GET /{entity_set}(id) (lookup) / POST /{entity_set} (create) / PATCH /{entity_set}(id) (update) via `Dynamics365Client`, under the declared per-call budget; token via ctx.secrets; 4xx/5xx and budget overruns → status=error | record_id, record_ref, record_name | ANONYMOUS |
| confirm | 5 Confirm | format operation + entity + record identifier + reference into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max_retry) ^
Inner (inside main / Dynamics365WorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_dynamics_fields
              -> call_dynamics_api -> confirm -> END
```

The framework hands the inner graph only the string returned by
`extract_input()` — the outer `input_context` is **not** forwarded into the
subgraph's initial state. The validated caller parameters therefore travel
inside `validated_input` as a JSON envelope
(`{text, record_hint, entity_type, intent, attributes}`), which
`Dynamics365WorkflowGraphNode.extract_input()` hands to the subgraph and
`validate_input` parses back out. Carrying them in a scanned field is
deliberate: the framework's PII mask covers the structured caller fields
exactly as it covers the free text, instead of the structured channel becoming
a way around it.

## Caller-data contract

`POST /invoke` accepts `input_context` alongside the free-text request. Every
field is optional; the pipeline degrades to the free-text path when a field is
absent. A field that is PRESENT but out of contract fails **closed** — the
request is never processed with the offending value dropped or guessed. The
audit entry names the field and never echoes the rejected value; how the
outcome reaches the caller is *Refusing a request* below.

| Field | Contract | Effect |
|---|---|---|
| `record_id` / `record_hint` / `record_guid` | Dataverse record identifier (8-4-4-4-12 hex). Aliases must agree. | Targets the record a lookup or update acts on |
| `entity_type` | `lead` \| `opportunity` \| `contact` | Overrides keyword entity detection |
| `intent` | `lookup_record` \| `create_record` \| `update_record` | Overrides the keyword classifier |
| `attributes` | mapping, ≤ 20 entries; keys `[a-z][a-z0-9 _-]{0,39}` (case-normalised); string values ≤ 200 chars, markup and control characters stripped; numeric values finite and within ±1e12 | Merged into the Dataverse attribute body, winning over a "Key: value" line parsed from the text |
| `channel` | inert identifier `[a-z0-9_]{1,32}` | Audit label only; never rendered into output |

Structural limits: at most 8 `input_context` fields, and the serialized
`input_context` is capped at 256 KB at the HTTP adapter (413 beyond that).

**Numbers are checked for finiteness, not just type.** `NaN` and `Infinity`
parse through `float()` and arrive through ordinary JSON, and every comparison
against `NaN` is False — so an unchecked non-finite value passes a range check
instead of failing it. `finite_in_range()` (`src/services/security.py`) rejects
bools, non-numerics, non-finite values and out-of-range magnitudes, and a
numeric-looking STRING is held to the same contract so `"NaN"` cannot slip
through as prose. The check is applied again in `infer_dynamics_fields` after
the value crosses the graph boundary: a value that arrived out of contract
fails the request rather than being written to the CRM as an unorderable number.
That second check is an invariant, not a caller-facing one — the two nodes ahead
of it already decline such a value, so reaching it means the contract itself was
broken, and it terminates the run (`status = error`) rather than reporting
something for the caller to correct.

## Refusing a request

A refusal never processes the request: nothing is classified, no Dynamics 365
call is made, no product is assembled, and the reason is written to `error_log`
and the audit events. What differs between the two classes of refusal is how the
outcome is reported, and that follows from whether the caller can act on it.

**A reason the caller can correct completes the run.** An empty request, and a
structured field that is out of contract, are values the caller can fix. The run
finishes with `status = success` carrying a reason code in State — `error_code`
is `EMPTY_INPUT` for a missing request and `INVALID_REQUEST` for an
out-of-contract value — and the caller-facing body is the fixed sentence for that
code (`src/services/failure_message.py`). Terminating instead would end the
calling surface's turn and surface an exception type alone, leaving the reason
reachable only from the audit trail; completing lets the caller correct the value
and send the request again on the same conversation.

**A reason the caller cannot correct terminates the run.** An
instruction-override payload is refused outright — re-sending a reworded variant
is not a correction — and so is every outcome the caller has no part in: an
unresolved record identifier, a Dynamics 365 API or transport failure, a missing
secret with a live transport in place, and a broken internal invariant. These
keep `status = error`.

**The reason code stays internal.** `error_code` is a State field and is not
released in the response envelope. The caller is told what to correct by the
sentence in the body, never by a code.

**Everything after the mark stands down.** Once `error_code` is set, the
`GraphNode` skips the inner workflow entirely and each remaining node returns the
mark untouched, so no node acts on input that was already declined and the
Dynamics 365 call is never reached. `post_process` renders the sentence, and
`get_output()` releases no structured record key: a run that did not carry out
the request must not hand back something shaped like a result. The mark crosses
the inner/outer boundary in both directions, and a reason settled on the outer
backbone wins over one the inner graph would otherwise overwrite it with.

## Configuration

`config/agent.yaml` is the **flat** registration manifest: identity, category,
industry, entry-point `class:` path, default trust level, and the compile-time
`requires` block. It carries no runtime parameters.

`config/config.yaml` holds every runtime parameter and is what the registry
loads and passes as `Graph(config=...)`:

| Key | Consumer |
|---|---|
| `max_retry` | the framework backbone's retry budget |
| `timeout_s` | per-call Dynamics 365 request budget, enforced by `Dynamics365Client` |
| `dynamics.base_url` | the organization Web API base URL |

Both deployment paths read the same file. `src/api/server.py` constructs the
agent with `Dynamics365SalesAgent(config=_runtime_config())`, mirroring the
registry, and `Dynamics365WorkflowGraphNode._parent_config()` reads it again to
forward the connector settings and the validated budget to the inner graph
under `config["configurable"]`; the inner graph's `_extra_initial_state()`
injects them into State as the JSON `dynamics_config` field, where the no-arg
`CallDynamicsApiNode` reads them. Nodes take **no constructor arguments**, so
this forwarding chain is the only route configuration can take.

A declared `timeout_s` that is not a finite number inside the supported range
is discarded in favour of the built-in default rather than trusted — a NaN
budget would compare as "never exceeded" and disable the limit it declares.

`requires.secrets` is deliberately empty. The integration token is read with
`ctx.secrets.get("DYNAMICS_TOKEN")` and the agent degrades to its network-free
default transport without it; declaring it would make it a hard compile-time
requirement and refuse to load a deployment that never makes a live call.

## Structured product output (`get_output()` override)

`Dynamics365SalesAgent.get_output()` **extends** `super().get_output()` (never
replaces the base envelope — `output`/`status`/`trace_id`/`correlation_id`/
`node_history` are preserved) and surfaces the structured record keys
(`record_id`, `record_ref`, `entity_type`, `intent`, `confirmation`) **only on
SUCCESS**. It is fail-closed: on any non-SUCCESS status, or when the gated
`formatted_output` carries no record evidence, no structured key is added. A run
that completed carrying a reason code is withheld the same way — the status is
SUCCESS but the request was never carried out, so the record keys it would
otherwise publish do not exist and none is released.

## Security design

- **Trust gate** — the single external trust gate is
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every
  inner domain node — **including the write-capable `CallDynamicsApiNode`** —
  declares `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary
  is enforced exactly once, at `pre_process`. The agent-level default trust is
  declared in `config/agent.yaml`; `src/api/server.py` enforces the standalone
  entry-point Bearer-token boundary (`INVOKE_AUTH_TOKEN` → VERIFIED_EXTERNAL).
- **Instruction-override refusal (template-owned)** — `PreProcessNode` refuses
  instruction-override payloads on both caller channels before any
  classification or CRM call. The platform's input gate refuses such payloads
  too, but a template whose only defence is that gate returns success wherever
  the gate is absent or configured off — so the refusal is the template's own
  guarantee, proven by calling `execute()` directly, and asserted as behaviour
  (error status, nothing carried forward) rather than any gate's wording. This
  is deliberately the terminating class of refusal (*Refusing a request*): a
  rewording is not a correction, so the run does not invite one. The
  pattern set is deliberately narrow: an ordinary sales request that happens to
  contain the same words is unaffected.
- **Input flag-and-redact** — `ValidateInputNode.execute()` runs a
  deterministic (regex, not LLM) scan for email addresses and access-token-like
  strings (`eyJ…`, `secret_…`, `sk-…`) and redacts them before any logging,
  across the free text AND the structured attribute values. A CRM write request
  legitimately names a person or company, so this is flag-and-redact for safe
  logging rather than a hard reject; the framework's own PII mask additionally
  rewrites emails/phones/names in `user_input` / `validated_input`. The
  deterministic auto-rejects are the empty/non-request guard and an
  out-of-contract structured field — both are caller-correctable, so both
  complete the run carrying a reason code rather than terminating it.
- **Secrets** — the integration token is read via
  `ctx.secrets.get("DYNAMICS_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. A missing token is tolerated
  **only** while the network-free default transport is active (no live call is
  made); with a live transport injected, a missing token is a hard
  `status=error`.
- **Output gate** — the domain output gate is the **module-level**
  `_security_gate_output()` in `src/nodes/post_process_node.py`, called from
  `PostProcessNode.execute()`. No node defines `_extra_security_gate_input` /
  `_extra_security_gate_output` instance methods: the framework gate methods
  are `@final` and the SDK auto-wraps the `_extra_` hooks, so domain checks live
  inline or in module-level helpers. The gate enforces two promises over
  **every representation** of the response — nested mappings and lists inside
  `dynamics_payload`, non-string scalars, and the error path's entries, not
  merely the top-level strings:
  1. a SUCCESS response always carries record evidence (record_id/record_ref);
  2. no credential-shaped string reaches the caller, at any depth.

  **What a refusal does.** Returning an error status is not enough to withhold
  anything. The response envelope resolves its payload as `formatted_output or
  result` without consulting the status, and `result` already holds the inner
  workflow's own answer when `post_process` runs — so a gate that only flips the
  status publishes the response it just refused, labelled as an error. A refusal
  therefore clears **every** output-bearing field (`result`, the record keys,
  the confirmation, the assembled request descriptor) and replaces
  `formatted_output` with a non-empty withheld notice; a falsy value there would
  fall through to `result` and re-open the same path. The refusal names the
  **location** of the problem and never quotes what was found: the quoted text
  would travel in the refusal result itself, where the framework credential scan
  raises on it — and the raise replaces the whole cleared result with a generic
  error, restoring the leak. Location labels are themselves held to that rule,
  since the assembled attribute body carries caller-supplied field names.

  **Numeric rendering.** This agent renders no monetary aggregate — the numbers
  in the response are the caller's own attribute values echoed back inside the
  assembled request descriptor, plus Dataverse identifiers. There is therefore
  no rounding grid to enforce, and imposing one would be actively harmful: a
  rounding pass over the response would rewrite record identifiers and
  `dynamics://` references, which are precisely the evidence promise 1 rests on.
  The invariant enforced is the one this agent actually states, applied
  everywhere.
- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its
  SUCCESS path (operation / presence signals only — never request text, CRM
  record content, or credentials). `__call__()` is never overridden. Event
  names, documented for operations:

  | Node | Event |
  |------|-------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_dynamics_fields | `infer_dynamics_fields_complete` |
  | call_dynamics_api | `call_dynamics_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete` |

## Deterministic pipeline — no LLM

Intent classification (`ClassifyIntentNode`) uses a keyword heuristic and field
inference (`InferDynamicsFieldsNode`) uses regex / line-structure extraction, so
the agent runs and tests without a live LLM. **No LLM client is constructed
anywhere** and no system prompt is read, so there is no dead configuration.
LLM-backed synthesis (richer classification, free-text-to-attribute mapping,
natural-language record summaries) would be additive and needs no graph-shape
change.

## Shipped limitation — the Dynamics 365 client

`src/services/dynamics_client.py` ships a **deterministic, network-free
stand-in** as its default transport: it returns the documented Dynamics 365 Web
API (Dataverse OData v4) response shapes — the entity record for lookups; the
`Prefer: return=representation` created/updated record, including the
primary-key identifier synthesised from the request, for create/update — so the
pipeline is runnable and testable without a live Dataverse environment or the
`requests` package. It does **not** perform a live Dynamics 365 call, and the
shipped graph **always** runs it: `CallDynamicsApiNode` constructs the client
without transports, and no runtime path (manifest, state, or
`config["configurable"]`) supplies one. The rule followed here is: never fake a
live call — document the limitation.

The live-transport parameters (`post`/`patch`/`get`) are a **construction-time
injection point reserved for deployment integration**. The method contracts, URL
forms (`/{entity_set}` and `/{entity_set}(<guid>)`) and payload shapes are
already Dataverse OData v4 exact, so a deployment-integration build that injects
live transports needs no business-logic change. `timeout_s` is enforced around
every transport call regardless of which transport is in place, so a slow or
hung integration surfaces as a request-timeout error instead of an invoke that
never returns.

## Framework utilization

### Shared components used
- [x] `InvocationContext` — read in `CallDynamicsApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (including `CallDynamicsApiNode`) declare `TrustLevel.ANONYMOUS`
- [x] Secrets — `ctx.secrets.get("DYNAMICS_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`, under the manifest `namespace`
- [x] `emit_trace_event()` — one positional call per node on the SUCCESS path; framework lifecycle events are not re-emitted

### Composition pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `Dynamics365WorkflowGraph` (`BaseGraph`) via `Dynamics365WorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `_parent_config()` reads `config/config.yaml` and forwards `{dynamics, timeout_s}` under `config["configurable"]`.
- **Error propagation**: `propagate` (default) — inner errors re-raised as `SubgraphError`; per-step `status=error` + `error_log` for API and invariant failures (no silent pass). A caller-correctable refusal is not an inner error: it is settled before the subgraph runs and the subgraph is skipped (*Refusing a request*).

### Import isolation
- [x] The template imports `framework/` and `shared/` only; no platform-internal import anywhere
- [x] `src/services/dynamics_client.py` and `src/services/security.py` carry no framework imports (pure stdlib service layer)

## Design decision record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat single main node | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat; five domain steps live in the inner graph |
| LLM dependency | LLM client in the pipeline | deterministic pipeline | deterministic | runs and tests without a live LLM; no dead prompt/config reads |
| Dynamics client | live `requests` call | injectable transport + documented network-free default | injectable + network-free default | no live network in the shipped build; the injection point is reserved for deployment integration, and going live needs no logic change |
| Caller parameter channel | ContextVar bridge around the framework | JSON envelope inside `validated_input` | JSON envelope | the framework does not forward `input_context` into a subgraph; routing the structured fields through a scanned field keeps the platform's PII mask covering them |
| Malformed caller field | ignore it and continue | fail closed, naming the field | fail closed | silently dropping a field lets a caller believe a constraint was applied when it never was |
| Reporting a caller-correctable refusal | terminate the run | complete the run carrying a reason code | complete | terminating ends the calling surface's turn and shows an exception type only, leaving the reason reachable from the audit trail alone; completing tells the caller what to correct and lets them resend on the same conversation. The refusal itself is unchanged — nothing is processed and no product is released |
| Reason code in the response envelope | publish it alongside the message | keep it in State, publish the sentence only | State only | a code is an internal routing label; what the caller needs is the sentence naming what to correct, and publishing both invites callers to branch on a label the template is then never free to change |
| Node configuration | constructor-argument injection | no-arg nodes + config forwarding via `configurable` → state | no-arg nodes | SDK nodes are no-arg (constructor args raise TypeError at graph build); `config/config.yaml` stays the single runtime source |
| Write target | infer the record identifier freely | caller-supplied or explicit identifier only; unresolved left empty | explicit only | never write to the wrong CRM record; unresolved → status=error, not invented |
| Unknown attributes | send unmapped pairs as-is | map known keys to Dataverse logical names; fold unknown pairs into `description` | map + fold | a live Dataverse call 400s on unknown attribute names |
| Default operation | create_record | lookup_record | lookup_record | low-confidence classification must never default to a write |
| Default entity | reject when unstated | lead (with a non-fatal note) | lead | "log a lead" is the dominant Sales write; the note keeps the fallback auditable, and the explicit-identifier rule still blocks mis-targeted writes |
| Output invariant | monetary rounding grid | the agent's own evidence + credential promises, enforced at every depth | own promises | no monetary aggregate is rendered; a rounding pass would corrupt the record identifiers that are the evidence |
