"""The Shadow Ledger.

An independent reconstruction of what the financial movement *should* have
been, computed from first principles out of orders, payments and refunds.

It never reads settlement or bank records to decide what to expect -- that is
the whole point. Settlements and bank entries are the *observed* side; the
Shadow Ledger is the *expected* side, and reconciliation is the comparison.

The AI investigator can call ``expected_for_payment`` to inspect output, but
has no path to change how the number is produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import (
    Batch,
    BankEntry,
    Order,
    Payment,
    PaymentStatus,
    Refund,
    RefundStatus,
    Settlement,
)
from .money import gst_on_fee, platform_fee

# Provider settlement window: money captured on day T is expected to settle on
# T+2, and we allow a one day tolerance either side for weekends/holidays.
SETTLEMENT_LAG_DAYS = 2
SETTLEMENT_WINDOW_TOLERANCE_DAYS = 1

# Bank credit is expected on the settlement date, allowing one day of float.
BANK_CREDIT_TOLERANCE_DAYS = 1


@dataclass(frozen=True)
class ExpectedSettlement:
    """What the Shadow Ledger says a payment should settle for."""

    payment_id: str
    gross_amount: int
    fees: int
    tax: int
    refund_adjustments: int
    other_adjustments: int
    net_amount: int
    refund_ids: tuple[str, ...] = ()
    window_start: datetime | None = None
    window_end: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "gross_amount": self.gross_amount,
            "fees": self.fees,
            "tax": self.tax,
            "refund_adjustments": self.refund_adjustments,
            "other_adjustments": self.other_adjustments,
            "net_amount": self.net_amount,
            "refund_ids": list(self.refund_ids),
        }


@dataclass
class ShadowLedger:
    """Indexes a batch and computes expected financial movement."""

    batch: Batch
    orders_by_id: dict[str, Order] = field(default_factory=dict)
    payments_by_id: dict[str, Payment] = field(default_factory=dict)
    refunds_by_id: dict[str, Refund] = field(default_factory=dict)
    refunds_by_payment: dict[str, list[Refund]] = field(default_factory=dict)
    settlements_by_payment: dict[str, list[Settlement]] = field(default_factory=dict)
    bank_by_reference: dict[str, list[BankEntry]] = field(default_factory=dict)
    orphan_refunds: list[Refund] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._index()

    def _index(self) -> None:
        for o in self.batch.orders:
            self.orders_by_id.setdefault(o.order_id, o)
        for p in self.batch.payments:
            self.payments_by_id.setdefault(p.payment_id, p)
        for r in self.batch.refunds:
            self.refunds_by_id.setdefault(r.refund_id, r)
            if r.payment_id:
                self.refunds_by_payment.setdefault(r.payment_id, []).append(r)
            else:
                self.orphan_refunds.append(r)
        for s in self.batch.settlements:
            for pid in s.payment_ids:
                self.settlements_by_payment.setdefault(pid, []).append(s)
        for b in self.batch.bank_entries:
            self.bank_by_reference.setdefault(b.reference, []).append(b)

    # ---------------------------------------------------------------- expected

    def valid_refunds_for(self, payment_id: str) -> list[Refund]:
        """Refunds that are *provably* linked to this payment and processed."""
        return [
            r
            for r in self.refunds_by_payment.get(payment_id, [])
            if r.status == RefundStatus.PROCESSED
        ]

    def expected_for_payment(
        self,
        payment: Payment,
        extra_refunds: list[Refund] | None = None,
        other_adjustments: int = 0,
    ) -> ExpectedSettlement:
        """Reconstruct the expected settlement for one captured payment.

        expected_net = captured - refunds - fees - tax +/- valid adjustments

        ``extra_refunds`` lets a caller ask a hypothetical: "if this refund
        really did belong to this payment, would the ledger balance?" It does
        not mutate any stored state.
        """
        if payment.status != PaymentStatus.CAPTURED:
            gross = 0
        else:
            gross = payment.amount

        fees = platform_fee(gross)
        tax = gst_on_fee(fees)

        refunds = list(self.valid_refunds_for(payment.payment_id))
        if extra_refunds:
            known = {r.refund_id for r in refunds}
            refunds += [r for r in extra_refunds if r.refund_id not in known]

        refund_total = sum(r.amount for r in refunds)
        net = gross - fees - tax - refund_total + other_adjustments

        start = end = None
        if payment.captured_at is not None:
            due = payment.captured_at + timedelta(days=SETTLEMENT_LAG_DAYS)
            start = due - timedelta(days=SETTLEMENT_WINDOW_TOLERANCE_DAYS)
            end = due + timedelta(days=SETTLEMENT_WINDOW_TOLERANCE_DAYS)

        return ExpectedSettlement(
            payment_id=payment.payment_id,
            gross_amount=gross,
            fees=fees,
            tax=tax,
            refund_adjustments=refund_total,
            other_adjustments=other_adjustments,
            net_amount=net,
            refund_ids=tuple(sorted(r.refund_id for r in refunds)),
            window_start=start,
            window_end=end,
        )

    # ---------------------------------------------------------------- observed

    def settlements_for(self, payment_id: str) -> list[Settlement]:
        return self.settlements_by_payment.get(payment_id, [])

    def bank_entries_for_settlement(self, settlement: Settlement) -> list[BankEntry]:
        return self.bank_by_reference.get(settlement.reference, [])

    def candidate_orphan_refunds(
        self, amount: int, tolerance: int = 0
    ) -> list[Refund]:
        """Unlinked refunds whose amount could explain a shortfall of ``amount``.

        This is retrieval only. It asserts nothing about whether the refund
        actually belongs to the payment in question -- that is the Evidence
        Gate's job, and the distinction is the entire adversarial case.
        """
        return [
            r
            for r in self.orphan_refunds
            if abs(r.amount - amount) <= tolerance
            and r.status == RefundStatus.PROCESSED
        ]

    def totals(self) -> dict:
        captured = sum(
            p.amount for p in self.batch.payments if p.status == PaymentStatus.CAPTURED
        )
        return {
            "orders": len(self.batch.orders),
            "payments": len(self.batch.payments),
            "refunds": len(self.batch.refunds),
            "settlements": len(self.batch.settlements),
            "bank_entries": len(self.batch.bank_entries),
            "captured_value_paise": captured,
        }
