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


def test_a_shared_id_with_different_content_is_a_conflict_not_a_duplicate():
    """The claim "proven by identifier AND content match" has to be true.

    Two rows carrying one payment_id but disagreeing on the amount are not a
    re-ingestion, they are two contradictory versions of one payment. Closing
    that as a duplicate would suppress a row on the strength of a content check
    that failed -- an unproven auto close on the deterministic path, which is
    the exact failure the whole system exists to prevent.
    """
    dataset = build_dataset()
    _dev, holdout = split(dataset)
    batch = holdout.batch()

    clean = next(
        p for p in batch.payments
        if holdout.fault_class_by_payment()[p.payment_id] == FaultClass.NONE
    )
    conflicting = clean.model_copy(update={"amount": clean.amount * 3})
    tampered = batch.model_copy(
        update={"payments": list(batch.payments) + [conflicting]}
    )

    dup = find_duplicates(tampered)
    assert clean.payment_id in dup.payment_conflicts
    assert clean.payment_id not in dup.payment_ids, "a conflict is not a duplicate"

    outcome = next(
        o for o in run(tampered, use_ai=False).outcomes
        if o.case.payment_id == clean.payment_id
    )
    assert outcome.state == HUMAN_REVIEW_REQUIRED
    assert outcome.decision.missing_evidence, "the human must be told what is missing"
    assert "disagree on content" in outcome.decision.reason

    # An exact re-ingestion of the same row is still provable, and still closes.
    exact = batch.model_copy(update={"payments": list(batch.payments) + [clean]})
    dup_exact = find_duplicates(exact)
    assert clean.payment_id in dup_exact.payment_ids
    assert not dup_exact.payment_conflicts
    closed = next(
        o for o in run(exact, use_ai=False).outcomes
        if o.case.payment_id == clean.payment_id
    )
    assert closed.state == AUTO_RESOLVED


def test_two_settlements_that_disagree_are_never_collapsed_into_one():
    """Same payment, two settlements, different nets. Which is real is unproven."""
    dataset = build_dataset()
    _dev, holdout = split(dataset)
    batch = holdout.batch()

    original = batch.settlements[0]
    rival = original.model_copy(
        update={
            "settlement_id": original.settlement_id + "X",
            "net_amount": original.net_amount - 5000,
        }
    )
    tampered = batch.model_copy(
        update={"settlements": list(batch.settlements) + [rival]}
    )

    outcome = next(
        o for o in run(tampered, use_ai=False).outcomes
        if o.case.payment_id == original.payment_ids[0]
    )
    assert outcome.case.exception.evidence["content_conflict"] == "settlement"
    assert outcome.state == HUMAN_REVIEW_REQUIRED


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


@pytest.mark.parametrize(
    "returned",
    [
        {"hypothesis": "unlinked_partial_refund", "recommended_action": "resolve"},
        "unlinked_partial_refund",
        None,
    ],
    ids=["dict", "string", "none"],
)
def test_a_provider_returning_the_wrong_type_cannot_take_down_the_batch(returned):
    """A provider that *returns* garbage, rather than raising it.

    The chain type-checks its members, but a single provider selected directly
    (`LEDGERGUARD_PROVIDER=groq`) does not go through the chain. Without a guard
    the admissibility filter dereferences whatever came back and the whole run
    dies -- the one thing no provider is allowed to do to the batch.
    """
    from ledgerguard.synthetic.adversarial import build_adversarial_batch

    class WrongType:
        name = "wrong_type"

        def investigate(self, context):
            return returned

    report = run(build_adversarial_batch(), use_ai=True, provider=WrongType())

    assert len(report.outcomes) == 2, "the batch must still complete"
    for outcome in report.outcomes:
        assert outcome.investigation.result.source == "unavailable"
        assert "expected InvestigationResult" in outcome.investigation.result.error
        assert outcome.decision.state == HUMAN_REVIEW_REQUIRED


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


def test_frozen_holdout_mismatch_is_refused(tmp_path, monkeypatch):
    """A holdout that drifted must abort scoring, not be silently re-frozen.

    The frozen manifest is what makes the benchmark a measurement rather than a
    claim. If the generator changes and this check stops firing, every published
    number quietly becomes a score against a different dataset.
    """
    from ledgerguard.evaluation import benchmark as bm

    monkeypatch.setattr(bm, "MANIFEST_DIR", tmp_path)
    full = build_dataset(seed=424242, count=40)
    _dev, holdout = split(full)

    first = bm.freeze_or_verify_holdout(holdout, 424242, 40)
    assert first["holdout_batch_sha256"]
    # Re-verifying the identical holdout is a no-op, not a rewrite.
    assert bm.freeze_or_verify_holdout(holdout, 424242, 40) == first

    drifted = split(build_dataset(seed=525252, count=40))[1]
    with pytest.raises(SystemExit) as excinfo:
        bm.freeze_or_verify_holdout(drifted, 424242, 40)
    assert "FROZEN HOLDOUT MISMATCH" in str(excinfo.value)


