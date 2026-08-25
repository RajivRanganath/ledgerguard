"""Deterministic synthetic lifecycle generator.

Produces internally consistent order -> payment -> (optional refund) ->
settlement -> bank entry lifecycles, computes the correct ground truth BEFORE
any fault is injected, and keeps that ground truth in a structure the
controller never receives.

Same seed + same version => byte-identical dataset.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..ledger.models import (
    BankEntry,
    Batch,
    CreditDebit,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Refund,
    RefundStatus,
    Settlement,
    SettlementStatus,
)
from ..ledger.money import gst_on_fee, platform_fee

GENERATOR_VERSION = "1.0.0"
EPOCH = datetime(2026, 6, 1, 9, 0, 0)
MERCHANTS = ["mch_ACME01", "mch_BLUE02", "mch_CITR03", "mch_DELT04"]
METHODS = ["upi", "card", "netbanking", "wallet"]
SETTLEMENT_LAG_DAYS = 2
REFUND_PROBABILITY = 0.28
CUSTOMER_POOL = 90          # < number of lifecycles, so customers repeat


class FaultClass:
    NONE = "NONE"
    F1_MISSING_SETTLEMENT = "F1_MISSING_SETTLEMENT"
    F2_DUPLICATE_RECORD = "F2_DUPLICATE_RECORD"
    F3_UNLINKED_PARTIAL_REFUND = "F3_UNLINKED_PARTIAL_REFUND"
    F4_FEE_TAX_MISMATCH = "F4_FEE_TAX_MISMATCH"
    F5_DELAYED_SETTLEMENT = "F5_DELAYED_SETTLEMENT"
    F6_INCORRECT_LINKAGE = "F6_INCORRECT_LINKAGE"


@dataclass
class GroundTruth:
    """Hidden truth. Never handed to the matcher, the AI, or the gate."""

    lifecycle_id: str
    fault_class: str
    expected_exception: str | None          # ExceptionType value, or None
    expected_disposition: str              # AUTO_RESOLVED | HUMAN_REVIEW_REQUIRED | NONE
    true_cause: str
    exposure_paise: int = 0
    paired_with: str | None = None

    def as_dict(self) -> dict:
        return dict(
            lifecycle_id=self.lifecycle_id,
            fault_class=self.fault_class,
            expected_exception=self.expected_exception,
            expected_disposition=self.expected_disposition,
            true_cause=self.true_cause,
            exposure_paise=self.exposure_paise,
            paired_with=self.paired_with,
        )


@dataclass
class Lifecycle:
    lifecycle_id: str
    order: Order
    payment: Payment
    refunds: list[Refund] = field(default_factory=list)
    settlement: Settlement | None = None
    bank_entries: list[BankEntry] = field(default_factory=list)
    extra_payments: list[Payment] = field(default_factory=list)

    def records(self) -> tuple[list, list, list, list, list]:
        return (
            [self.order],
            [self.payment] + self.extra_payments,
            list(self.refunds),
            [self.settlement] if self.settlement else [],
            list(self.bank_entries),
        )


@dataclass
class Dataset:
    seed: int
    generator_version: str
    lifecycles: list[Lifecycle]
    ground_truth: dict[str, GroundTruth]

    def batch(self) -> Batch:
        b = Batch()
        for lc in self.lifecycles:
            o, p, r, s, e = lc.records()
            b.orders += o
            b.payments += p
            b.refunds += r
            b.settlements += s
            b.bank_entries += e
        return b

    def lifecycle_by_payment(self) -> dict[str, str]:
        return {lc.payment.payment_id: lc.lifecycle_id for lc in self.lifecycles}

    def fault_class_by_payment(self) -> dict[str, str]:
        return {
            lc.payment.payment_id: self.ground_truth[lc.lifecycle_id].fault_class
            for lc in self.lifecycles
        }

    def subset(self, lifecycle_ids: list[str]) -> "Dataset":
        keep = set(lifecycle_ids)
        return Dataset(
            seed=self.seed,
            generator_version=self.generator_version,
            lifecycles=[lc for lc in self.lifecycles if lc.lifecycle_id in keep],
            ground_truth={k: v for k, v in self.ground_truth.items() if k in keep},
        )


def _money_amount(rng: random.Random) -> int:
    """A plausible order amount in paise, varied enough to limit collisions."""
    band = rng.choice([(49900, 500000), (500000, 2500000), (2500000, 9500000)])
    return rng.randrange(band[0], band[1], 100)


def build_clean_lifecycles(seed: int, count: int) -> tuple[list[Lifecycle], random.Random]:
    """Build ``count`` fully consistent lifecycles. No faults yet."""
    rng = random.Random(seed)
    lifecycles: list[Lifecycle] = []

    for i in range(count):
        lc_id = f"LC{i:04d}"
        merchant = MERCHANTS[i % len(MERCHANTS)]
        customer = f"CUST-{rng.randrange(CUSTOMER_POOL):04d}"
        order_id = f"ord_{i:04d}{rng.randrange(0x1000, 0xFFFF):04X}"
        amount = _money_amount(rng)

        created = EPOCH + timedelta(
            days=rng.randrange(0, 30), hours=rng.randrange(0, 11), minutes=rng.randrange(0, 60)
        )
        captured = created + timedelta(minutes=rng.randrange(1, 25))

        order = Order(
            order_id=order_id,
            merchant_id=merchant,
            amount=amount,
            created_at=created,
            status=OrderStatus.PAID,
            customer_reference=customer,
        )

        fee = platform_fee(amount)
        tax = gst_on_fee(fee)
        payment = Payment(
            payment_id=f"pay_{i:04d}{rng.randrange(0x1000, 0xFFFF):04X}",
            order_id=order_id,
            amount=amount,
            status=PaymentStatus.CAPTURED,
            method=rng.choice(METHODS),
            captured_at=captured,
            fee=fee,
            tax=tax,
            reference=f"PAY/{order_id}",
        )

        refunds: list[Refund] = []
        if rng.random() < REFUND_PROBABILITY:
            # Partial refund, always strictly less than the captured amount.
            refund_amount = rng.randrange(int(amount * 0.15), int(amount * 0.65), 100)
            refunds.append(
                Refund(
                    refund_id=f"rfnd_{i:04d}{rng.randrange(0x1000, 0xFFFF):04X}",
                    payment_id=payment.payment_id,
                    amount=refund_amount,
                    status=RefundStatus.PROCESSED,
                    created_at=captured + timedelta(days=1, hours=rng.randrange(0, 12)),
                    reference=f"RFND/{order_id}",
                )
            )

        refund_total = sum(r.amount for r in refunds)
        net = amount - fee - tax - refund_total
        settled_on = captured + timedelta(days=SETTLEMENT_LAG_DAYS)
        settlement_id = f"setl_{i:04d}{rng.randrange(0x1000, 0xFFFF):04X}"
        settlement = Settlement(
            settlement_id=settlement_id,
            merchant_id=merchant,
            payment_ids=[payment.payment_id],
            gross_amount=amount,
            fees=fee,
            tax=tax,
            refund_adjustments=refund_total,
            other_adjustments=0,
            net_amount=net,
            settlement_date=settled_on,
            status=SettlementStatus.PROCESSED,
            reference=f"STL/{settlement_id}",
        )

        bank = BankEntry(
            bank_entry_id=f"bnk_{i:04d}{rng.randrange(0x1000, 0xFFFF):04X}",
            amount=net,
            credit_or_debit=CreditDebit.CREDIT,
            date=settled_on,
            reference=settlement.reference,
            description=f"RAZORPAY SETTLEMENT {merchant}",
        )

        lifecycles.append(
            Lifecycle(
                lifecycle_id=lc_id,
                order=order,
                payment=payment,
                refunds=refunds,
                settlement=settlement,
                bank_entries=[bank],
            )
        )

    return lifecycles, rng
