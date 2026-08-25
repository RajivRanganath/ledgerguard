"""FastAPI backend for the demo dashboard.

One process. It runs the controller once at startup over the frozen holdout,
holds the result in memory, and serves it. There is no database and no write
path -- the dashboard is a window onto a real run, not a separate story.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from ..ai.provider import HeuristicProvider, get_provider
from ..evaluation.benchmark import batch_digest
from ..evaluation.metrics import compute
from ..evidence.safety import AUTO_RESOLVED
from ..ledger.money import to_rupees_str
from ..pipeline import run
from ..synthetic.adversarial import (
    DONOR_PAYMENT_ID,
    VICTIM_PAYMENT_ID,
    build_adversarial_batch,
)
from ..synthetic.fault_injector import build_dataset, split
from ..synthetic.generator import FaultClass

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"

app = FastAPI(title="LedgerGuard", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@lru_cache(maxsize=1)
def _state() -> dict:
    """Run the controller once, on the frozen holdout, and cache the result."""
    full = build_dataset()
    _dev, holdout = split(full)
    lbp = holdout.lifecycle_by_payment()
    batch = holdout.batch()
    provider = get_provider()

    baseline = run(batch, use_ai=False, lifecycle_by_payment=lbp)
    hybrid = run(batch, use_ai=True, provider=provider, lifecycle_by_payment=lbp)

    base_metrics = compute(baseline, holdout)
    hyb_metrics = compute(hybrid, holdout)

    fault_by_payment = holdout.fault_class_by_payment()
    lifecycle_by_pid = holdout.lifecycle_by_payment()

    cases = []
    for outcome in hybrid.outcomes:
        case = outcome.case
        payment = hybrid.ledger.payments_by_id[case.payment_id]
        order = hybrid.ledger.orders_by_id.get(payment.order_id or "")
        cases.append(
            {
                "case_id": outcome.case_id,
                "lifecycle_id": lifecycle_by_pid.get(case.payment_id, ""),
                "fault_class": fault_by_payment.get(case.payment_id),
                "state": outcome.state,
                "exception_type": (
                    case.exception.exception_type.value if case.exception else None
                ),
                "exposure_inr": to_rupees_str(outcome.exposure_paise),
                "records": {
                    "order": (
                        {
                            "order_id": order.order_id,
                            "amount_inr": to_rupees_str(order.amount),
                            "customer_reference": order.customer_reference,
                            "created_at": order.created_at.isoformat(),
                        }
                        if order
                        else None
                    ),
                    "payment": {
                        "payment_id": payment.payment_id,
                        "amount_inr": to_rupees_str(payment.amount),
                        "method": payment.method,
                        "captured_at": (
                            payment.captured_at.isoformat() if payment.captured_at else None
                        ),
                    },
                    "settlement": (
                        {
                            "settlement_id": case.settlement.settlement_id,
                            "gross_inr": to_rupees_str(case.settlement.gross_amount),
                            "fees_inr": to_rupees_str(case.settlement.fees),
                            "tax_inr": to_rupees_str(case.settlement.tax),
                            "refund_adjustments_inr": to_rupees_str(
                                case.settlement.refund_adjustments
                            ),
                            "net_inr": to_rupees_str(case.settlement.net_amount),
                            "settlement_date": case.settlement.settlement_date.isoformat(),
                        }
                        if case.settlement
                        else None
                    ),
                    "candidate_refunds": (
                        outcome.investigation.context.get("candidate_evidence", [])
                        if outcome.investigation
                        else []
                    ),
                },
                "expected": {
                    "gross_inr": to_rupees_str(case.expected.gross_amount),
                    "fees_inr": to_rupees_str(case.expected.fees),
                    "tax_inr": to_rupees_str(case.expected.tax),
                    "refund_adjustments_inr": to_rupees_str(case.expected.refund_adjustments),
                    "net_inr": to_rupees_str(case.expected.net_amount),
                    "window": (
                        f"{case.expected.window_start.date()} .. {case.expected.window_end.date()}"
                        if case.expected.window_start
                        else None
                    ),
                },
                "invariants": [i.as_dict() for i in case.invariants],
                "investigation": (
                    {
                        "hypothesis": outcome.investigation.result.hypothesis,
                        "reason": outcome.investigation.result.reason,
                        "recommended_action": outcome.investigation.result.recommended_action,
                        "required_evidence": [
                            e.model_dump() for e in outcome.investigation.result.required_evidence
                        ],
                        "candidate_evidence_ids": outcome.investigation.result.candidate_evidence_ids,
                        "source": outcome.investigation.result.source,
                        "model_name": outcome.investigation.result.model_name,
                        "latency_ms": round(outcome.investigation.latency_ms, 2),
                    }
                    if outcome.investigation
                    else None
                ),
                "verification": (
                    outcome.verification.as_dict() if outcome.verification else None
                ),
                "decision": (
                    {
                        "state": outcome.decision.state,
                        "reason": outcome.decision.reason,
                        "resolution": outcome.decision.resolution,
                        "missing_evidence": outcome.decision.missing_evidence,
                        "suggested_action": outcome.decision.suggested_action,
                    }
                    if outcome.decision
                    else None
                ),
            }
        )

    # The hand-built adversarial pair is run separately so the demo can show it
    # in isolation, with both halves side by side.
    #
    # It is run twice: once with the configured investigator, and once with the
    # deliberately naive offline stub. A capable model sometimes declines this
    # case on its own, which is a good outcome -- but the gate exists so that
    # the system does not have to depend on that, and the only way to show that
    # honestly is to show both investigators on the same records.
    adv_report = run(build_adversarial_batch(), use_ai=True, provider=provider)
    naive_report = run(
        build_adversarial_batch(), use_ai=True, provider=HeuristicProvider()
    )
    adv = {}
    for outcome in adv_report.outcomes:
        key = "victim" if outcome.case.payment_id == VICTIM_PAYMENT_ID else "donor"
        adv[key] = {
            "case_id": outcome.case_id,
            "customer": adv_report.ledger.orders_by_id[
                outcome.case.order_id
            ].customer_reference,
            "shortfall_inr": to_rupees_str(outcome.exposure_paise),
            "hypothesis": outcome.investigation.result.hypothesis,
            "ai_reason": outcome.investigation.result.reason,
            "recommended_action": outcome.investigation.result.recommended_action,
            "verdict": outcome.verification.verdict,
            "verification_score": outcome.verification.verification_score,
            "checks": [c.as_dict() for c in outcome.verification.checks],
            "state": outcome.decision.state,
            "decision_reason": outcome.decision.reason,
            "suggested_action": outcome.decision.suggested_action,
        }

    naive = {}
    for outcome in naive_report.outcomes:
        key = "victim" if outcome.case.payment_id == VICTIM_PAYMENT_ID else "donor"
        naive[key] = {
            "hypothesis": outcome.investigation.result.hypothesis,
            "recommended_action": outcome.investigation.result.recommended_action,
            "verdict": outcome.verification.verdict,
            "verification_score": outcome.verification.verification_score,
            "checks": [c.as_dict() for c in outcome.verification.checks],
            "state": outcome.decision.state,
            "decision_reason": outcome.decision.reason,
        }
    adv["naive"] = naive
    adv["investigator"] = hyb_metrics.provider

    unresolved = [
        {
            "case_id": c["case_id"],
            "exception_type": c["exception_type"],
            "state": c["state"],
            "exposure_inr": c["exposure_inr"],
            "blocked_because": c["decision"]["reason"],
            "missing_evidence": c["decision"]["missing_evidence"],
            "suggested_action": c["decision"]["suggested_action"],
        }
        for c in cases
        if c["state"] not in ("MATCHED", AUTO_RESOLVED)
    ]

    return {
        "dataset": {
            "seed": full.seed,
            "generator_version": full.generator_version,
            "split": "frozen holdout",
            "lifecycles": len(holdout.lifecycles),
            "batch_sha256": batch_digest(holdout)[:16],
        },
        "provider": hyb_metrics.provider,
        "provider_is_model": hyb_metrics.provider not in ("heuristic_stub", "unavailable", "none"),
        "overview": {
            "records_processed": hyb_metrics.total_cases,
            "value_inr": to_rupees_str(hyb_metrics.total_value_paise),
            "match_rate": hyb_metrics.match_rate,
            "matched": hyb_metrics.matched,
            "exceptions": hyb_metrics.exceptions,
            "auto_resolved": hyb_metrics.auto_resolved,
            "unresolved": len(unresolved),
            "unresolved_value_inr": to_rupees_str(hyb_metrics.unresolved_value_paise),
            "false_auto_resolutions": hyb_metrics.false_auto_resolutions,
            "false_auto_resolved_value_inr": to_rupees_str(
                hyb_metrics.false_auto_resolved_value_paise
            ),
            "throughput_per_second": hyb_metrics.throughput_per_second,
            "investigations": hyb_metrics.investigations,
        },
        "benchmark": {
            "baseline": base_metrics.as_dict(),
            "hybrid": hyb_metrics.as_dict(),
        },
        "cases": cases,
        "adversarial": adv,
        "unresolved": unresolved,
        "demo_cases": _pick_demo_cases(cases),
    }


def _pick_demo_cases(cases: list[dict]) -> list[str]:
    """Five prepared cases that carry the demo story, chosen from real output.

    Each entry gives a preferred state and a fallback. The F3 beat wants a case
    the Evidence Gate actually verified, because the story is "ambiguous, then
    proved" -- but a live model does not resolve the same cases on every run, so
    it falls back to any F3 case rather than dropping the beat entirely.
    """
    wanted = [
        (FaultClass.NONE, "MATCHED"),
        (FaultClass.F4_FEE_TAX_MISMATCH, None),
        (FaultClass.F3_UNLINKED_PARTIAL_REFUND, AUTO_RESOLVED),
        (FaultClass.F6_INCORRECT_LINKAGE, None),
        (FaultClass.F1_MISSING_SETTLEMENT, None),
    ]
    picked: list[str] = []
    for fault_class, preferred in wanted:
        candidates = [c for c in cases if c["fault_class"] == fault_class]
        chosen = next(
            (c for c in candidates if preferred and c["state"] == preferred),
            next(iter(candidates), None),
        )
        if chosen:
            picked.append(chosen["case_id"])
    return picked


@app.get("/api/state")
def api_state() -> dict:
    return _state()


@app.get("/api/cases/{case_id}")
def api_case(case_id: str) -> dict:
    for case in _state()["cases"]:
        if case["case_id"] == case_id:
            return case
    raise HTTPException(status_code=404, detail=f"unknown case {case_id}")


@app.get("/api/health")
def api_health() -> dict:
    state = _state()
    return {
        "status": "ok",
        "provider": state["provider"],
        "provider_is_model": state["provider_is_model"],
        "cases": len(state["cases"]),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    if not FRONTEND.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html not built")
    return FileResponse(FRONTEND)
