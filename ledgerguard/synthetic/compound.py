"""Hand-designed compound failure cases.

The generated fault taxonomy injects one fault per lifecycle. Real
reconciliation failures are not that tidy: a settlement can be both late and
short, or carry a fee overcharge *and* an unexplained deduction. Compound cases
are where a controller that classifies by "first failing invariant" quietly
closes a case on one proven cause while a second, unproven one is still open.

These five are built by hand rather than generated, because the point is not
volume -- it is that each one probes a specific way the single-cause assumption
can break.

Kept out of the generator on purpose: adding them to the seeded dataset would
change the frozen holdout and invalidate every published number.
"""

from __future__ import annotations

from dataclasses import dataclass
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

T0 = datetime(2026, 7, 15, 10, 0, 0)

AUTO = "AUTO_RESOLVED"
HUMAN = "HUMAN_REVIEW_REQUIRED"


@dataclass(frozen=True)
class CompoundCase:
    case_id: str
    name: str
    faults: tuple[str, ...]
    payment_id: str
    expected_disposition: str
    why: str


def _leg(
    tag: str,
    customer: str,
    amount: int,
    *,
    fee_overcharge: int = 0,
    refund_adjustment: int = 0,
    days_late: int = 0,
    settle: bool = True,
    duplicate_bank_credit: bool = False,
):
    """One lifecycle, with each fault applied independently and explicitly."""
    order = Order(
        order_id=f"ord_{tag}",
        merchant_id="mch_CMPD",
        amount=amount,
        created_at=T0,
        customer_reference=customer,
    )
    fee = platform_fee(amount)
    tax = gst_on_fee(fee)
    captured = T0 + timedelta(minutes=5)
    payment = Payment(
        payment_id=f"pay_{tag}",
        order_id=order.order_id,
        amount=amount,
        captured_at=captured,
        fee=fee,
        tax=tax,
        reference=f"PAY/{order.order_id}",
    )
    if not settle:
        return order, payment, None, []

    charged_fee = fee + fee_overcharge
    charged_tax = gst_on_fee(charged_fee)
    net = amount - charged_fee - charged_tax - refund_adjustment
    settled_on = captured + timedelta(days=2 + days_late)
    settlement = Settlement(
        settlement_id=f"setl_{tag}",
        merchant_id=order.merchant_id,
        payment_ids=[payment.payment_id],
        gross_amount=amount,
        fees=charged_fee,
        tax=charged_tax,
        refund_adjustments=refund_adjustment,
        other_adjustments=0,
        net_amount=net,
        settlement_date=settled_on,
        reference=f"STL/setl_{tag}",
    )
    entries = [
        BankEntry(
            bank_entry_id=f"bnk_{tag}",
            amount=net,
            credit_or_debit=CreditDebit.CREDIT,
            date=settled_on,
            reference=settlement.reference,
        )
    ]
    if duplicate_bank_credit:
        entries.append(entries[0].model_copy(update={"bank_entry_id": f"bnk_{tag}D"}))
    return order, payment, settlement, entries


def build_compound_batch() -> tuple[Batch, list[CompoundCase]]:
    orders, payments, refunds, settlements, entries = [], [], [], [], []
    cases: list[CompoundCase] = []

    def add(leg, refund=None):
        o, p, s, e = leg
        orders.append(o)
        payments.append(p)
        if s:
            settlements.append(s)
        entries.extend(e)
        if refund:
            refunds.append(refund)

    # --- C1: late settlement AND an unlinked partial refund -----------------
    # Both faults are real. The refund is genuinely this payment's, so the
    # discrepancy is resolvable -- but the delay must not disappear because the
    # shortfall was classified first.
    r1 = to_paise("900.00")
    add(_leg("C1", "CUST-C1", to_paise("6000.00"), refund_adjustment=r1, days_late=7),
        Refund(refund_id="rfnd_C1", payment_id=None, amount=r1,
               created_at=T0 + timedelta(days=1), reference="ADJ/CUST-C1/partial"))
    cases.append(CompoundCase(
        "C1", "delayed settlement + unlinked partial refund",
        ("F5", "F3"), "pay_C1", AUTO,
        "The refund genuinely belongs to this payment and restores the ledger. "
        "Resolvable -- but the 7 day delay is a second finding that must still "
        "be reported, not silently absorbed.",
    ))

    # --- C2: fee overcharge AND an unexplained deduction --------------------
    # The fee variance is provable. The extra deduction is not. Closing on the
    # proven cause alone would leave real money unexplained.
    add(_leg("C2", "CUST-C2", to_paise("9000.00"),
             fee_overcharge=to_paise("40.00"), refund_adjustment=to_paise("650.00")))
    cases.append(CompoundCase(
        "C2", "fee overcharge + unexplained deduction",
        ("F4", "F6"), "pay_C2", HUMAN,
        "The fee overcharge is deterministically proven, but correcting it does "
        "not close the gap: INR 650.00 is still deducted with no refund record "
        "anywhere. One proven cause does not license closing the case.",
    ))

    # --- C3: duplicate bank credit AND a late settlement --------------------
    add(_leg("C3", "CUST-C3", to_paise("4500.00"), days_late=6,
             duplicate_bank_credit=True))
    cases.append(CompoundCase(
        "C3", "duplicate bank credit + delayed settlement",
        ("F2", "F5"), "pay_C3", AUTO,
        "Both faults are individually provable and neither leaves money "
        "unexplained: the duplicate is suppressed and the delay is timing only.",
    ))

    # --- C4: missing settlement, with a tempting orphan refund --------------
    # Nothing settled at all, and an unlinked refund of a plausible size sits in
    # the batch. It must not be netted against a settlement that never arrived.
    add(_leg("C4", "CUST-C4", to_paise("7000.00"), settle=False),
        Refund(refund_id="rfnd_C4", payment_id=None, amount=to_paise("1200.00"),
               created_at=T0 + timedelta(days=1), reference="ADJ/CUST-C4/partial"))
    cases.append(CompoundCase(
        "C4", "missing settlement + orphan refund of the same customer",
        ("F1", "F3"), "pay_C4", HUMAN,
        "The funds never settled. A matching refund for the same customer is "
        "irrelevant: there is no settlement to reconcile it against, and the "
        "full captured amount is outstanding.",
    ))

    # --- C5: fee overcharge AND a wrong-linkage deduction -------------------
    # The adversarial case, with a proven fault layered on top. The fee variance
    # is real; the deduction is explained by a refund belonging to C1's customer.
    add(_leg("C5", "CUST-C5", to_paise("8000.00"),
             fee_overcharge=to_paise("25.00"), refund_adjustment=r1))
    cases.append(CompoundCase(
        "C5", "fee overcharge + wrong-linkage deduction",
        ("F4", "F6"), "pay_C5", HUMAN,
        "A provable fee variance sits on top of a deduction that exactly matches "
        "rfnd_C1 -- which belongs to CUST-C1, not CUST-C5. The proven fault must "
        "not become a reason to close the unproven one.",
    ))

    return (
        Batch(
            orders=orders,
            payments=payments,
            refunds=refunds,
            settlements=settlements,
            bank_entries=entries,
        ),
        cases,
    )
