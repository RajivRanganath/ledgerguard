"""Exception investigator.

Assembles the *minimum* context an investigator needs, calls the provider, and
returns a validated structured result. Every failure mode -- no key, timeout,
provider error, malformed JSON, unknown hypothesis -- degrades to a result that
the safety gate reads as "cannot resolve", never to a crash and never to a
resolution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..ledger.models import ExceptionType
from ..ledger.shadow_ledger import ShadowLedger
from ..reconciliation.exceptions import NEEDS_INVESTIGATION, PERMITTED_HYPOTHESES
from ..reconciliation.matcher import CaseResult
from .provider import InvestigatorProvider
from .schemas import InvestigationResult


@dataclass
class Investigation:
    case_id: str
    context: dict
    result: InvestigationResult
    latency_ms: float
    called_model: bool


def should_investigate(case: CaseResult) -> bool:
    """Only genuinely ambiguous exceptions are worth a model call."""
    if not case.is_exception or case.exception is None:
        return False
    return case.exception.exception_type in NEEDS_INVESTIGATION


def build_context(ledger: ShadowLedger, case: CaseResult) -> dict:
    """The exact payload the investigator sees. No ground truth, ever."""
    exc = case.exception
    payment = ledger.payments_by_id[case.payment_id]
    order = ledger.orders_by_id.get(payment.order_id or "")
    settlement = case.settlement

    difference = exc.difference

    candidates = []
    if exc.exception_type is ExceptionType.UNEXPLAINED_SHORTFALL and difference:
        for r in ledger.candidate_orphan_refunds(difference):
            candidates.append(
                {
                    "record_type": "refund",
                    "refund_id": r.refund_id,
                    "amount": r.amount,
                    "status": r.status.value,
                    "created_at": r.created_at.isoformat(),
                    "reference": r.reference,
                    "payment_id": r.payment_id,
                    "note": "unlinked refund; linkage is NOT established",
                }
            )

    return {
        "exception": {
            "exception_id": exc.exception_id,
            "type": exc.exception_type.value,
            "detected_by": exc.detected_by,
            "risk": exc.risk.value,
            "reason": exc.evidence.get("reason", ""),
        },
        "permitted_hypotheses": PERMITTED_HYPOTHESES,
        "records": {
            "order": (
                {
                    "order_id": order.order_id,
                    "amount": order.amount,
                    "created_at": order.created_at.isoformat(),
                    "customer_reference": order.customer_reference,
                }
                if order
                else None
            ),
            "payment": {
                "payment_id": payment.payment_id,
                "order_id": payment.order_id,
                "amount": payment.amount,
                "status": payment.status.value,
                "captured_at": payment.captured_at.isoformat() if payment.captured_at else None,
                "reference": payment.reference,
            },
            "linked_refunds": [
                {"refund_id": r.refund_id, "amount": r.amount, "payment_id": r.payment_id}
                for r in ledger.valid_refunds_for(payment.payment_id)
            ],
            "settlement": exc.evidence.get("observed_settlement"),
            "bank_entries": exc.evidence.get("bank_entries", []),
        },
        "shadow_ledger_expected": case.expected.as_dict(),
        "discrepancy": {
            "expected_paise": exc.expected_value,
            "observed_paise": exc.observed_value,
            "difference_paise": difference,
            "failed_invariants": [
                i["name"] for i in exc.evidence.get("failed_invariants", [])
            ],
        },
        "candidate_evidence": candidates,
        "instruction": (
            "All monetary values are integer paise and were computed by an independent "
            "shadow ledger. Do not recompute them. Do not invent identifiers. If the "
            "records shown do not prove an explanation, return insufficient_evidence."
        ),
    }


def investigate_case(
    ledger: ShadowLedger, case: CaseResult, provider: InvestigatorProvider
) -> Investigation:
    context = build_context(ledger, case)
    started = time.perf_counter()
    try:
        result = provider.investigate(context)
    except Exception as exc:                      # a provider that raises, not returns
        result = InvestigationResult.unavailable(f"{type(exc).__name__}: {exc}")
    latency_ms = (time.perf_counter() - started) * 1000

    # An id the investigator invented is not evidence. Drop it before the gate
    # ever sees it, and record that we did.
    known_ids = {c["refund_id"] for c in context["candidate_evidence"]}
    known_ids |= {r["refund_id"] for r in context["records"]["linked_refunds"]}
    hallucinated = [i for i in result.candidate_evidence_ids if i not in known_ids]
    if hallucinated:
        result = result.model_copy(
            update={
                "candidate_evidence_ids": [
                    i for i in result.candidate_evidence_ids if i in known_ids
                ],
                "error": f"dropped unknown evidence ids: {hallucinated}",
            }
        )

    return Investigation(
        case_id=case.case_id,
        context=context,
        result=result,
        latency_ms=latency_ms,
        called_model=result.source in ("model", "invalid_response"),
    )
