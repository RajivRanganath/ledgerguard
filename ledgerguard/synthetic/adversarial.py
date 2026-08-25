"""The hand-built adversarial case.

Two payments, one orphan refund.

  Payment A (CUST-0001, INR 8,000)  settled INR 1,500.00 short, with no refund
                                    of its own anywhere in the system.
  Payment B (CUST-0077, INR 6,000)  settled INR 1,500.00 short, and its own
                                    partial refund lost its payment linkage.

One refund record exists, for exactly INR 1,500.00, carrying a dirty free-text
reference that names CUST-0077.

Both shortfalls are the same size. Applying the refund to *either* payment
makes the shadow ledger balance -- so arithmetic alone cannot separate them,
and an investigator that reasons from amount agreement will confidently attach
the refund to whichever case it is shown. Only the customer linkage
distinguishes them.

This fixture is built by hand, kept out of the generator, and pinned by two
tests, so a refactor cannot quietly break the case the demo depends on.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..ledger.models import (
    BankEntry,
    Batch,
    CreditDebit,
    Order,
    Payment,
    Refund,
    Settlement,
)
from ..ledger.money import gst_on_fee, platform_fee, to_paise

T0 = datetime(2026, 7, 1, 11, 0, 0)
SHARED_SHORTFALL = to_paise("1500.00")

VICTIM_PAYMENT_ID = "pay_ADVERSARY_A"      # innocent: nothing explains its shortfall
DONOR_PAYMENT_ID = "pay_ADVERSARY_B"       # genuinely explained by the orphan refund
ORPHAN_REFUND_ID = "rfnd_ORPHAN"


def _leg(
    tag: str,
    payment_id: str,
    customer: str,
    amount: int,
    shortfall: int,
    offset_days: int,
):
    order = Order(
        order_id=f"ord_{tag}",
        merchant_id="mch_ADV",
        amount=amount,
        created_at=T0 + timedelta(days=offset_days),
        customer_reference=customer,
    )
    fee = platform_fee(amount)
    tax = gst_on_fee(fee)
    captured = order.created_at + timedelta(minutes=6)
    payment = Payment(
        payment_id=payment_id,
        order_id=order.order_id,
        amount=amount,
        captured_at=captured,
        fee=fee,
        tax=tax,
        reference=f"PAY/{order.order_id}",
    )
    net = amount - fee - tax - shortfall
    settled_on = captured + timedelta(days=2)
    settlement = Settlement(
        settlement_id=f"setl_{tag}",
        merchant_id=order.merchant_id,
        payment_ids=[payment_id],
        gross_amount=amount,
        fees=fee,
        tax=tax,
        refund_adjustments=shortfall,      # provider says a refund was netted off
        other_adjustments=0,
        net_amount=net,
        settlement_date=settled_on,
        reference=f"STL/setl_{tag}",
    )
    bank = BankEntry(
        bank_entry_id=f"bnk_{tag}",
        amount=net,
        credit_or_debit=CreditDebit.CREDIT,
        date=settled_on,
        reference=settlement.reference,
        description="RAZORPAY SETTLEMENT mch_ADV",
    )
    return order, payment, settlement, bank


def build_adversarial_batch() -> Batch:
    a_order, a_payment, a_settlement, a_bank = _leg(
        "ADVERSARY_A", VICTIM_PAYMENT_ID, "CUST-0001", to_paise("8000.00"),
        SHARED_SHORTFALL, offset_days=0,
    )
    b_order, b_payment, b_settlement, b_bank = _leg(
        "ADVERSARY_B", DONOR_PAYMENT_ID, "CUST-0077", to_paise("6000.00"),
        SHARED_SHORTFALL, offset_days=0,
    )

    # The single orphan refund. It really belongs to payment B; the reference
    # names B's customer in dirty free text, and the payment linkage is gone.
    orphan = Refund(
        refund_id=ORPHAN_REFUND_ID,
        payment_id=None,
        amount=SHARED_SHORTFALL,
        created_at=b_payment.captured_at + timedelta(days=1),
        reference="ADJ/CUST-0077/partial",
    )

    return Batch(
        orders=[a_order, b_order],
        payments=[a_payment, b_payment],
        refunds=[orphan],
        settlements=[a_settlement, b_settlement],
        bank_entries=[a_bank, b_bank],
    )
