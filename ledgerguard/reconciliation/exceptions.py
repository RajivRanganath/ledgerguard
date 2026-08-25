"""Exception construction and the exception taxonomy exposed to the AI."""

from __future__ import annotations

from ..ledger.models import ExceptionType, ReconciliationException, Risk

#: The only hypotheses the AI investigator is permitted to return.
PERMITTED_HYPOTHESES = [
    "unlinked_partial_refund",
    "fee_or_tax_variance",
    "duplicate_deduction",
    "timing_difference",
    "unexplained_deduction",
    "insufficient_evidence",
]

#: Exception types the deterministic engine can already prove the cause of.
#: These never reach the AI investigator.
DETERMINISTICALLY_PROVEN = {
    ExceptionType.MISSING_SETTLEMENT,
    ExceptionType.DUPLICATE_RECORD,
    ExceptionType.FEE_MISMATCH,
    ExceptionType.DELAYED_SETTLEMENT,
}

#: Exception types that are genuinely ambiguous and warrant investigation.
NEEDS_INVESTIGATION = {
    ExceptionType.UNEXPLAINED_SHORTFALL,
    ExceptionType.AMBIGUOUS_REFERENCE,
    ExceptionType.BANK_MISMATCH,
}

_RISK_BY_TYPE = {
    ExceptionType.MISSING_SETTLEMENT: Risk.HIGH,
    ExceptionType.UNEXPLAINED_SHORTFALL: Risk.HIGH,
    ExceptionType.BANK_MISMATCH: Risk.HIGH,
    ExceptionType.DUPLICATE_RECORD: Risk.MEDIUM,
    ExceptionType.FEE_MISMATCH: Risk.MEDIUM,
    ExceptionType.AMBIGUOUS_REFERENCE: Risk.MEDIUM,
    ExceptionType.DELAYED_SETTLEMENT: Risk.LOW,
}


def make_exception(
    *,
    case_id: str,
    lifecycle_id: str,
    transaction_ids: list[str],
    exception_type: ExceptionType,
    detected_by: str,
    expected_value: int | None,
    observed_value: int | None,
    evidence: dict,
) -> ReconciliationException:
    difference = None
    if expected_value is not None and observed_value is not None:
        difference = expected_value - observed_value
    return ReconciliationException(
        exception_id=f"EXC-{case_id}",
        lifecycle_id=lifecycle_id,
        transaction_ids=transaction_ids,
        exception_type=exception_type,
        detected_by=detected_by,
        expected_value=expected_value,
        observed_value=observed_value,
        difference=difference,
        evidence=evidence,
        risk=_RISK_BY_TYPE.get(exception_type, Risk.MEDIUM),
    )
