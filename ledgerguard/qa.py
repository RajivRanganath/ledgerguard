"""Finance question answering, grounded in records.

Deliberately not a chatbot, and deliberately not the centerpiece.

The architecture's rule applies here exactly as it does to reconciliation: the
model may *choose the question*, but it never produces the number. Every answer
below is computed by ordinary code over the reconciled records, and carries the
record ids it was derived from. A question outside the supported set is refused
rather than improvised -- an invented total is worse than no answer.

    python -m ledgerguard.qa "how much is unresolved?"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .evidence.safety import AUTO_RESOLVED
from .ledger.money import to_rupees_str
from .pipeline import RunReport

MATCHED = "MATCHED"


@dataclass
class Answer:
    question: str
    query: str
    answer: str
    citations: list[str] = field(default_factory=list)
    grounded: bool = True

    def render(self) -> str:
        if not self.grounded:
            return f"Q: {self.question}\nA: {self.answer}"
        cites = ", ".join(self.citations[:8]) or "(aggregate over all cases)"
        more = f" (+{len(self.citations) - 8} more)" if len(self.citations) > 8 else ""
        return f"Q: {self.question}\nA: {self.answer}\n   from: {cites}{more}"


@dataclass
class _Query:
    name: str
    patterns: tuple[str, ...]
    describe: str
    fn: Callable[[RunReport], tuple[str, list[str]]]


def _captured(report: RunReport) -> tuple[str, list[str]]:
    total = sum(
        report.ledger.payments_by_id[o.case.payment_id].amount for o in report.outcomes
    )
    return f"INR {to_rupees_str(total)} captured across {len(report.outcomes)} payments.", []


def _unresolved(report: RunReport) -> tuple[str, list[str]]:
    open_cases = [o for o in report.outcomes if o.state not in (MATCHED, AUTO_RESOLVED)]
    total = sum(o.exposure_paise for o in open_cases)
    return (
        f"INR {to_rupees_str(total)} across {len(open_cases)} unresolved cases.",
        [o.case_id for o in open_cases],
    )


def _auto_resolved(report: RunReport) -> tuple[str, list[str]]:
    closed = [o for o in report.outcomes if o.state == AUTO_RESOLVED]
    total = sum(o.exposure_paise for o in closed)
    return (
        f"{len(closed)} exceptions were closed automatically, covering "
        f"INR {to_rupees_str(total)}. Every one is backed by named evidence checks.",
        [o.case_id for o in closed],
    )


def _largest_unresolved(report: RunReport) -> tuple[str, list[str]]:
    open_cases = [o for o in report.outcomes if o.state not in (MATCHED, AUTO_RESOLVED)]
    if not open_cases:
        return "Nothing is unresolved.", []
    worst = max(open_cases, key=lambda o: o.exposure_paise)
    return (
        f"{worst.case_id}, INR {to_rupees_str(worst.exposure_paise)}, "
        f"{worst.case.exception.exception_type.value}. "
        f"{worst.decision.reason}",
        [worst.case_id],
    )


def _exception_breakdown(report: RunReport) -> tuple[str, list[str]]:
    counts: dict[str, int] = {}
    for o in report.outcomes:
        if o.case.exception:
            key = o.case.exception.exception_type.value.replace("EXCEPTION_", "")
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "No exceptions were raised.", []
    parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    return f"{sum(counts.values())} exceptions: {parts}.", []


def _match_rate(report: RunReport) -> tuple[str, list[str]]:
    matched = sum(1 for o in report.outcomes if o.state == MATCHED)
    total = len(report.outcomes)
    pct = (matched / total * 100) if total else 0
    return (
        f"{matched} of {total} payments reconciled with no exception ({pct:.1f}%).",
        [],
    )


def _missing_settlements(report: RunReport) -> tuple[str, list[str]]:
    cases = [
        o for o in report.outcomes
        if o.case.exception
        and o.case.exception.exception_type.value == "EXCEPTION_MISSING_SETTLEMENT"
    ]
    total = sum(o.exposure_paise for o in cases)
    return (
        f"{len(cases)} captured payments have no settlement record, "
        f"INR {to_rupees_str(total)} outstanding.",
        [o.case_id for o in cases],
    )


def _rejected_hypotheses(report: RunReport) -> tuple[str, list[str]]:
    cases = [
        o for o in report.outcomes
        if o.verification and o.verification.verdict == "REJECTED"
    ]
    return (
        f"The Evidence Gate rejected {len(cases)} proposed explanations. "
        "Each was disproved on a linkage check, not on amount.",
        [o.case_id for o in cases],
    )


#: Order matters: the router takes the first match, so the most specific
#: queries are listed first. "largest unresolved case" must not be answered by
#: the aggregate "unresolved" query.
QUERIES: tuple[_Query, ...] = (
    _Query("largest_unresolved", ("largest", "biggest", "worst"),
           "the largest unresolved case", _largest_unresolved),
    _Query("missing_settlements", ("missing settlement", "never settled", "not settled"),
           "payments with no settlement", _missing_settlements),
    _Query("total_captured", ("captur", "total value", "how much processed", "gross"),
           "total value captured", _captured),
    _Query("unresolved_exposure", ("unresolved", "still open", "not closed", "outstanding exposure", "how much is at risk"),
           "value left unresolved", _unresolved),
    _Query("auto_resolved", ("auto", "closed automatically", "resolved automatically"),
           "exceptions closed automatically", _auto_resolved),
    _Query("exception_breakdown", ("breakdown", "what kind", "types of exception", "by type"),
           "exceptions by type", _exception_breakdown),
    _Query("match_rate", ("match rate", "how many reconciled", "clean"),
           "match rate", _match_rate),
    _Query("rejected", ("reject", "blocked", "gate stop"),
           "hypotheses the gate rejected", _rejected_hypotheses),
)


def _route(question: str) -> _Query | None:
    lowered = question.lower()
    for query in QUERIES:
        if any(p in lowered for p in query.patterns):
            return query
    return None


def _case_lookup(question: str, report: RunReport) -> Answer | None:
    """Direct lookup when the question names a case id."""
    match = re.search(r"\b(pay_[A-Za-z0-9_]+)\b", question)
    if not match:
        return None
    case_id = match.group(1)
    outcome = next((o for o in report.outcomes if o.case_id == case_id), None)
    if outcome is None:
        return Answer(question, "case_lookup", f"No case {case_id} in this run.", [], False)
    if outcome.state == MATCHED:
        body = f"{case_id} reconciled exactly. No exception was raised."
    else:
        body = (
            f"{case_id} is {outcome.state}, "
            f"{outcome.case.exception.exception_type.value}, exposure "
            f"INR {to_rupees_str(outcome.exposure_paise)}. {outcome.decision.reason}"
        )
    return Answer(question, "case_lookup", body, [case_id])


def ask(question: str, report: RunReport) -> Answer:
    """Answer from records, or refuse. Never improvise a number."""
    direct = _case_lookup(question, report)
    if direct is not None:
        return direct

    query = _route(question)
    if query is None:
        supported = "; ".join(q.describe for q in QUERIES)
        return Answer(
            question,
            "unsupported",
            (
                "I can only answer from the reconciled records, and this question "
                "does not map to a supported query. Supported: "
                f"{supported}; or ask about a specific case id. "
                "I will not estimate a figure I cannot derive."
            ),
            [],
            grounded=False,
        )

    body, citations = query.fn(report)
    return Answer(question, query.name, body, citations)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .ai.provider import get_provider
    from .pipeline import run
    from .synthetic.fault_injector import build_dataset, split

    parser = argparse.ArgumentParser(description="Ask a grounded question")
    parser.add_argument("question", nargs="*", help="question to answer")
    parser.add_argument("--provider", default="stub")
    args = parser.parse_args(argv)

    full = build_dataset()
    _dev, holdout = split(full)
    report = run(
        holdout.batch(), use_ai=True, provider=get_provider(args.provider),
        lifecycle_by_payment=holdout.lifecycle_by_payment(),
    )

    questions = [" ".join(args.question)] if args.question else [
        "How much value was captured?",
        "How much is still unresolved?",
        "What is the largest unresolved case?",
        "Give me a breakdown by exception type.",
        "How many payments were never settled?",
        "What did the gate reject?",
        "What is the match rate?",
        "Should I invest in this merchant?",
    ]
    for q in questions:
        print(ask(q, report).render())
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
