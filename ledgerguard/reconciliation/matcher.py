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
    #: Ids that appear more than once carrying *different* content. Sharing an
    #: identifier is not the same finding as being the same record, and the two
    #: must not resolve the same way -- see ``_id_groups``.
    payment_conflicts: set[str] = field(default_factory=set)
    settlement_conflicts: set[str] = field(default_factory=set)


def _payment_signature(p: Payment) -> tuple:
    """Everything about a payment except its identifier."""
    return (
        p.order_id,
        p.amount,
        p.currency,
        p.status,
        p.method,
        p.captured_at,
        p.fee,
        p.tax,
        p.reference,
    )


def _settlement_signature(s: Settlement) -> tuple:
    """Everything about a settlement except its identifier."""
    return (
        s.merchant_id,
        s.gross_amount,
        s.fees,
        s.tax,
        s.refund_adjustments,
        s.other_adjustments,
        s.net_amount,
        s.settlement_date,
        s.status,
        s.reference,
        tuple(s.payment_ids),
    )


def _id_groups(records, key, signature) -> tuple[set[str], set[str]]:
    """Split repeated identifiers into true duplicates and content conflicts.

    A record ingested twice is a duplicate: the identifier and every other
    field agree, so suppressing the second copy is provable. Two records that
    share an identifier but disagree on content are *not* a duplicate -- they
    are two contradictory versions of the same entity, and which one is real
    cannot be proved from the batch. Collapsing them would close a case on a
    content match that never happened.
    """
    by_id: dict[str, list[tuple]] = {}
    for record in records:
        by_id.setdefault(key(record), []).append(signature(record))
    duplicates, conflicts = set(), set()
    for record_id, signatures in by_id.items():
        if len(signatures) < 2:
            continue
        if len(set(signatures)) == 1:
            duplicates.add(record_id)
        else:
            conflicts.add(record_id)
    return duplicates, conflicts


def find_duplicates(batch: Batch) -> BatchDuplicates:
    """Pre-pass: identifier and content level duplicate detection.

    Every record type is compared on content, not only on identifier. Payments
    and settlements are grouped by id and then split by signature; bank entries
    carry no reliable identifier across systems, so they are matched on content
    alone.
    """
    dup = BatchDuplicates()
    dup.payment_ids, dup.payment_conflicts = _id_groups(
        batch.payments, lambda p: p.payment_id, _payment_signature
    )
    dup.settlement_ids, dup.settlement_conflicts = _id_groups(
        batch.settlements, lambda s: s.settlement_id, _settlement_signature
    )

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

    # 1a. The same payment_id ingested twice with *different* content. Not a
    # duplicate: two contradictory versions of one payment, and the batch does
    # not say which is real. Closing it would assert a content match that
    # failed.
    if payment.payment_id in duplicates.payment_conflicts:
        return build(
            ExceptionType.DUPLICATE_RECORD,
            payment.amount,
            0,                       # which row is real is unproven, so all of it is exposed
            {
                "reason": (
                    "payment_id appears more than once with conflicting content; "
                    "the rows are not copies of each other"
                ),
                "content_conflict": "payment",
                "expected_shadow_net": expected.as_dict(),
            },
            settlements[0] if settlements else None,
            [],
        )

    # 1b. Duplicate ingestion of the payment itself: same id, same content.
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
        # Identical copies are a provable re-ingestion. Settlements that differ
        # in content, or that share an id while disagreeing, are contradictory
        # statements about the same money and cannot be collapsed.
        signatures = {_settlement_signature(s) for s in settlements}
        conflicting = len(signatures) > 1 or any(
            s.settlement_id in duplicates.settlement_conflicts for s in settlements
        )
        evidence = {
            "reason": (
                "multiple settlements reference the same payment with conflicting content"
                if conflicting
                else "multiple settlements reference the same payment"
            ),
            "settlement_ids": [s.settlement_id for s in settlements],
        }
        if conflicting:
            evidence["content_conflict"] = "settlement"
        return build(
            ExceptionType.DUPLICATE_RECORD,
            expected.net_amount,
            sum(s.net_amount for s in settlements),
            evidence,
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
