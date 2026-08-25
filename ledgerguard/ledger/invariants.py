"""Explicit Shadow Ledger invariants.

Each invariant is a named, independently testable predicate over a
(payment, expected settlement, observed settlement) triple. Nothing here is
heuristic and nothing here consults the AI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .models import BankEntry, CreditDebit, Payment, Settlement
from .money import gst_on_fee, platform_fee
from .shadow_ledger import BANK_CREDIT_TOLERANCE_DAYS, ExpectedSettlement


@dataclass(frozen=True)
class InvariantResult:
    name: str
    holds: bool
    expected: int | str | None
    observed: int | str | None
    detail: str

    @property
    def difference(self) -> int | None:
        if isinstance(self.expected, int) and isinstance(self.observed, int):
            return self.expected - self.observed
        return None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "holds": self.holds,
            "expected": self.expected,
            "observed": self.observed,
            "difference": self.difference,
            "detail": self.detail,
        }


# I1 -------------------------------------------------------------------------
def settlement_arithmetic_consistent(s: Settlement) -> InvariantResult:
    """gross - fees - tax - refund_adjustments + other_adjustments == net."""
    derived = (
        s.gross_amount - s.fees - s.tax - s.refund_adjustments + s.other_adjustments
    )
    return InvariantResult(
        name="I1_settlement_arithmetic_consistent",
        holds=derived == s.net_amount,
        expected=derived,
        observed=s.net_amount,
        detail=(
            f"{s.settlement_id}: gross {s.gross_amount} - fees {s.fees} - tax {s.tax} "
            f"- refunds {s.refund_adjustments} + adj {s.other_adjustments} "
            f"= {derived}, reported net {s.net_amount}"
        ),
    )


# I2 -------------------------------------------------------------------------
def fees_and_tax_match_schedule(s: Settlement) -> InvariantResult:
    """Reported fees and tax equal the independently computed fee schedule."""
    exp_fee = platform_fee(s.gross_amount)
    exp_tax = gst_on_fee(exp_fee)
    holds = s.fees == exp_fee and s.tax == exp_tax
    return InvariantResult(
        name="I2_fees_and_tax_match_schedule",
        holds=holds,
        expected=exp_fee + exp_tax,
        observed=s.fees + s.tax,
        detail=(
            f"{s.settlement_id}: expected fee {exp_fee} + tax {exp_tax}, "
            f"observed fee {s.fees} + tax {s.tax}"
        ),
    )


# I3 -------------------------------------------------------------------------
def net_matches_shadow_ledger(
    expected: ExpectedSettlement, s: Settlement
) -> InvariantResult:
    """Observed settlement net equals the Shadow Ledger's reconstructed net."""
    return InvariantResult(
        name="I3_net_matches_shadow_ledger",
        holds=expected.net_amount == s.net_amount,
        expected=expected.net_amount,
        observed=s.net_amount,
        detail=(
            f"{s.settlement_id}: shadow ledger expects net {expected.net_amount} "
            f"for payment {expected.payment_id}, provider reported {s.net_amount}"
        ),
    )


# I4 -------------------------------------------------------------------------
def gross_matches_capture(expected: ExpectedSettlement, s: Settlement) -> InvariantResult:
    """Settlement gross equals the captured payment amount."""
    return InvariantResult(
        name="I4_gross_matches_capture",
        holds=expected.gross_amount == s.gross_amount,
        expected=expected.gross_amount,
        observed=s.gross_amount,
        detail=(
            f"{s.settlement_id}: captured {expected.gross_amount}, "
            f"settlement gross {s.gross_amount}"
        ),
    )


# I5 -------------------------------------------------------------------------
def settlement_within_window(
    expected: ExpectedSettlement, s: Settlement
) -> InvariantResult:
    """Settlement date falls inside the documented T+2 (+/-1d) window."""
    if expected.window_start is None or expected.window_end is None:
        return InvariantResult(
            name="I5_settlement_within_window",
            holds=False,
            expected="capture timestamp required",
            observed=s.settlement_date.isoformat(),
            detail=f"{s.settlement_id}: payment has no captured_at, window undecidable",
        )
    holds = expected.window_start <= s.settlement_date <= expected.window_end
    return InvariantResult(
        name="I5_settlement_within_window",
        holds=holds,
        expected=f"{expected.window_start.date()}..{expected.window_end.date()}",
        observed=str(s.settlement_date.date()),
        detail=(
            f"{s.settlement_id}: settled {s.settlement_date.date()}, expected between "
            f"{expected.window_start.date()} and {expected.window_end.date()}"
        ),
    )


# I6 -------------------------------------------------------------------------
def bank_credit_matches_settlement(
    s: Settlement, entries: list[BankEntry]
) -> InvariantResult:
    """A single bank credit equals the settlement net, within the float window."""
    credits = [e for e in entries if e.credit_or_debit == CreditDebit.CREDIT]
    total = sum(e.amount for e in credits)
    if not credits:
        return InvariantResult(
            name="I6_bank_credit_matches_settlement",
            holds=False,
            expected=s.net_amount,
            observed=None,
            detail=f"{s.settlement_id}: no bank credit found for reference {s.reference}",
        )
    if len(credits) > 1:
        return InvariantResult(
            name="I6_bank_credit_matches_settlement",
            holds=False,
            expected=s.net_amount,
            observed=total,
            detail=(
                f"{s.settlement_id}: {len(credits)} bank credits share reference "
                f"{s.reference} ({[e.bank_entry_id for e in credits]})"
            ),
        )
    entry = credits[0]
    within = abs((entry.date - s.settlement_date).days) <= BANK_CREDIT_TOLERANCE_DAYS
    holds = entry.amount == s.net_amount and within
    return InvariantResult(
        name="I6_bank_credit_matches_settlement",
        holds=holds,
        expected=s.net_amount,
        observed=entry.amount,
        detail=(
            f"{s.settlement_id}: bank credit {entry.bank_entry_id} of {entry.amount} "
            f"on {entry.date.date()} vs settlement net {s.net_amount} on "
            f"{s.settlement_date.date()}"
        ),
    )


ALL_INVARIANT_NAMES = [
    "I1_settlement_arithmetic_consistent",
    "I2_fees_and_tax_match_schedule",
    "I3_net_matches_shadow_ledger",
    "I4_gross_matches_capture",
    "I5_settlement_within_window",
    "I6_bank_credit_matches_settlement",
]


def check_all(
    payment: Payment,
    expected: ExpectedSettlement,
    settlement: Settlement,
    bank_entries: list[BankEntry],
) -> list[InvariantResult]:
    """Run every invariant for one lifecycle, in declaration order."""
    return [
        settlement_arithmetic_consistent(settlement),
        fees_and_tax_match_schedule(settlement),
        net_matches_shadow_ledger(expected, settlement),
        gross_matches_capture(expected, settlement),
        settlement_within_window(expected, settlement),
        bank_credit_matches_settlement(settlement, bank_entries),
    ]
