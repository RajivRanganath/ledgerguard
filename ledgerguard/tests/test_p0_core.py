"""The five core P0 tests.

Each one protects something that would otherwise break silently:

1. financial arithmetic is exact
2. the Shadow Ledger invariant separates a good lifecycle from a corrupted one
3. the Evidence Gate accepts a correctly linked refund
4. the Evidence Gate rejects a refund that matches on amount but belongs to
   a different payment  (the adversarial case)
5. insufficient evidence produces abstention, never a forced auto resolution
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ledgerguard.ledger.invariants import (
    check_all,
    fees_and_tax_match_schedule,
    net_matches_shadow_ledger,
    settlement_arithmetic_consistent,
)
from ledgerguard.ledger.models import (
    BankEntry,
    Batch,
    CreditDebit,
    Order,
    Payment,
    Refund,
    Settlement,
)
from ledgerguard.ledger.money import gst_on_fee, platform_fee, to_paise, to_rupees_str
from ledgerguard.ledger.shadow_ledger import ShadowLedger

T0 = datetime(2026, 6, 10, 10, 0, 0)


# --------------------------------------------------------------------------- 1
def test_1_financial_arithmetic_is_exact():
    """A known example must produce the exact expected result, in paise.

    Order of INR 1,234.56, 2% platform fee, 18% GST on the fee, partial
    refund of INR 300.00.

        gross   123456 paise
        fee       2469 paise   (123456 * 0.02 = 2469.12 -> 2469)
        tax        444 paise   (2469 * 0.18 = 444.42   -> 444)
        refund   30000 paise
        net      90543 paise
    """
    gross = to_paise("1234.56")
    assert gross == 123456

    fee = platform_fee(gross)
    tax = gst_on_fee(fee)
    assert fee == 2469, f"fee drifted: {fee}"
    assert tax == 444, f"tax drifted: {tax}"

    refund = to_paise("300.00")
    net = gross - fee - tax - refund
    assert net == 90543
    assert to_rupees_str(net) == "905.43"

    # Everything stays an exact integer; no float ever touches the money path.
    for value in (gross, fee, tax, refund, net):
        assert isinstance(value, int)

    # Half-up rounding at the paise boundary, not banker's rounding.
    assert platform_fee(25) == 1          # 0.50 -> 1
    assert platform_fee(75) == 2          # 1.50 -> 2


def _lifecycle(amount: int = 500000, refund_amount: int | None = None):
    """A single, fully consistent lifecycle built by hand."""
    order = Order(
        order_id="ord_TEST01",
        merchant_id="mch_TEST",
        amount=amount,
        created_at=T0,
        customer_reference="CUST-0001",
    )
    fee = platform_fee(amount)
    tax = gst_on_fee(fee)
    payment = Payment(
        payment_id="pay_TEST01",
        order_id=order.order_id,
        amount=amount,
        captured_at=T0 + timedelta(minutes=3),
        fee=fee,
        tax=tax,
        reference=f"PAY/{order.order_id}",
    )
    refunds = []
    if refund_amount:
        refunds.append(
            Refund(
                refund_id="rfnd_TEST01",
                payment_id=payment.payment_id,
                amount=refund_amount,
                created_at=T0 + timedelta(days=1),
                reference=f"RFND/{order.order_id}",
            )
        )
    refund_total = sum(r.amount for r in refunds)
    net = amount - fee - tax - refund_total
    settled_on = payment.captured_at + timedelta(days=2)
    settlement = Settlement(
        settlement_id="setl_TEST01",
        merchant_id=order.merchant_id,
        payment_ids=[payment.payment_id],
        gross_amount=amount,
        fees=fee,
        tax=tax,
        refund_adjustments=refund_total,
        other_adjustments=0,
        net_amount=net,
        settlement_date=settled_on,
        reference="STL/setl_TEST01",
    )
    bank = BankEntry(
        bank_entry_id="bnk_TEST01",
        amount=net,
        credit_or_debit=CreditDebit.CREDIT,
        date=settled_on,
        reference=settlement.reference,
    )
    batch = Batch(
        orders=[order],
        payments=[payment],
        refunds=refunds,
        settlements=[settlement],
        bank_entries=[bank],
    )
    return batch, payment, settlement, bank


# --------------------------------------------------------------------------- 2
def test_2_shadow_ledger_invariants_separate_good_from_corrupted():
    """A valid lifecycle balances on every invariant; a corrupted one does not."""
    batch, payment, settlement, bank = _lifecycle(amount=500000, refund_amount=120000)
    ledger = ShadowLedger(batch)
    expected = ledger.expected_for_payment(payment)

    # 500000 - 10000 fee - 1800 tax - 120000 refund
    assert expected.net_amount == 368200
    assert settlement.net_amount == 368200

    results = check_all(payment, expected, settlement, [bank])
    assert all(r.holds for r in results), [r.detail for r in results if not r.holds]

    # --- corruption A: the provider under-pays by 5000 paise.
    settlement.net_amount -= 5000
    bank.amount = settlement.net_amount
    assert not settlement_arithmetic_consistent(settlement).holds
    i3 = net_matches_shadow_ledger(expected, settlement)
    assert not i3.holds
    assert i3.difference == 5000
    settlement.net_amount += 5000
    bank.amount = settlement.net_amount

    # --- corruption B: fees charged off-schedule, arithmetic still internally
    #     consistent. Only an independent fee schedule catches this.
    settlement.fees += 3000
    settlement.net_amount -= 3000
    bank.amount = settlement.net_amount
    assert settlement_arithmetic_consistent(settlement).holds
    assert not fees_and_tax_match_schedule(settlement).holds
    assert not net_matches_shadow_ledger(expected, settlement).holds

    # --- a refund the ledger does not know about is never silently absorbed.
    batch2, payment2, settlement2, bank2 = _lifecycle(amount=500000, refund_amount=None)
    ledger2 = ShadowLedger(batch2)
    settlement2.refund_adjustments = 90000
    settlement2.net_amount -= 90000
    inv = net_matches_shadow_ledger(ledger2.expected_for_payment(payment2), settlement2)
    assert not inv.holds
    assert inv.difference == 90000


def test_2b_generated_clean_lifecycles_reconcile_without_exceptions():
    """No clean lifecycle in the generated dataset may raise an exception."""
    from ledgerguard.reconciliation.matcher import MATCHED, reconcile_batch
    from ledgerguard.synthetic.fault_injector import build_dataset
    from ledgerguard.synthetic.generator import FaultClass

    ds = build_dataset()
    results, _ = reconcile_batch(ds.batch(), ds.lifecycle_by_payment())
    fault_class = ds.fault_class_by_payment()

    clean = [r for r in results if fault_class[r.payment_id] == FaultClass.NONE]
    assert clean, "dataset produced no clean lifecycles"
    false_positives = [r for r in clean if r.status != MATCHED]
    assert not false_positives, [
        (r.payment_id, r.status, r.exception.evidence.get("reason"))
        for r in false_positives[:5]
    ]

    # ... and every injected fault must raise the exception type it should.
    expected_type = {
        FaultClass.F1_MISSING_SETTLEMENT: "EXCEPTION_MISSING_SETTLEMENT",
        FaultClass.F2_DUPLICATE_RECORD: "EXCEPTION_DUPLICATE_RECORD",
        FaultClass.F3_UNLINKED_PARTIAL_REFUND: "EXCEPTION_UNEXPLAINED_SHORTFALL",
        FaultClass.F4_FEE_TAX_MISMATCH: "EXCEPTION_FEE_MISMATCH",
        FaultClass.F5_DELAYED_SETTLEMENT: "EXCEPTION_DELAYED_SETTLEMENT",
        FaultClass.F6_INCORRECT_LINKAGE: "EXCEPTION_UNEXPLAINED_SHORTFALL",
    }
    mismatches = [
        (r.payment_id, fault_class[r.payment_id], r.status)
        for r in results
        if fault_class[r.payment_id] != FaultClass.NONE
        and r.status != expected_type[fault_class[r.payment_id]]
    ]
    assert not mismatches, mismatches[:5]

    # Each of the six classes must actually be present.
    present = set(fault_class.values()) - {FaultClass.NONE}
    assert len(present) == 6, present


# ------------------------------------------------------------------------ 3,4
def _adversarial_run():
    from ledgerguard.pipeline import run
    from ledgerguard.synthetic.adversarial import build_adversarial_batch

    report = run(build_adversarial_batch(), use_ai=True)
    return {o.case.payment_id: o for o in report.outcomes}


def test_3_evidence_gate_accepts_a_correctly_linked_refund():
    """Payment B's shortfall really is its own refund. The gate must prove it."""
    from ledgerguard.evidence.verifier import VERIFIED
    from ledgerguard.evidence.safety import AUTO_RESOLVED
    from ledgerguard.synthetic.adversarial import DONOR_PAYMENT_ID, ORPHAN_REFUND_ID

    outcome = _adversarial_run()[DONOR_PAYMENT_ID]

    assert outcome.investigation is not None, "an ambiguous shortfall must be investigated"
    assert outcome.investigation.result.hypothesis == "unlinked_partial_refund"

    ver = outcome.verification
    assert ver.verdict == VERIFIED, [c.as_dict() for c in ver.failed()]
    assert ver.claimed_evidence_ids == [ORPHAN_REFUND_ID]
    assert ver.passed_count == ver.total_count

    # The decisive check is the ledger re-derivation, not the model's say-so.
    restored = next(c for c in ver.checks if c.name == "E7_invariant_restored")
    assert restored.passed
    linkage = next(c for c in ver.checks if c.name == "E4_reference_identifies_this_customer")
    assert linkage.passed

    assert outcome.decision.state == AUTO_RESOLVED