def test_hypothesis_must_be_admissible_for_the_exception_it_would_close():
    """A passing evidence battery must not close an exception it never explains.

    The refund battery reconstructs the settlement net. A bank-side discrepancy
    is a different discrepancy, so `unlinked_partial_refund` must not be
    verifiable against it even if every individual check would pass.
    """
    from ledgerguard.evidence.verifier import UNVERIFIED, verify
    from ledgerguard.ledger.shadow_ledger import ExpectedSettlement, ShadowLedger
    from ledgerguard.reconciliation.exceptions import ADMISSIBLE_HYPOTHESES, make_exception
    from ledgerguard.reconciliation.matcher import CaseResult

    assert "unlinked_partial_refund" not in ADMISSIBLE_HYPOTHESES[ExceptionType.BANK_MISMATCH]

    exc = make_exception(
        case_id="pay_B", lifecycle_id="LC", transaction_ids=["pay_B"],
        exception_type=ExceptionType.BANK_MISMATCH,
        detected_by="deterministic_matcher",
        expected_value=1000, observed_value=900, evidence={},
    )
    case = CaseResult(
        case_id="pay_B", payment_id="pay_B", order_id="ord_B",
        status=exc.exception_type.value,
        expected=ExpectedSettlement("pay_B", 0, 0, 0, 0, 0, 0),
        settlement=None, exception=exc,
    )
    result = InvestigationResult(
        hypothesis="unlinked_partial_refund",
        reason="the amounts line up",
        candidate_evidence_ids=["rfnd_X"],
        recommended_action="resolve",
        source="model",
    )
    outcome = verify(ShadowLedger(Batch()), case, result)
    assert outcome.verdict == UNVERIFIED
    assert "not an admissible explanation" in outcome.note
    # The battery must not have run at all -- no checks were weighed.
    assert outcome.checks == []


def test_a_broken_provider_is_reported_not_silently_dropped():
    """A configured-but-broken provider must not look like an absent one.

    A missing key is a legitimate way to run on a subset and stays quiet. A
    provider that raises for any other reason is a defect, and a benchmark that
    silently ran on fewer providers than intended is not the run it claims.
    """
    from ledgerguard.ai import provider as prov

    def _boom(kind: str):
        if kind == "groq":
            raise AttributeError("simulated defect")
        raise prov.ProviderNotConfigured(f"{kind} is not configured")

    original = prov._build
    try:
        prov._build = _boom
        chain = prov.build_chain(["groq", "gemini"])
    finally:
        prov._build = original

    assert chain == []
    assert [e["provider"] for e in prov.CHAIN_BUILD_ERRORS] == ["groq"]
    assert "AttributeError: simulated defect" in prov.CHAIN_BUILD_ERRORS[0]["error"]


# ------------------------------------------- real transport failures, no stubs
#
# The tests above replace `provider._client` with an object that raises, which
# proves the handler but never runs httpx, the configured timeout, or
# `_post_with_retries`. These drive the real client against a real socket, so a
# regression in the actual request path cannot pass by raising the right
# exception type in a stub.
class _FakeUpstream:
    """A real HTTP server that misbehaves in one specific way."""

    def __init__(self, mode: str):
        import http.server
        import threading

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):                       # noqa: N802 (stdlib name)
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                if outer.mode == "hang":
                    import time as _t
                    _t.sleep(30)                     # outlive any sane timeout
                    return
                if outer.mode == "not_json":
                    body = b"<html>502 Bad Gateway</html>"
                elif outer.mode == "wrong_envelope":
                    body = b'{"unexpected": "shape"}'
                else:                                # garbage where JSON belongs
                    body = (
                        b'{"choices":[{"message":{"content":'
                        b'"Sure! I think this is a refund, trust me."}}]}'
                    )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):               # keep pytest output clean
                pass

        self.mode = mode
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _live_provider(url: str, timeout: float = 30.0):
    from ledgerguard.ai.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        preset="openai_compatible",
        models=["fake-model"],
        api_key="k",
        base_url=url,
        timeout=timeout,
    )
    provider.max_retries = 0                         # one attempt, keep it fast
    return provider


def test_a_real_hanging_provider_times_out_and_the_batch_survives():
    """Force a real timeout against a real socket, not an injected exception.

    The server accepts the request and then never answers. This exercises the
    configured httpx timeout and `_post_with_retries`, which the stub-based
    timeout test above cannot reach.
    """
    from ledgerguard.synthetic.adversarial import build_adversarial_batch

    upstream = _FakeUpstream("hang")
    try:
        provider = _live_provider(upstream.url, timeout=0.5)
        report = run(build_adversarial_batch(), use_ai=True, provider=provider)
    finally:
        upstream.close()

    assert len(report.outcomes) == 2, "the batch must still complete"
    for outcome in report.outcomes:
        assert outcome.investigation.result.source == "unavailable"
        assert "timeout" in outcome.investigation.result.error.lower()
        assert outcome.decision.state == HUMAN_REVIEW_REQUIRED
        assert outcome.decision.state != AUTO_RESOLVED


@pytest.mark.parametrize("mode", ["not_json", "wrong_envelope", "garbage_content"])
def test_a_real_malformed_response_never_becomes_a_resolution(mode):
    """A 200 carrying unusable content must abstain, not misparse.

    Three real shapes seen from live routers: an HTML error page served as 200,
    a JSON body of the wrong shape, and a correct envelope whose content is
    prose instead of the requested JSON.
    """
    upstream = _FakeUpstream(mode)
    try:
        provider = _live_provider(upstream.url)
        result = provider.investigate({"exception": {"type": "X"}})
    finally:
        upstream.close()

    assert result.source in ("invalid_response", "unavailable")
    assert result.hypothesis == "insufficient_evidence"
    assert result.recommended_action == "review"
    assert result.candidate_evidence_ids == []
    assert result.error


def test_a_real_malformed_response_degrades_the_whole_batch_to_review():
    """The batch-level counterpart: bad output must not close anything."""
    from ledgerguard.synthetic.adversarial import build_adversarial_batch

    upstream = _FakeUpstream("garbage_content")
    try:
        provider = _live_provider(upstream.url)
        report = run(build_adversarial_batch(), use_ai=True, provider=provider)
    finally:
        upstream.close()

    assert len(report.outcomes) == 2
    for outcome in report.outcomes:
        assert outcome.decision.state != AUTO_RESOLVED
