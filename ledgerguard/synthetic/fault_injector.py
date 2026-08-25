"""Controlled fault injection over clean lifecycles.

Every injected fault records its ground truth: which lifecycle was corrupted,
what the true cause is, what exception the controller *should* raise, and what
the *safe* disposition is. "Safe disposition" is deliberately not "resolve
everything" -- for two fault classes the correct behaviour is to abstain.
"""

from __future__ import annotations

import random
from copy import deepcopy

from ..ledger.models import BankEntry, CreditDebit, ExceptionType, RefundStatus
from ..ledger.money import gst_on_fee, platform_fee
from .generator import Dataset, FaultClass, GroundTruth, Lifecycle, build_clean_lifecycles

# Share of lifecycles that receive a fault.
FAULT_RATE = 0.32

AUTO = "AUTO_RESOLVED"
HUMAN = "HUMAN_REVIEW_REQUIRED"
NONE = "NONE"


def _noisy_customer_reference(customer: str, rng: random.Random) -> str:
    """A dirty free-text refund reference: the customer, never the order id.

    This is what makes F3/F6 genuinely ambiguous for a structured matcher. The
    matcher does exact ID matching only; a customer can have several orders, so
    even parsing this string does not identify which payment the refund belongs
    to. Selecting the payment is the investigator's job; proving the selection
    is the Evidence Gate's job.
    """
    template = rng.choice(
        [
            "ADJ/{c}/partial",
            "REF {c} - cust adj",
            "chargeback-adj {c}",
            "{c}/refund posted",
        ]
    )
    return template.format(c=customer)


def _apply_f1(lc: Lifecycle, rng: random.Random) -> GroundTruth:
    """Missing settlement: payment captured, settlement and credit never arrived."""
    exposure = lc.settlement.net_amount if lc.settlement else lc.payment.amount
    lc.settlement = None
    lc.bank_entries = []
    return GroundTruth(
        lifecycle_id=lc.lifecycle_id,
        fault_class=FaultClass.F1_MISSING_SETTLEMENT,
        expected_exception=ExceptionType.MISSING_SETTLEMENT.value,
        expected_disposition=HUMAN,
        true_cause="Captured payment was never settled; funds are outstanding.",
        exposure_paise=exposure,
    )


def _apply_f2(lc: Lifecycle, rng: random.Random) -> GroundTruth:
    """Duplicate record: the same event is ingested twice."""
    if rng.random() < 0.5 and lc.bank_entries:
        dup = deepcopy(lc.bank_entries[0])
        dup.bank_entry_id = dup.bank_entry_id + "D"
        lc.bank_entries.append(dup)
        detail = f"Bank credit {lc.bank_entries[0].bank_entry_id} re-ingested as {dup.bank_entry_id}."
        exposure = dup.amount
    else:
        lc.extra_payments.append(deepcopy(lc.payment))
        detail = f"Payment {lc.payment.payment_id} appears twice in the batch."
        exposure = lc.payment.amount
    return GroundTruth(
        lifecycle_id=lc.lifecycle_id,
        fault_class=FaultClass.F2_DUPLICATE_RECORD,
        expected_exception=ExceptionType.DUPLICATE_RECORD.value,
        expected_disposition=AUTO,
        true_cause=detail,
        exposure_paise=exposure,
    )


def _apply_f3(lc: Lifecycle, rng: random.Random) -> GroundTruth:
    """Unlinked partial refund.

    The settlement correctly deducted a partial refund, but the refund record
    lost its payment linkage and carries only a dirty customer reference. The
    Shadow Ledger therefore expects a higher net than was paid out.
    """
    refund = lc.refunds[0]
    refund.payment_id = None
    refund.reference = _noisy_customer_reference(lc.order.customer_reference, rng)
    return GroundTruth(
        lifecycle_id=lc.lifecycle_id,
        fault_class=FaultClass.F3_UNLINKED_PARTIAL_REFUND,
        expected_exception=ExceptionType.UNEXPLAINED_SHORTFALL.value,
        expected_disposition=AUTO,
        true_cause=(
            f"Shortfall is genuinely explained by refund {refund.refund_id} "
            f"({refund.amount} paise), which belongs to payment {lc.payment.payment_id} "
            "but lost its linkage during ingestion."
        ),
        exposure_paise=refund.amount,
    )