def test_4_evidence_gate_rejects_a_refund_belonging_to_another_payment():
    """THE adversarial case.

    Payment A's shortfall is the same size as the orphan refund, and applying
    that refund makes A's shadow ledger balance perfectly. The gate must still
    reject it -- on linkage, not on amount.
    """
    from ledgerguard.evidence.verifier import REJECTED
    from ledgerguard.evidence.safety import AUTO_RESOLVED, HUMAN_REVIEW_REQUIRED
    from ledgerguard.synthetic.adversarial import VICTIM_PAYMENT_ID

    outcome = _adversarial_run()[VICTIM_PAYMENT_ID]
    ver = outcome.verification

    # The investigator is expected to find this plausible. That is the point.
    assert outcome.investigation.result.hypothesis == "unlinked_partial_refund"
    assert outcome.investigation.result.recommended_action == "resolve"

    assert ver.verdict == REJECTED, ver.as_dict()

    # It must be rejected for the RIGHT reason.
    linkage = next(c for c in ver.checks if c.name == "E4_reference_identifies_this_customer")
    assert not linkage.passed, "linkage check must be the thing that fails"
    assert "NO MATCH" in linkage.detail

    # ... and specifically NOT because the numbers disagreed.
    amount = next(c for c in ver.checks if c.name == "E5_amount_equals_shortfall")
    assert amount.passed, "the amounts do match; rejection must not depend on that"
    restored = next(c for c in ver.checks if c.name == "E7_invariant_restored")
    assert restored.passed, "the ledger does balance; rejection must not depend on that"

    assert outcome.decision.state == HUMAN_REVIEW_REQUIRED
    assert outcome.decision.state != AUTO_RESOLVED
    assert "linkage" in outcome.decision.suggested_action.lower() or "counterparty" in (
        outcome.decision.suggested_action.lower()
    )


