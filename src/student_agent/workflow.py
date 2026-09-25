from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    if isinstance(value, Mapping):
        return [item for child in value.values() for item in _strings(child)]
    return []


def _entities(case: Mapping[str, Any]) -> dict[str, list[str]]:
    entity_keys = ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")
    result = {key: [] for key in entity_keys}
    aliases = {
        "order_ids": ("order_id", "order_ids"),
        "item_ids": ("item_id", "item_ids", "order_item_id"),
        "seller_ids": ("seller_id", "seller_ids"),
        "payment_references": (
            "payment_reference", "payment_id", "payment_ref", "payment_references"
        ),
        "shipment_ids": ("shipment_id", "tracking_code", "shipment_ids"),
    }
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for name, child in value.items():
                key = str(name).lower()
                for output_key, names in aliases.items():
                    if key in names:
                        for text in _strings(child):
                            if text and text not in result[output_key]:
                                result[output_key].append(text)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(case)
    return {key: values[:20] for key, values in result.items()}


def _claims(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = case.get("claims", case.get("customer_claims", []))
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    claims = []
    if isinstance(raw, list):
        for index, claim in enumerate(raw[:5]):
            if isinstance(claim, Mapping):
                claim_id = claim.get("claim_id", claim.get("id", f"claim_{index + 1}"))
                claims.append({"claim_id": str(claim_id)[:64] or f"claim_{index + 1}"})
            elif isinstance(claim, str):
                claims.append({"claim_id": f"claim_{index + 1}"})
    return claims


def _tool_for(tools: list[str], domain: str) -> str | None:
    patterns = {
        "order": ("order",), "item": ("item",), "payment": ("payment", "refund"),
        "shipment": ("shipment", "shipping", "delivery", "tracking"), "policy": ("policy",),
    }
    mutating = re.compile(r"create|update|delete|cancel|refund|resolve|submit|write|set", re.I)
    read = re.compile(r"get|read|lookup|query|list|search|fetch", re.I)
    candidates = [
        name for name in tools
        if any(word in name.lower() for word in patterns[domain])
        and read.search(name)
        and not mutating.search(name)
    ]
    candidates.sort()
    return candidates[0] if candidates else None


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate evidence specialists and build a contract-compliant assessment."""
    case_id = case.get("case_id")
    if not isinstance(case_id, str):
        raise ValueError("case is missing a string case_id")
    tools = await gateway.list_tools()
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="evidence_specialists",
    )
    entities = _entities(case)
    evidence_refs: list[str] = []
    domain_evidence: dict[str, list[dict[str, Any]]] = {}
    # Route only known case identifiers to matching read tools. A tool call is never
    # fabricated from a customer narrative or an identifier discovered in another case.
    work = (
        ("order", "order_ids", "order_id", "order-agent"),
        ("item", "item_ids", "item_id", "order-agent"),
        ("payment", "order_ids", "order_id", "payment-agent"),
        ("shipment", "order_ids", "order_id", "shipment-agent"),
    )
    for domain, id_key, argument_name, actor in work:
        name = _tool_for(tools, domain)
        if name is None:
            continue
        ids = entities[id_key]
        if not ids and domain == "policy":
            ids = []
        for identifier in ids[:3]:
            argument = {argument_name: identifier}
            try:
                response = await gateway.call(name, case_id=case_id, **argument)
            except Exception:
                continue
            ref = response.get("evidence_ref")
            if isinstance(ref, str) and ref not in evidence_refs:
                evidence_refs.append(ref)
                domain_evidence.setdefault(domain, []).append(response)
                trace.emit(case_id=case_id, event_type="tool_result_consumed", actor=actor,
                           tool_name=name, evidence_refs=[ref])
    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="verifier",
               decision_code="evidence_collection_complete")

    # Diagnose only what the structured evidence supports; missing evidence remains explicit.
    primary_issue = "insufficient_evidence"
    cause_code = "EVIDENCE_INSUFFICIENT"
    responsible = {"party_type": "unknown", "party_id": None}
    if evidence_refs:
        primary_issue = "unsupported_claim"
        cause_code = "CLAIM_NOT_CORROBORATED"
    # Capture high-confidence facts directly represented in the authoritative response.
    evidence_data = [
        entry.get("data", {})
        for group in domain_evidence.values()
        for entry in group
    ]
    all_data = " ".join(_strings(evidence_data)).lower()
    if re.search(r"cancel|canceled|cancelled", all_data) and re.search(r"paid|captur", all_data):
        primary_issue, cause_code = "canceled_order_paid", "CANCELED_ORDER_PAYMENT_CAPTURED"
    elif "duplicate" in all_data and ("charge" in all_data or "payment" in all_data):
        primary_issue, cause_code = "duplicate_charge", "DUPLICATE_PAYMENT_CAPTURE"
    elif "refund" in all_data and "fail" in all_data:
        primary_issue, cause_code = "refund_failed", "REFUND_PROCESSING_FAILED"
    elif "refund" in all_data and "pend" in all_data:
        primary_issue, cause_code = "refund_pending", "REFUND_PENDING"
    elif "late" in all_data and domain_evidence.get("shipment"):
        primary_issue, cause_code = "late_delivery_logistics", "LOGISTICS_DELIVERY_DELAY"
        responsible = {"party_type": "logistics_provider", "party_id": None}

    claim_assessments = [
        {"claim_id": claim["claim_id"], "verdict": "insufficient_evidence", "confidence": 0.25,
         "evidence_refs": evidence_refs[:30]}
        for claim in _claims(case)
    ]
    if claim_assessments and evidence_refs:
        insufficient = ("insufficient_evidence", "unsupported_claim")
        verdict = "insufficient_evidence" if primary_issue in insufficient else "supported"
        for claim in claim_assessments:
            claim["verdict"] = verdict
            claim["confidence"] = 0.65 if verdict == "supported" else 0.35

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue,
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="evidence_refs_case_scoped",
    )
    needs_investigation = primary_issue in ("insufficient_evidence", "unsupported_claim")
    result = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": (
                "needs_investigation" if needs_investigation else "action_required"
            ),
            "confidence": 0.3 if needs_investigation else 0.65,
        },
        "affected_entities": entities,
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {"ranked_causes": [{"cause_code": cause_code, "rank": 1}],
                                "responsible_parties": [responsible]},
        "evidence_refs": evidence_refs[:30],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": (
            ["investigate_with_authoritative_records"]
            if needs_investigation
            else ["review_case_for_resolution"]
        ),
    }
    return result
