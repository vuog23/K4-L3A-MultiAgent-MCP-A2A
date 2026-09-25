# L3A Architecture Record

## 1. System overview

`day09 run` loads one case, discovers the gateway tool inventory, then assigns available read tools to the order/item, payment, and shipment specialists. Each specialist receives only identifiers found in that case. The coordinator collects validated MCP responses, the policy actor classifies the supported outcome, and the verifier creates the schema-shaped result. `TraceWriter` records observable assignments, consumed evidence, handoff, decision, verification, and finalization.

```text
Case input -> Coordinator -> Order/item | Payment | Shipment specialists
                         -> MCP Evidence Gateway -> evidence responses
                         -> Policy decision -> Verifier -> output + trace
```

## 2. Agent ownership

| Actor | Input | Responsibility | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case and discovered tool names | Extract case-scoped identifiers and assign available specialists | Specialist tasks keyed by case ID |
| Order/item | Order and item IDs from case | Read matching order and item records | Validated evidence references and response data |
| Payment | Order IDs from case | Read payment/refund records | Validated payment evidence |
| Shipment | Order IDs from case | Read shipment/tracking records | Validated delivery evidence |
| Policy | Structured case evidence | Select an allowed issue code only where evidence supports it | Decision code for verifier |
| Verifier | Assessment, entities, evidence | Assemble schema-constrained output and ensure references came from the current case's MCP calls | Final output and verification trace |

The coordinator uses discovered tool names and routes domain tools by name. It does not invoke mutation tools. Policy lookup is not attempted without a supported input identifier.

## 3. A2A protocol

The in-process envelope is the case ID plus a task target and decision code; every MCP call separately includes that same `case_id`. A specialist hands off after its finite set of case identifiers is processed. There are no agent retries or recursive handoffs, so failure cannot cause a loop. Trace contains event types, actors, tool names, evidence references, and decision codes, never private reasoning text.

## 4. Evidence lifecycle

The gateway validates each MCP response against `mcp-evidence-response-v1`. The workflow stores its `evidence_ref` exactly as returned, associates it with the current specialist/case, and emits `tool_result_consumed`. Output and claim evidence lists use only those returned references. Evidence is local to a single `solve_case` call; no cross-case cache exists.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout or tool error | No | Continue other specialists; return investigation status if evidence is inadequate | Handoff / `evidence_collection_complete` |
| Not found | No | Treat as unavailable evidence; do not infer absent facts | Verifier / `evidence_refs_case_scoped` |
| Source conflict | No | Preserve neutral investigation status unless structured data resolves it | Policy decision code |
| Invalid specialist result | No | Gateway validation rejects it; continue without that result | No evidence-consumption event |

Calls are read-only; retry is omitted to avoid duplicate audit entries and unbounded work.

## 6. Verification invariants

The caller validates output schema and case ID before writing. Evidence references are copied from validated current-case responses, deduplicated, and bounded by the public maximum. Entity IDs originate in the input. Refund amounts default to zero in the absence of structured refund calculations; confidence is within `[0, 1]`. Actions are unique and limited to the public schema bounds.

## 7. Reproducibility

Runtime: Python 3.11 or newer, dependencies as declared in `pyproject.toml`, one case processed at a time, and no random sampling in workflow decisions. Run with `day09 run`, then `day09 validate`; package with `day09 package`. Credentials are read from `.env` and are never written to output or trace.
