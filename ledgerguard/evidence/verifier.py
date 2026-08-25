"""The Evidence Gate.

The investigator proposes. This module proves, or refuses to.

Nothing here consults the model's reason string, its confidence, or its
self-assessment. For each permitted hypothesis there is a fixed battery of
deterministic checks over the actual records. The AI's only influence is
*which record* to check -- it selects a candidate, and the gate decides whether
that selection survives contact with the ledger.

Verdicts:
  VERIFIED   every check passed; the ledger balances once the evidence is applied
  UNVERIFIED evidence is incomplete or absent; nothing was disproved
  REJECTED   a check actively failed; the proposed explanation is wrong

REJECTED is reserved for positive disproof, and linkage failures are the main
source of it: a refund whose amount matches the discrepancy exactly but which
demonstrably belongs to a different customer is not weak evidence, it is
counter-evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from ..ai.schemas import InvestigationResult
from ..ledger.models import Refund, RefundStatus
from ..ledger.shadow_ledger import ShadowLedger
from ..reconciliation.matcher import CaseResult

VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
REJECTED = "REJECTED"

#: How late after settlement a refund may still legitimately be booked into it.
REFUND_BOOKING_TOLERANCE_DAYS = 1

#: Check kinds. A failure of a LINKAGE check is disproof, not just absence.
LINKAGE = "linkage"
EXISTENCE = "existence"
AMOUNT = "amount"
TIMING = "timing"
INVARIANT = "invariant"
UNIQUENESS = "uniqueness"


@dataclass(frozen=True)
class EvidenceCheck:
    name: str
    kind: str
    passed: bool
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "passed": self.passed, "detail": self.detail}


@dataclass
class VerificationOutcome:
    case_id: str
    hypothesis: str
    verdict: str
    checks: list[EvidenceCheck] = field(default_factory=list)
    claimed_evidence_ids: list[str] = field(default_factory=list)
    ai_required_evidence: list[dict] = field(default_factory=list)
    note: str = ""

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total_count(self) -> int:
        return len(self.checks)

    @property
    def verification_score(self) -> str:
        """A transparent count, never a model-generated confidence number."""
        return f"{self.passed_count} of {self.total_count} evidence checks passed"

    def failed(self) -> list[EvidenceCheck]:
        return [c for c in self.checks if not c.passed]

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "hypothesis": self.hypothesis,
            "verdict": self.verdict,
            "verification_score": self.verification_score,
            "checks": [c.as_dict() for c in self.checks],
            "claimed_evidence_ids": self.claimed_evidence_ids,
            "ai_required_evidence": self.ai_required_evidence,
            "note": self.note,
        }


def _normalise(value: str | None) -> str:
    """Strip formatting so a dirty reference can still be compared exactly."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _verify_unlinked_partial_refund(
    ledger: ShadowLedger, case: CaseResult, result: InvestigationResult
) -> tuple[str, list[EvidenceCheck], list[str]]:
    checks: list[EvidenceCheck] = []
    exc = case.exception
    payment = ledger.payments_by_id[case.payment_id]
    order = ledger.orders_by_id.get(payment.order_id or "")
    settlement = case.settlement

    # --- E1 existence -------------------------------------------------------
    ids = result.candidate_evidence_ids
    refunds: list[Refund] = [ledger.refunds_by_id[i] for i in ids if i in ledger.refunds_by_id]
    checks.append(
        EvidenceCheck(
            "E1_evidence_records_exist",
            EXISTENCE,
            passed=bool(refunds) and len(refunds) == len(ids),
            detail=(
                f"proposed refund ids {ids or '[]'}; resolved "
                f"{[r.refund_id for r in refunds]} against the record store"
            ),
        )
    )
    if not refunds:
        return UNVERIFIED, checks, []

    refund = refunds[0]

    # --- E2 status ----------------------------------------------------------
    checks.append(
        EvidenceCheck(
            "E2_refund_is_processed",
            EXISTENCE,
            passed=refund.status == RefundStatus.PROCESSED,
            detail=f"refund {refund.refund_id} status is {refund.status.value}",
        )
    )

    # --- E3 linkage: not already owned by a different payment ---------------
    not_owned = refund.payment_id is None or refund.payment_id == payment.payment_id
    checks.append(
        EvidenceCheck(
            "E3_refund_not_linked_elsewhere",
            LINKAGE,
            passed=not_owned,
            detail=(
                f"refund {refund.refund_id} payment_id is {refund.payment_id!r}; "
                f"this case is payment {payment.payment_id}"
            ),
        )
    )

    # --- E4 linkage: the reference must point at THIS order's customer ------
    # This is the check that separates a real unlinked refund from a refund
    # that merely happens to match on amount. Amount similarity is never
    # accepted as linkage evidence.
    customer = order.customer_reference if order else None
    ref_norm = _normalise(refund.reference)
    cust_norm = _normalise(customer)
    linked = bool(cust_norm) and cust_norm in ref_norm
    checks.append(
        EvidenceCheck(
            "E4_reference_identifies_this_customer",
            LINKAGE,
            passed=linked,
            detail=(
                f"refund reference {refund.reference!r} vs order customer "
                f"{customer!r} -> {'match' if linked else 'NO MATCH'}"
            ),
        )
    )

    # --- E5 amount ----------------------------------------------------------
    shortfall = exc.difference
    checks.append(
        EvidenceCheck(
            "E5_amount_equals_shortfall",
            AMOUNT,
            passed=refund.amount == shortfall,
            detail=f"refund amount {refund.amount} vs shortfall {shortfall}",
        )
    )

    # --- E6 timing ----------------------------------------------------------
    if payment.captured_at is None or settlement is None:
        timing_ok, timing_detail = False, "capture or settlement timestamp unavailable"
    else:
        latest = settlement.settlement_date + timedelta(days=REFUND_BOOKING_TOLERANCE_DAYS)
        timing_ok = payment.captured_at <= refund.created_at <= latest
        timing_detail = (
            f"refund {refund.created_at.date()} must fall between capture "
            f"{payment.captured_at.date()} and settlement+{REFUND_BOOKING_TOLERANCE_DAYS}d "
            f"{latest.date()}"
        )
    checks.append(EvidenceCheck("E6_timing_is_plausible", TIMING, timing_ok, timing_detail))

    # --- E7 invariant restoration ------------------------------------------
    # The decisive test, and one the AI cannot influence: re-run the Shadow
    # Ledger with the proposed refund included and see whether the observed
    # settlement now balances exactly.
    if settlement is None:
        restored, detail = False, "no settlement to reconcile against"
    else:
        rebuilt = ledger.expected_for_payment(payment, extra_refunds=[refund])
        restored = rebuilt.net_amount == settlement.net_amount
        detail = (
            f"shadow ledger net with refund applied = {rebuilt.net_amount}, "
            f"observed settlement net = {settlement.net_amount}"
        )
    checks.append(
        EvidenceCheck("E7_invariant_restored", INVARIANT, restored, detail)
    )

    linkage_failed = any(not c.passed and c.kind == LINKAGE for c in checks)
    if linkage_failed:
        return REJECTED, checks, [refund.refund_id]
    if all(c.passed for c in checks):
        return VERIFIED, checks, [refund.refund_id]
    return UNVERIFIED, checks, [refund.refund_id]