def _apply_f4(lc: Lifecycle, rng: random.Random) -> GroundTruth:
    """Fee or tax mismatch: provider charged off-schedule fees."""
    s = lc.settlement
    delta = rng.randrange(2000, max(3000, int(s.gross_amount * 0.012)), 100)
    s.fees += delta
    s.tax = gst_on_fee(s.fees)
    correct_tax = gst_on_fee(platform_fee(s.gross_amount))
    s.net_amount = (
        s.gross_amount - s.fees - s.tax - s.refund_adjustments + s.other_adjustments
    )
    for e in lc.bank_entries:
        e.amount = s.net_amount
    overcharge = delta + (s.tax - correct_tax)
    return GroundTruth(
        lifecycle_id=lc.lifecycle_id,
        fault_class=FaultClass.F4_FEE_TAX_MISMATCH,
        expected_exception=ExceptionType.FEE_MISMATCH.value,
        expected_disposition=AUTO,
        true_cause=(
            f"Fee overcharged by {delta} paise plus GST; total overcharge {overcharge} paise."
        ),
        exposure_paise=overcharge,
    )


def _apply_f5(lc: Lifecycle, rng: random.Random) -> GroundTruth:
    """Delayed settlement: correct amounts, outside the expected window."""
    from datetime import timedelta

    delay = rng.randrange(5, 13)
    lc.settlement.settlement_date += timedelta(days=delay)
    for e in lc.bank_entries:
        e.date = lc.settlement.settlement_date
    return GroundTruth(
        lifecycle_id=lc.lifecycle_id,
        fault_class=FaultClass.F5_DELAYED_SETTLEMENT,
        expected_exception=ExceptionType.DELAYED_SETTLEMENT.value,
        expected_disposition=AUTO,
        true_cause=f"Settlement arrived {delay} days late; all amounts are correct.",
        exposure_paise=0,
    )


def _apply_f6(victim: Lifecycle, donor: Lifecycle, rng: random.Random) -> tuple[GroundTruth, GroundTruth]:
    """Incorrect linkage -- the adversarial case.

    ``donor`` is given a genuine F3 (its own refund is unlinked). ``victim``
    is given an unexplained deduction of *exactly the same amount* with no
    refund of its own anywhere in the system.

    From the outside the two exceptions are indistinguishable: same shape,
    same size, one plausible orphan refund that fits both by amount. Only the
    customer linkage separates them, and only the Evidence Gate checks it.
    """
    donor_truth = _apply_f3(donor, rng)
    refund = donor.refunds[0]
    amount = refund.amount

    s = victim.settlement
    s.refund_adjustments += amount
    s.net_amount = (
        s.gross_amount - s.fees - s.tax - s.refund_adjustments + s.other_adjustments
    )
    for e in victim.bank_entries:
        e.amount = s.net_amount

    victim_truth = GroundTruth(
        lifecycle_id=victim.lifecycle_id,
        fault_class=FaultClass.F6_INCORRECT_LINKAGE,
        expected_exception=ExceptionType.UNEXPLAINED_SHORTFALL.value,
        expected_disposition=HUMAN,
        true_cause=(
            f"Settlement deducted {amount} paise with no refund for payment "
            f"{victim.payment.payment_id}. Refund {refund.refund_id} matches the amount "
            f"exactly but belongs to customer {donor.order.customer_reference} "
            f"(payment {donor.payment.payment_id}). Not resolvable from available evidence."
        ),
        exposure_paise=amount,
        paired_with=donor.lifecycle_id,
    )
    donor_truth.paired_with = victim.lifecycle_id
    return victim_truth, donor_truth


def _clean_truth(lc: Lifecycle) -> GroundTruth:
    return GroundTruth(
        lifecycle_id=lc.lifecycle_id,
        fault_class=FaultClass.NONE,
        expected_exception=None,
        expected_disposition=NONE,
        true_cause="Clean lifecycle; reconciles exactly.",
        exposure_paise=0,
    )