# --------------------------------------------------------------------------- 5
def test_5_insufficient_evidence_abstains_and_never_force_resolves():
    """Three separate ways of having no evidence must all end in abstention."""
    from ledgerguard.ai.provider import HeuristicProvider, UnavailableProvider
    from ledgerguard.ai.schemas import InvestigationResult
    from ledgerguard.evidence.safety import AUTO_RESOLVED, HUMAN_REVIEW_REQUIRED
    from ledgerguard.evidence.verifier import UNVERIFIED
    from ledgerguard.pipeline import run
    from ledgerguard.synthetic.adversarial import (
        DONOR_PAYMENT_ID,
        ORPHAN_REFUND_ID,
        build_adversarial_batch,
    )

    # (a) provider completely unavailable -> the batch still completes.
    batch = build_adversarial_batch()
    report = run(batch, use_ai=True, provider=UnavailableProvider("no API key"))
    assert len(report.outcomes) == 2
    for outcome in report.outcomes:
        assert outcome.verification.verdict == UNVERIFIED
        assert outcome.decision.state == HUMAN_REVIEW_REQUIRED
        assert outcome.decision.state != AUTO_RESOLVED

    # (b) a provider that raises rather than returns -> still no crash.
    class ExplodingProvider:
        name = "exploding"

        def investigate(self, context):
            raise RuntimeError("provider melted")

    report = run(build_adversarial_batch(), use_ai=True, provider=ExplodingProvider())
    assert all(o.decision.state == HUMAN_REVIEW_REQUIRED for o in report.outcomes)

    # (c) the evidence record simply does not exist -> abstain, do not invent.
    class GhostEvidenceProvider(HeuristicProvider):
        name = "ghost"

        def investigate(self, context):
            result = super().investigate(context)
            return result.model_copy(update={"candidate_evidence_ids": ["rfnd_DOES_NOT_EXIST"]})

    stripped = build_adversarial_batch()
    report = run(stripped, use_ai=True, provider=GhostEvidenceProvider())
    for outcome in report.outcomes:
        assert outcome.decision.state != AUTO_RESOLVED, outcome.verification.as_dict()
        assert ORPHAN_REFUND_ID not in outcome.verification.claimed_evidence_ids

    # (d) an out-of-taxonomy hypothesis must not validate at all.
    with pytest.raises(ValueError):
        InvestigationResult(
            hypothesis="the money was probably fine",
            reason="trust me",
            recommended_action="resolve",
        )