def verify(
    ledger: ShadowLedger, case: CaseResult, result: InvestigationResult
) -> VerificationOutcome:
    """Run the deterministic check battery for the proposed hypothesis."""
    ai_required = [e.model_dump() for e in result.required_evidence]

    if result.source in ("unavailable", "invalid_response"):
        return VerificationOutcome(
            case_id=case.case_id,
            hypothesis=result.hypothesis,
            verdict=UNVERIFIED,
            checks=[],
            ai_required_evidence=ai_required,
            note=f"no usable investigation to verify ({result.error})",
        )

    if result.hypothesis == "unlinked_partial_refund":
        verdict, checks, claimed = _verify_unlinked_partial_refund(ledger, case, result)
        return VerificationOutcome(
            case_id=case.case_id,
            hypothesis=result.hypothesis,
            verdict=verdict,
            checks=checks,
            claimed_evidence_ids=claimed,
            ai_required_evidence=ai_required,
            note=(
                "linkage disproved; amount agreement is not evidence of ownership"
                if verdict == REJECTED
                else ""
            ),
        )

    # Every other permitted hypothesis is either already proven deterministically
    # upstream (so it never reaches here) or has no evidence battery that could
    # establish it from the records available. Abstain rather than invent one.
    return VerificationOutcome(
        case_id=case.case_id,
        hypothesis=result.hypothesis,
        verdict=UNVERIFIED,
        checks=[],
        ai_required_evidence=ai_required,
        note=(
            f"hypothesis {result.hypothesis!r} has no deterministic evidence battery; "
            "the gate will not accept an explanation it cannot re-derive"
        ),
    )


def resolve_evidence_conflicts(
    outcomes: dict[str, VerificationOutcome]
) -> dict[str, VerificationOutcome]:
    """Order-independent uniqueness pass.

    A single refund cannot legitimately explain two different shortfalls. If
    two cases both come back VERIFIED claiming the same record, neither is
    proved -- both are downgraded and sent to a human.
    """
    claims: dict[str, list[str]] = {}
    for case_id, outcome in outcomes.items():
        if outcome.verdict != VERIFIED:
            continue
        for evidence_id in outcome.claimed_evidence_ids:
            claims.setdefault(evidence_id, []).append(case_id)

    contested = {eid: cases for eid, cases in claims.items() if len(cases) > 1}
    for evidence_id, case_ids in contested.items():
        for case_id in case_ids:
            outcome = outcomes[case_id]
            outcome.checks.append(
                EvidenceCheck(
                    "E8_evidence_claimed_once",
                    UNIQUENESS,
                    passed=False,
                    detail=(
                        f"record {evidence_id} is claimed by {len(case_ids)} cases "
                        f"({sorted(case_ids)}); ownership is ambiguous"
                    ),
                )
            )
            outcome.verdict = UNVERIFIED
            outcome.note = "contested evidence: the same record would have to explain two cases"
    return outcomes