def build_dataset(seed: int = 20260905, count: int = 320) -> Dataset:
    """Generate clean lifecycles, then inject the six core fault classes."""
    lifecycles, rng = build_clean_lifecycles(seed, count)
    by_id = {lc.lifecycle_id: lc for lc in lifecycles}
    truth: dict[str, GroundTruth] = {}
    used: set[str] = set()

    n_faulty = int(count * FAULT_RATE)
    order = list(range(count))
    rng.shuffle(order)
    pool = [lifecycles[i] for i in order]

    with_refund = [lc for lc in pool if lc.refunds]
    without_refund = [lc for lc in pool if not lc.refunds]

    # ---- F6 first: it is the most constrained (needs a donor/victim pair).
    n_f6 = max(3, n_faulty // 8)
    for _ in range(n_f6):
        donor = next((lc for lc in with_refund if lc.lifecycle_id not in used), None)
        victim = next((lc for lc in without_refund if lc.lifecycle_id not in used), None)
        if donor is None or victim is None:
            break
        used.add(donor.lifecycle_id)
        used.add(victim.lifecycle_id)
        vt, dt = _apply_f6(victim, donor, rng)
        truth[victim.lifecycle_id] = vt
        truth[donor.lifecycle_id] = dt

    # ---- F3 standalone: needs a lifecycle that actually has a refund.
    n_f3 = max(4, n_faulty // 6)
    for _ in range(n_f3):
        lc = next((x for x in with_refund if x.lifecycle_id not in used), None)
        if lc is None:
            break
        used.add(lc.lifecycle_id)
        truth[lc.lifecycle_id] = _apply_f3(lc, rng)

    # ---- F1, F2, F4, F5 spread over the remaining budget.
    remaining = max(0, n_faulty - len(used))
    plan = [
        (_apply_f1, remaining // 4),
        (_apply_f2, remaining // 4),
        (_apply_f4, remaining // 4),
        (_apply_f5, remaining - 3 * (remaining // 4)),
    ]
    for fn, n in plan:
        for _ in range(n):
            lc = next((x for x in pool if x.lifecycle_id not in used), None)
            if lc is None:
                break
            used.add(lc.lifecycle_id)
            truth[lc.lifecycle_id] = fn(lc, rng)

    for lc in lifecycles:
        truth.setdefault(lc.lifecycle_id, _clean_truth(lc))

    assert len(truth) == len(lifecycles)
    return Dataset(
        seed=seed,
        generator_version="1.0.0",
        lifecycles=lifecycles,
        ground_truth=truth,
    )


def split(dataset: Dataset, holdout_fraction: float = 0.25) -> tuple[Dataset, Dataset]:
    """Deterministic dev / frozen-holdout split, stratified by fault class.

    Stratifying matters: an unstratified 25% slice can easily contain zero F6
    cases, which would make the headline safety metric meaningless.
    Paired F6 lifecycles are always kept on the same side of the split.
    """
    rng = random.Random(dataset.seed ^ 0x5EED)
    buckets: dict[str, list[str]] = {}
    for lc_id, gt in sorted(dataset.ground_truth.items()):
        buckets.setdefault(gt.fault_class, []).append(lc_id)

    holdout: set[str] = set()
    for fault_class, ids in sorted(buckets.items()):
        ids = sorted(ids)
        rng.shuffle(ids)
        n = round(len(ids) * holdout_fraction)
        for lc_id in ids[:n]:
            holdout.add(lc_id)
            partner = dataset.ground_truth[lc_id].paired_with
            if partner:
                holdout.add(partner)

    dev_ids = [lc.lifecycle_id for lc in dataset.lifecycles if lc.lifecycle_id not in holdout]
    hold_ids = [lc.lifecycle_id for lc in dataset.lifecycles if lc.lifecycle_id in holdout]
    return dataset.subset(dev_ids), dataset.subset(hold_ids)