def test_5b_malformed_and_contested_evidence_degrade_safely():
    """Malformed model output, and two cases claiming one record, both abstain."""
    from ledgerguard.ai.schemas import InvestigationResult
    from ledgerguard.evidence.safety import AUTO_RESOLVED, HUMAN_REVIEW_REQUIRED
    from ledgerguard.evidence.verifier import (
        UNVERIFIED,
        VERIFIED,
        EvidenceCheck,
        VerificationOutcome,
        resolve_evidence_conflicts,
    )

    invalid = InvestigationResult.invalid("expecting ',' delimiter: line 3 column 9")
    assert invalid.hypothesis == "insufficient_evidence"
    assert invalid.recommended_action == "review"

    # Two verified cases claiming the same refund must both be downgraded,
    # regardless of which one was processed first.
    outcomes = {
        "caseA": VerificationOutcome(
            case_id="caseA",
            hypothesis="unlinked_partial_refund",
            verdict=VERIFIED,
            checks=[EvidenceCheck("E7_invariant_restored", "invariant", True, "")],
            claimed_evidence_ids=["rfnd_SHARED"],
        ),
        "caseB": VerificationOutcome(
            case_id="caseB",
            hypothesis="unlinked_partial_refund",
            verdict=VERIFIED,
            checks=[EvidenceCheck("E7_invariant_restored", "invariant", True, "")],
            claimed_evidence_ids=["rfnd_SHARED"],
        ),
    }
    resolve_evidence_conflicts(outcomes)
    assert outcomes["caseA"].verdict == UNVERIFIED
    assert outcomes["caseB"].verdict == UNVERIFIED
    assert "contested" in outcomes["caseA"].note
