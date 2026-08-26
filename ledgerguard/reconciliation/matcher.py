"""Deterministic reconciliation engine.

Ordinary software runs first. Exact identifier matching, exact settlement
arithmetic against the Shadow Ledger, duplicate detection, timing windows and
bank matching all happen here. Anything provable at this layer never reaches
the model.

The matcher produces exactly one case per captured payment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ledger.invariants import InvariantResult, check_all
from ..ledger.models import (
    Batch,
    ExceptionType,
    Payment,
    PaymentStatus,
    ReconciliationException,
    Settlement,
)
from ..ledger.shadow_ledger import ExpectedSettlement, ShadowLedger
from .exceptions import make_exception

MATCHED = "MATCHED"


@dataclass
class CaseResult:
    case_id: str
    payment_id: str
    order_id: str | None
    status: str                                  # MATCHED or an ExceptionType value
    expected: ExpectedSettlement
    settlement: Settlement | None
    invariants: list[InvariantResult] = field(default_factory=list)
    exception: ReconciliationException | None = None

    @property
    def is_exception(self) -> bool:
        return self.status != MATCHED

    def failing_invariants(self) -> list[InvariantResult]:
        return [i for i in self.invariants if not i.holds]


@dataclass
class BatchDuplicates:
    payment_ids: set[str] = field(default_factory=set)
    settlement_ids: set[str] = field(default_factory=set)
    bank_signatures: dict[tuple, list[str]] = field(default_factory=dict)


def find_duplicates(batch: Batch) -> BatchDuplicates:
    """Pre-pass: identifier and content level duplicate detection."""
    dup = BatchDuplicates()
    seen: set[str] = set()
    for p in batch.payments:
        if p.payment_id in seen:
            dup.payment_ids.add(p.payment_id)
        seen.add(p.payment_id)

    seen = set()
    for s in batch.settlements:
        if s.settlement_id in seen:
            dup.settlement_ids.add(s.settlement_id)
        seen.add(s.settlement_id)

    for e in batch.bank_entries:
        sig = (e.reference, e.amount, e.date, e.credit_or_debit)
        dup.bank_signatures.setdefault(sig, []).append(e.bank_entry_id)
    dup.bank_signatures = {k: v for k, v in dup.bank_signatures.items() if len(v) > 1}
    return dup


#: Which invariants each classification actually accounts for. Anything else
#: that failed is a secondary finding.
_EXPLAINED_BY = {
    ExceptionType.FEE_MISMATCH: {
        "I1_settlement_arithmetic_consistent",
        "I2_fees_and_tax_match_schedule",
        "I3_net_matches_shadow_ledger",
    },
    ExceptionType.DUPLICATE_RECORD: {"I6_bank_credit_matches_settlement"},
    ExceptionType.DELAYED_SETTLEMENT: {"I5_settlement_within_window"},
    ExceptionType.UNEXPLAINED_SHORTFALL: {
        "I3_net_matches_shadow_ledger",
        "I4_gross_matches_capture",
    },
    ExceptionType.BANK_MISMATCH: {"I6_bank_credit_matches_settlement"},
}


def _classify(
    invariants: list[InvariantResult],
) -> tuple[ExceptionType | None, InvariantResult | None]:
    """Map failing invariants to a specific exception type, most severe first."""
    failing = {i.name: i for i in invariants if not i.holds}
    if not failing:
        return None, None

    # A fee schedule breach is a proven arithmetic cause; report it as such
    # even though it also shows up as a net difference.
    if "I2_fees_and_tax_match_schedule" in failing:
        return ExceptionType.FEE_MISMATCH, failing["I2_fees_and_tax_match_schedule"]
    if "I1_settlement_arithmetic_consistent" in failing:
        return ExceptionType.FEE_MISMATCH, failing["I1_settlement_arithmetic_consistent"]
    if "I4_gross_matches_capture" in failing:
        return ExceptionType.UNEXPLAINED_SHORTFALL, failing["I4_gross_matches_capture"]
    if "I3_net_matches_shadow_ledger" in failing:
        return ExceptionType.UNEXPLAINED_SHORTFALL, failing["I3_net_matches_shadow_ledger"]
    if "I6_bank_credit_matches_settlement" in failing:
        inv = failing["I6_bank_credit_matches_settlement"]
        if "share reference" in inv.detail:
            return ExceptionType.DUPLICATE_RECORD, inv
        return ExceptionType.BANK_MISMATCH, inv
    if "I5_settlement_within_window" in failing:
        return ExceptionType.DELAYED_SETTLEMENT, failing["I5_settlement_within_window"]
    first = next(iter(failing.values()))
    return ExceptionType.AMBIGUOUS_REFERENCE, first


def reconcile_payment(
    ledger: ShadowLedger,
    payment: Payment,
    duplicates: BatchDuplicates,
    lifecycle_id: str | None = None,
) -> CaseResult:
    case_id = payment.payment_id
    lifecycle_id = lifecycle_id or payment.order_id or payment.payment_id
    expected = ledger.expected_for_payment(payment)
    settlements = ledger.settlements_for(payment.payment_id)
    txn_ids = [payment.payment_id]
    if payment.order_id:
        txn_ids.append(payment.order_id)

    def build(exc_type, expected_value, observed_value, evidence, settlement, invs):
        exc = make_exception(
            case_id=case_id,
            lifecycle_id=lifecycle_id,
            transaction_ids=txn_ids
            + ([settlement.settlement_id] if settlement else []),
            exception_type=exc_type,
            detected_by="deterministic_matcher",
            expected_value=expected_value,
            observed_value=observed_value,
            evidence=evidence,
        )
        return CaseResult(
            case_id=case_id,
            payment_id=payment.payment_id,
            order_id=payment.order_id,
            status=exc_type.value,
            expected=expected,
            settlement=settlement,
            invariants=invs,
            exception=exc,
        )

    # 1. Duplicate ingestion of the payment itself.
    if payment.payment_id in duplicates.payment_ids:
        return build(
            ExceptionType.DUPLICATE_RECORD,
            payment.amount,
            payment.amount * 2,
            {
                "reason": "payment_id appears more than once in the batch",
                "duplicate_of": payment.payment_id,
                "expected_shadow_net": expected.as_dict(),
            },
            settlements[0] if settlements else None,
            [],
        )

    # 2. No settlement at all.
    if not settlements:
        return build(
            ExceptionType.MISSING_SETTLEMENT,
            expected.net_amount,
            0,                       # nothing was settled, so the whole net is exposed
            {
                "reason": "captured payment has no settlement record",
                "expected_shadow_net": expected.as_dict(),
                "captured_at": payment.captured_at.isoformat() if payment.captured_at else None,
            },
            None,
            [],
        )

    # 3. More than one settlement claims the same payment.
    if len(settlements) > 1:
        return build(
            ExceptionType.DUPLICATE_RECORD,
            expected.net_amount,
            sum(s.net_amount for s in settlements),
            {
                "reason": "multiple settlements reference the same payment",
                "settlement_ids": [s.settlement_id for s in settlements],
            },
            settlements[0],
            [],
        )

    settlement = settlements[0]
    bank = ledger.bank_entries_for_settlement(settlement)
    invariants = check_all(payment, expected, settlement, bank)
    exc_type, driver = _classify(invariants)

    if exc_type is None:
        return CaseResult(
            case_id=case_id,
            payment_id=payment.payment_id,
            order_id=payment.order_id,
            status=MATCHED,
            expected=expected,
            settlement=settlement,
            invariants=invariants,
        )

    # Findings beyond the one that determined the classification. They do not
    # necessarily block closure, but they must never be silently dropped just
    # because a different invariant was reported first.
    explained = _EXPLAINED_BY.get(exc_type, set())
    secondary = [i.name for i in invariants if not i.holds and i.name not in explained]

    evidence = {
        "reason": driver.detail if driver else "",
        "failed_invariants": [i.as_dict() for i in invariants if not i.holds],
        "secondary_findings": secondary,
        "expected_shadow_net": expected.as_dict(),
        "observed_settlement": {
            "settlement_id": settlement.settlement_id,
            "gross_amount": settlement.gross_amount,
            "fees": settlement.fees,
            "tax": settlement.tax,
            "refund_adjustments": settlement.refund_adjustments,
            "other_adjustments": settlement.other_adjustments,
            "net_amount": settlement.net_amount,
            "settlement_date": settlement.settlement_date.isoformat(),
        },
        "bank_entries": [
            {"bank_entry_id": e.bank_entry_id, "amount": e.amount, "date": e.date.isoformat()}
            for e in bank
        ],
    }

    if exc_type is ExceptionType.UNEXPLAINED_SHORTFALL:
        shortfall = expected.net_amount - settlement.net_amount
        candidates = ledger.candidate_orphan_refunds(shortfall)
        evidence["shortfall_paise"] = shortfall
        evidence["candidate_orphan_refund_ids"] = [r.refund_id for r in candidates]
        expected_value, observed_value = expected.net_amount, settlement.net_amount
    elif exc_type is ExceptionType.FEE_MISMATCH:
        expected_value = expected.fees + expected.tax
        observed_value = settlement.fees + settlement.tax
        # A proven cause is not the same as a fully explained case. Correct the
        # fee and tax back to schedule and ask whether the settlement now
        # reconciles. Whatever is left over is money the fee variance does not
        # account for, and it must not be closed on the strength of the part
        # that was proved.
        corrected_net = (
            settlement.gross_amount
            - expected.fees
            - expected.tax
            - settlement.refund_adjustments
            + settlement.other_adjustments
        )
        evidence["residual_paise"] = expected.net_amount - corrected_net
    elif exc_type is ExceptionType.DELAYED_SETTLEMENT:
        expected_value, observed_value = expected.net_amount, settlement.net_amount
        evidence["days_late"] = (
            (settlement.settlement_date - expected.window_end).days
            if expected.window_end
            else None
        )
        # "Late" and "we cannot tell when it should have arrived" are different
        # findings. Without a capture timestamp there is no window to be late
        # against, and treating the second as the first would close a case on a
        # check that never actually ran.
        evidence["window_undecidable"] = expected.window_end is None
    else:
        expected_value, observed_value = expected.net_amount, settlement.net_amount

    return build(exc_type, expected_value, observed_value, evidence, settlement, invariants)


def reconcile_batch(
    batch: Batch, lifecycle_by_payment: dict[str, str] | None = None
) -> tuple[list[CaseResult], ShadowLedger]:
    """Reconcile every captured payment in the batch. No AI involved."""
    ledger = ShadowLedger(batch)
    duplicates = find_duplicates(batch)
    lifecycle_by_payment = lifecycle_by_payment or {}

    results: list[CaseResult] = []
    seen: set[str] = set()
    for payment in batch.payments:
        if payment.payment_id in seen:
            continue                     # duplicate rows collapse into one case
        seen.add(payment.payment_id)
        if payment.status != PaymentStatus.CAPTURED:
            continue
        results.append(
            reconcile_payment(
                ledger,
                payment,
                duplicates,
                lifecycle_by_payment.get(payment.payment_id),
            )
        )
    return results, ledger
