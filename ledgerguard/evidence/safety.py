"""Safety and action gate.

Turns a verification outcome into one of four states. The bias is deliberate:
abstention is a correct answer, and the gate is not scored on how many
exceptions it closes.

"Auto resolved" here means the reconciliation exception is classified and
closed inside LedgerGuard. It never means money moves.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ai.schemas import InvestigationResult
from ..ledger.models import ExceptionStatus, ExceptionType
from ..ledger.money import to_rupees_str
from ..reconciliation.matcher import CaseResult
from .verifier import REJECTED, UNVERIFIED, VERIFIED, VerificationOutcome

AUTO_RESOLVED = ExceptionStatus.AUTO_RESOLVED.value
RECOMMEND_REVIEW = ExceptionStatus.RECOMMEND_REVIEW.value
HUMAN_REVIEW_REQUIRED = ExceptionStatus.HUMAN_REVIEW_REQUIRED.value
UNRESOLVED = ExceptionStatus.UNRESOLVED.value

#: Exception types the deterministic layer proves outright: the cause is known
#: exactly, so the exception can be classified and closed without a human.
_DETERMINISTIC_AUTO_CLOSE = {
    ExceptionType.DUPLICATE_RECORD: "Duplicate ingestion proven by identifier and content match; the duplicate is suppressed, not paid.",
    ExceptionType.FEE_MISMATCH: "Fee and tax reconciled against the independent fee schedule; the exact variance is quantified.",
    ExceptionType.DELAYED_SETTLEMENT: "All amounts reconcile exactly; the only discrepancy is timing against the expected settlement window.",
}

#: Proven, but the resolution is outside the system's authority to close.
_DETERMINISTIC_ESCALATE = {
    ExceptionType.MISSING_SETTLEMENT: "Captured funds have no settlement record. The money is outstanding; LedgerGuard cannot close this, it can only report it.",
}


@dataclass
class Decision:
    case_id: str
    state: str
    reason: str
    resolution: str | None = None
    missing_evidence: list[str] = None            # type: ignore[assignment]
    suggested_action: str = ""

    def __post_init__(self) -> None:
        if self.missing_evidence is None:
            self.missing_evidence = []

    @property
    def is_auto_resolved(self) -> bool:
        return self.state == AUTO_RESOLVED

    @property
    def needs_human(self) -> bool:
        return self.state in (HUMAN_REVIEW_REQUIRED, RECOMMEND_REVIEW, UNRESOLVED)


def decide(
    case: CaseResult,
    investigation: InvestigationResult | None = None,
    verification: VerificationOutcome | None = None,
) -> Decision:
    exc = case.exception
    assert exc is not None, "decide() is only called for exceptions"
    exc_type = exc.exception_type

    if (
        exc_type is ExceptionType.DELAYED_SETTLEMENT
        and exc.evidence.get("window_undecidable")
    ):
        # Amounts reconcile, but the expected settlement window could not be
        # computed at all, so the timing check never ran. Closing this as a
        # routine delay would assert a verification that did not happen.
        return Decision(
            case_id=case.case_id,
            state=HUMAN_REVIEW_REQUIRED,
            reason=(
                "Amounts reconcile exactly, but the payment has no capture "
                "timestamp, so the expected settlement window is undecidable. "
                "The timing check could not be run, and is not assumed to pass."
            ),
            missing_evidence=["payment.captured_at"],
            suggested_action=(
                "Recover the capture timestamp from the provider, then re-run "
                "reconciliation for this payment."
            ),
        )

    residual = exc.evidence.get("residual_paise") or 0
    secondary = exc.evidence.get("secondary_findings") or []

    if exc_type in _DETERMINISTIC_AUTO_CLOSE:
        if residual:
            # Money the proven cause does not account for. Closing here would be
            # closing a case on the strength of the part that was proved while
            # the rest stays unexplained -- the exact failure this system exists
            # to prevent.
            return Decision(
                case_id=case.case_id,
                state=HUMAN_REVIEW_REQUIRED,
                reason=(
                    f"{_DETERMINISTIC_AUTO_CLOSE[exc_type]} Correcting it still leaves "
                    f"{to_rupees_str(abs(residual))} INR unattributed, with no record "
                    f"explaining it. A second, unproven discrepancy is present."
                ),
                missing_evidence=["explanation for the residual deduction"],
                suggested_action=(
                    "Recover the fee variance, and separately identify the "
                    "counterparty of the remaining deduction before closing."
                ),
            )
        reason = _DETERMINISTIC_AUTO_CLOSE[exc_type]
        if secondary:
            reason += f" Also flagged, and individually benign: {', '.join(secondary)}."
        return Decision(
            case_id=case.case_id,
            state=AUTO_RESOLVED,
            reason=reason,
            resolution=exc_type.value,
            suggested_action="No action; classified and closed by the deterministic engine.",
        )

    if exc_type in _DETERMINISTIC_ESCALATE:
        return Decision(
            case_id=case.case_id,
            state=HUMAN_REVIEW_REQUIRED,
            reason=_DETERMINISTIC_ESCALATE[exc_type],
            missing_evidence=["settlement record", "bank credit"],
            suggested_action="Raise a settlement query with the provider for this payment.",
        )

    # ---- ambiguous: an investigation was attempted -------------------------
    if verification is None or investigation is None:
        return Decision(
            case_id=case.case_id,
            state=UNRESOLVED,
            reason=f"No handler exists for exception type {exc_type.value}.",
            suggested_action="Extend the reconciliation taxonomy or review manually.",
        )

    if verification.verdict == REJECTED:
        failed = [c for c in verification.failed() if c.kind == "linkage"]
        return Decision(
            case_id=case.case_id,
            state=HUMAN_REVIEW_REQUIRED,
            reason=(
                f"Evidence Gate rejected the hypothesis {investigation.hypothesis!r}: "
                + "; ".join(c.detail for c in failed)
            ),
            missing_evidence=[c.name for c in verification.failed()],
            suggested_action=(
                "Do not net this against the proposed refund. Identify the true "
                "counterparty of the deduction with the provider before closing."
            ),
        )

    if verification.verdict == VERIFIED:
        if investigation.recommended_action == "resolve":
            reason = (
                f"Hypothesis {investigation.hypothesis!r} verified independently: "
                f"{verification.verification_score}, and the shadow ledger balances "
                f"once the evidence is applied."
            )
            if secondary:
                reason += (
                    f" Separate findings still reported on this case: "
                    f"{', '.join(secondary)}."
                )
            return Decision(
                case_id=case.case_id,
                state=AUTO_RESOLVED,
                reason=reason,
                resolution=investigation.hypothesis,
                suggested_action="No action; evidence-backed classification closed automatically.",
            )
        return Decision(
            case_id=case.case_id,
            state=RECOMMEND_REVIEW,
            reason=(
                f"Evidence verified ({verification.verification_score}) but the "
                f"investigator did not request resolution."
            ),
            resolution=investigation.hypothesis,
            suggested_action="Confirm the classification and close.",
        )

    # UNVERIFIED
    missing = [c.name for c in verification.failed()] or ["no supporting evidence produced"]

    if investigation.hypothesis == "insufficient_evidence":
        # The investigator declined to propose an explanation. That is a
        # permitted, and often correct, answer -- report it as the investigator
        # abstaining, not as the gate failing to verify a hypothesis that was
        # never offered.
        if investigation.source in ("unavailable", "invalid_response"):
            reason = (
                "No investigation was available for this case "
                f"({investigation.error}). Escalated without one."
            )
        else:
            reason = (
                "The investigator found no explanation it could support from the "
                f"available records: {investigation.reason}"
            )
        return Decision(
            case_id=case.case_id,
            state=HUMAN_REVIEW_REQUIRED,
            reason=reason,
            missing_evidence=["no hypothesis was proposed"],
            suggested_action=(
                "Obtain the missing record from the provider, or confirm the "
                "deduction manually before closing."
            ),
        )

    return Decision(
        case_id=case.case_id,
        state=HUMAN_REVIEW_REQUIRED,
        reason=(
            f"Insufficient evidence to prove {investigation.hypothesis!r}: "
            f"{verification.verification_score or 'no checks were possible'}. "
            + (verification.note or "")
        ).strip(),
        missing_evidence=missing,
        suggested_action=(
            "Obtain the missing record from the provider, or confirm the deduction "
            "manually before closing."
        ),
    )
