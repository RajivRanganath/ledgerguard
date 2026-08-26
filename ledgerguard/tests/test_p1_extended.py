"""P1 extended coverage.

The five P0 tests protect the core argument. These protect the things most
likely to rot quietly underneath it: duplicate handling, timing windows,
generator reproducibility, and every way a model or an upstream record can be
malformed.

Nothing here touches the network. Providers are stubbed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from ledgerguard.ai.provider import HeuristicProvider
from ledgerguard.ai.schemas import (
    REASON_MAX_CHARS,
    InvestigationResult,
    InvestigatorOutput,
)
from ledgerguard.evidence.safety import AUTO_RESOLVED, HUMAN_REVIEW_REQUIRED
from ledgerguard.ledger.models import (
    BankEntry,
    Batch,
    CreditDebit,
    ExceptionType,
    Order,
    Payment,
    Settlement,
)
from ledgerguard.ledger.money import gst_on_fee, platform_fee, to_paise
from ledgerguard.pipeline import run
from ledgerguard.reconciliation.matcher import MATCHED, find_duplicates, reconcile_batch
from ledgerguard.synthetic.fault_injector import build_dataset, split
from ledgerguard.synthetic.generator import FaultClass

T0 = datetime(2026, 6, 10, 10, 0, 0)


def _cases_by_fault(dataset, results):
    fault = dataset.fault_class_by_payment()
    out: dict[str, list] = {}
    for r in results:
        out.setdefault(fault[r.payment_id], []).append(r)
    return out


# --------------------------------------------------------------- duplicates
def test_duplicate_payment_and_bank_entry_are_both_detected():
    """Both duplicate shapes must be caught, and neither may be double counted."""
    dataset = build_dataset()
    results, _ = reconcile_batch(dataset.batch(), dataset.lifecycle_by_payment())
    by_fault = _cases_by_fault(dataset, results)

    dupes = by_fault[FaultClass.F2_DUPLICATE_RECORD]
    assert dupes, "generator produced no duplicate cases"
    assert all(
        c.status == ExceptionType.DUPLICATE_RECORD.value for c in dupes
    ), [(c.payment_id, c.status) for c in dupes]

    # The injector produces two distinct duplicate shapes; both must be present,
    # otherwise a regression in one is invisible.
    reasons = {c.exception.evidence.get("reason", "") for c in dupes}
    assert any("payment_id appears more than once" in r for r in reasons), reasons
    assert any("share reference" in r for r in reasons), reasons

    # A duplicated payment must collapse into exactly one case, not two.
    batch = dataset.batch()
    payment_ids = [c.payment_id for c in results]
    assert len(payment_ids) == len(set(payment_ids)), "a payment produced two cases"
    assert len(batch.payments) > len(results), "no duplicate rows in the batch to collapse"


def test_find_duplicates_distinguishes_id_from_content_duplication():
    fee = platform_fee(100000)
    payment = Payment(
        payment_id="pay_D", order_id="ord_D", amount=100000,
        captured_at=T0, fee=fee, tax=gst_on_fee(fee), reference="PAY/ord_D",
    )
    entry = BankEntry(
        bank_entry_id="bnk_1", amount=90000, credit_or_debit=CreditDebit.CREDIT,
        date=T0, reference="STL/x",
    )
    twin = entry.model_copy(update={"bank_entry_id": "bnk_2"})

    dup = find_duplicates(Batch(payments=[payment, payment], bank_entries=[entry, twin]))
    assert dup.payment_ids == {"pay_D"}
    assert len(dup.bank_signatures) == 1
    assert sorted(next(iter(dup.bank_signatures.values()))) == ["bnk_1", "bnk_2"]

    # Same reference but a different amount is NOT a duplicate.
    other = entry.model_copy(update={"bank_entry_id": "bnk_3", "amount": 90001})
    assert not find_duplicates(Batch(bank_entries=[entry, other])).bank_signatures


# --------------------------------------------------------- delayed settlement
def test_delayed_settlement_is_timing_only_and_never_an_amount_exception():
    dataset = build_dataset()
    results, _ = reconcile_batch(dataset.batch(), dataset.lifecycle_by_payment())
    delayed = _cases_by_fault(dataset, results)[FaultClass.F5_DELAYED_SETTLEMENT]
    assert delayed

    for case in delayed:
        assert case.status == ExceptionType.DELAYED_SETTLEMENT.value
        failing = {i.name for i in case.failing_invariants()}
        # Only the window invariant may break. If an amount invariant also
        # broke, the classification above is hiding a real money discrepancy.
        assert failing == {"I5_settlement_within_window"}, failing
        assert case.exception.evidence["days_late"] >= 1
        assert case.exception.difference == 0, "amounts must reconcile exactly"


def test_settlement_window_boundaries_are_inclusive():
    """T+2 with one day tolerance: T+1 and T+3 hold, T+4 does not."""
    from ledgerguard.ledger.invariants import settlement_within_window
    from ledgerguard.ledger.shadow_ledger import ShadowLedger

    amount = 500000
    fee, tax = platform_fee(amount), gst_on_fee(platform_fee(amount))
    payment = Payment(
        payment_id="pay_W", order_id="ord_W", amount=amount,
        captured_at=T0, fee=fee, tax=tax, reference="PAY/ord_W",
    )
    ledger = ShadowLedger(Batch(payments=[payment]))
    expected = ledger.expected_for_payment(payment)

    def at(days: int):
        return Settlement(
            settlement_id="setl_W", merchant_id="m", payment_ids=["pay_W"],
            gross_amount=amount, fees=fee, tax=tax, refund_adjustments=0,
            net_amount=amount - fee - tax,
            settlement_date=T0 + timedelta(days=days), reference="STL/setl_W",
        )

    assert settlement_within_window(expected, at(1)).holds
    assert settlement_within_window(expected, at(2)).holds
    assert settlement_within_window(expected, at(3)).holds
    assert not settlement_within_window(expected, at(4)).holds
    assert not settlement_within_window(expected, at(0)).holds


# ------------------------------------------------------ generator determinism
def test_generator_is_reproducible_and_the_split_is_stable():
    a, b = build_dataset(), build_dataset()
    assert a.batch().model_dump_json() == b.batch().model_dump_json()
    assert {k: v.as_dict() for k, v in a.ground_truth.items()} == {
        k: v.as_dict() for k, v in b.ground_truth.items()
    }

    dev_a, hold_a = split(a)
    dev_b, hold_b = split(b)
    assert [x.lifecycle_id for x in hold_a.lifecycles] == [
        x.lifecycle_id for x in hold_b.lifecycles
    ]
    assert not (
        {x.lifecycle_id for x in dev_a.lifecycles}
        & {x.lifecycle_id for x in hold_a.lifecycles}
    ), "dev and holdout overlap"

    # A different seed must actually produce different data.
    assert build_dataset(seed=1).batch().model_dump_json() != a.batch().model_dump_json()

    # Every fault class must survive into the holdout, or the safety metric is
    # measured on an empty set.
    classes = {gt.fault_class for gt in hold_a.ground_truth.values()}
    assert len(classes - {FaultClass.NONE}) == 6, classes

    # Paired F6 lifecycles must never straddle the split.
    hold_ids = {x.lifecycle_id for x in hold_a.lifecycles}
    for lc_id, gt in a.ground_truth.items():
        if gt.paired_with:
            assert (lc_id in hold_ids) == (gt.paired_with in hold_ids), lc_id


# --------------------------------------------------------- malformed AI output
@pytest.mark.parametrize(
    "raw",
    [
        "",                                   # empty
        "not json at all",                    # prose
        '{"hypothesis": "unlinked_partial',   # truncated
        '{"hypothesis": "make_it_go_away", "reason": "x", "required_evidence": [],'
        ' "candidate_evidence_ids": [], "recommended_action": "resolve"}',  # off-taxonomy
        '{"reason": "x"}',                    # missing required fields
    ],
)
def test_malformed_model_output_becomes_invalid_never_a_resolution(raw):
    from ledgerguard.ai.openai_compatible import _parse

    result = _parse(raw, "test-model")
    assert result.source == "invalid_response"
    assert result.hypothesis == "insufficient_evidence"
    assert result.recommended_action == "review"
    assert result.candidate_evidence_ids == []


def test_fenced_json_is_recovered_and_long_reasons_are_truncated():
    from ledgerguard.ai.openai_compatible import _parse

    payload = (
        '{"hypothesis": "unlinked_partial_refund", "reason": "%s", '
        '"required_evidence": [], "candidate_evidence_ids": ["rfnd_1"], '
        '"recommended_action": "resolve"}' % ("x" * 5000)
    )
    result = _parse("```json\n" + payload + "\n```", "test-model")
    assert result.source == "model"
    assert result.hypothesis == "unlinked_partial_refund"
    assert len(result.reason) == REASON_MAX_CHARS
    assert result.reason.endswith("...")


def test_investigator_output_rejects_unbounded_candidate_lists():
    with pytest.raises(ValidationError):
        InvestigationResult(
            hypothesis="unlinked_partial_refund",
            reason="x",
            candidate_evidence_ids=[f"rfnd_{i}" for i in range(11)],
            recommended_action="resolve",
        )


# --------------------------------------------------------------- model timeout
def _stub_provider(exc: Exception):
    from ledgerguard.ai.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        preset="openai_compatible",
        models=["stub-model"],
        api_key="k",
        base_url="http://stub.invalid/v1",
    )
    provider.max_retries = 0                 # keep the test instant

    class _Boom:
        def post(self, *a, **kw):
            raise exc

    provider._client = _Boom()
    return provider


def test_model_timeout_degrades_to_review_and_keeps_the_batch_running():
    from ledgerguard.synthetic.adversarial import build_adversarial_batch

    provider = _stub_provider(httpx.TimeoutException("timed out"))
    report = run(build_adversarial_batch(), use_ai=True, provider=provider)

    assert len(report.outcomes) == 2, "the batch must still complete"
    for outcome in report.outcomes:
        assert outcome.investigation.result.source == "unavailable"
        assert "timeout" in outcome.investigation.result.error.lower()
        assert outcome.decision.state == HUMAN_REVIEW_REQUIRED
        assert outcome.decision.state != AUTO_RESOLVED


def test_connection_failure_is_reported_as_transport_not_as_bad_output():
    provider = _stub_provider(httpx.ConnectError("refused"))
    result = provider.investigate({"exception": {"type": "X"}})
    assert result.source == "unavailable"
    assert "transport error" in result.error


# --------------------------------------------------------------- missing fields
def test_malformed_records_fail_loudly_at_ingestion():
    with pytest.raises(ValidationError):
        Order(order_id="o", merchant_id="m", created_at=T0, customer_reference="c")

    with pytest.raises(ValidationError):
        Order(
            order_id="o", merchant_id="m", amount=1, created_at=T0,
            customer_reference="c", surprise_field="!",
        )

    with pytest.raises(ValidationError):
        Payment(
            payment_id="p", order_id="o", amount="not-a-number",
            captured_at=T0, fee=0, tax=0, reference="r",
        )


def test_a_payment_with_no_capture_timestamp_is_handled_not_crashed():
    """Timing cannot be decided without a capture time; it must not be assumed."""
    amount = to_paise("1000.00")
    fee, tax = platform_fee(amount), gst_on_fee(platform_fee(amount))
    order = Order(
        order_id="ord_N", merchant_id="m", amount=amount, created_at=T0,
        customer_reference="CUST-N",
    )
    payment = Payment(
        payment_id="pay_N", order_id="ord_N", amount=amount, captured_at=None,
        fee=fee, tax=tax, reference="PAY/ord_N",
    )
    settlement = Settlement(
        settlement_id="setl_N", merchant_id="m", payment_ids=["pay_N"],
        gross_amount=amount, fees=fee, tax=tax, refund_adjustments=0,
        net_amount=amount - fee - tax, settlement_date=T0 + timedelta(days=2),
        reference="STL/setl_N",
    )
    bank = BankEntry(
        bank_entry_id="bnk_N", amount=settlement.net_amount,
        credit_or_debit=CreditDebit.CREDIT, date=settlement.settlement_date,
        reference="STL/setl_N",
    )
    batch = Batch(
        orders=[order], payments=[payment], settlements=[settlement],
        bank_entries=[bank],
    )

    report = run(batch, use_ai=True, provider=HeuristicProvider())
    outcome = report.outcomes[0]
    assert outcome.state != MATCHED, "an undecidable window must not pass silently"
    window = next(
        i for i in outcome.case.invariants if i.name == "I5_settlement_within_window"
    )
    assert not window.holds
    assert "no captured_at" in window.detail
    assert outcome.decision.state != AUTO_RESOLVED


def test_unknown_exception_type_reaches_unresolved_not_a_close():
    """An exception with no handler must land in UNRESOLVED, never AUTO_RESOLVED."""
    from ledgerguard.evidence.safety import UNRESOLVED, decide
    from ledgerguard.reconciliation.matcher import CaseResult
    from ledgerguard.reconciliation.exceptions import make_exception
    from ledgerguard.ledger.shadow_ledger import ExpectedSettlement

    exc = make_exception(
        case_id="pay_U", lifecycle_id="LC", transaction_ids=["pay_U"],
        exception_type=ExceptionType.AMBIGUOUS_REFERENCE,
        detected_by="deterministic_matcher",
        expected_value=100, observed_value=90, evidence={},
    )
    case = CaseResult(
        case_id="pay_U", payment_id="pay_U", order_id="ord_U",
        status=exc.exception_type.value,
        expected=ExpectedSettlement("pay_U", 0, 0, 0, 0, 0, 0),
        settlement=None, exception=exc,
    )
    decision = decide(case, None, None)
    assert decision.state == UNRESOLVED
    assert decision.state != AUTO_RESOLVED
