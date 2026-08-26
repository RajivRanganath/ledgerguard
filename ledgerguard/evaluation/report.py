"""Exportable reconciliation report.

The unresolved-exceptions CSV is the honest failure surface. This is its
counterpart: the full reconciliation, every case, in a form a finance team can
open in a spreadsheet and a reviewer can diff.

Three files:

  reconciliation_report.csv   one row per case, all states
  reconciliation_report.md    a human-readable summary with the money totals
  evidence_ledger.csv         one row per evidence check the gate ran

The third is the one that matters for an audit. Every automatic closure in this
system is backed by named checks against named records, and this writes them out
so that "why was this closed" is answerable from a file rather than from a
running process.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ..evidence.safety import AUTO_RESOLVED
from ..ledger.money import to_rupees_str
from ..pipeline import RunReport

OUTPUT_DIR = Path(__file__).parent / "outputs"


@dataclass
class ReportTotals:
    cases: int = 0
    captured_paise: int = 0
    matched: int = 0
    auto_resolved: int = 0
    escalated: int = 0
    exception_exposure_paise: int = 0
    unresolved_exposure_paise: int = 0
    evidence_checks: int = 0
    evidence_checks_passed: int = 0


def write_reconciliation_report(
    report: RunReport, output_dir: Path = OUTPUT_DIR
) -> ReportTotals:
    output_dir.mkdir(parents=True, exist_ok=True)
    totals = ReportTotals()

    with (output_dir / "reconciliation_report.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "case_id", "order_id", "payment_id", "state", "exception_type",
            "captured_inr", "expected_net_inr", "observed_net_inr",
            "difference_inr", "settlement_id", "settlement_date",
            "invariants_failed", "secondary_findings", "investigator",
            "hypothesis", "verdict", "verification_score", "decision_reason",
            "suggested_action",
        ])
        for outcome in report.outcomes:
            case = outcome.case
            payment = report.ledger.payments_by_id[case.payment_id]
            exc = case.exception
            inv = outcome.investigation
            ver = outcome.verification

            totals.cases += 1
            totals.captured_paise += payment.amount
            if outcome.state == "MATCHED":
                totals.matched += 1
            elif outcome.state == AUTO_RESOLVED:
                totals.auto_resolved += 1
            else:
                totals.escalated += 1
                totals.unresolved_exposure_paise += outcome.exposure_paise
            if exc:
                totals.exception_exposure_paise += outcome.exposure_paise
            if ver:
                totals.evidence_checks += len(ver.checks)
                totals.evidence_checks_passed += ver.passed_count

            writer.writerow([
                case.case_id,
                case.order_id or "",
                case.payment_id,
                outcome.state,
                exc.exception_type.value if exc else "",
                to_rupees_str(payment.amount),
                to_rupees_str(case.expected.net_amount),
                to_rupees_str(case.settlement.net_amount) if case.settlement else "",
                to_rupees_str(exc.difference) if exc and exc.difference is not None else "",
                case.settlement.settlement_id if case.settlement else "",
                case.settlement.settlement_date.date().isoformat() if case.settlement else "",
                "; ".join(i.name for i in case.failing_invariants()),
                "; ".join(exc.evidence.get("secondary_findings", [])) if exc else "",
                (inv.result.model_name or inv.result.source) if inv else "",
                inv.result.hypothesis if inv else "",
                ver.verdict if ver else "",
                ver.verification_score if ver and ver.checks else "",
                outcome.decision.reason if outcome.decision else "",
                outcome.decision.suggested_action if outcome.decision else "",
            ])

    # --- the audit trail: every check, against every record ------------------
    with (output_dir / "evidence_ledger.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "case_id", "hypothesis", "verdict", "check", "kind", "passed",
            "detail", "claimed_evidence_ids", "final_state",
        ])
        for outcome in report.outcomes:
            ver = outcome.verification
            if ver is None or not ver.checks:
                continue
            for check in ver.checks:
                writer.writerow([
                    outcome.case_id, ver.hypothesis, ver.verdict, check.name,
                    check.kind, "PASS" if check.passed else "FAIL", check.detail,
                    " ".join(ver.claimed_evidence_ids), outcome.state,
                ])

    md = [
        "# Reconciliation report",
        "",
        f"System: {report.system}. Investigator: `{report.provider_name}`.",
        "",
        "| | |",
        "|---|---|",
        f"| Cases reconciled | {totals.cases} |",
        f"| Captured value | INR {to_rupees_str(totals.captured_paise)} |",
        f"| Matched, no exception | {totals.matched} |",
        f"| Exceptions closed automatically | {totals.auto_resolved} |",
        f"| Escalated to a human | {totals.escalated} |",
        f"| Value under exception | INR {to_rupees_str(totals.exception_exposure_paise)} |",
        f"| Value left unresolved | INR {to_rupees_str(totals.unresolved_exposure_paise)} |",
        f"| Evidence checks run | {totals.evidence_checks} |",
        f"| Evidence checks passed | {totals.evidence_checks_passed} |",
        "",
        "Every automatic closure above is backed by the named checks in",
        "`evidence_ledger.csv`. Nothing is closed on a model's say-so, and no",
        "money is moved by this system under any state.",
        "",
        "Files: `reconciliation_report.csv` (one row per case),",
        "`evidence_ledger.csv` (one row per check), `unresolved_exceptions.csv`",
        "(the cases that stayed open, with what was missing).",
    ]
    (output_dir / "reconciliation_report.md").write_text("\n".join(md) + "\n")
    return totals


def main(argv: list[str] | None = None) -> int:
    import argparse

    from ..ai.provider import get_provider
    from ..pipeline import run
    from ..synthetic.fault_injector import build_dataset, split

    parser = argparse.ArgumentParser(description="Export a reconciliation report")
    parser.add_argument("--provider", default=None)
    args = parser.parse_args(argv)

    full = build_dataset()
    _dev, holdout = split(full)
    report = run(
        holdout.batch(), use_ai=True, provider=get_provider(args.provider),
        lifecycle_by_payment=holdout.lifecycle_by_payment(),
    )
    totals = write_reconciliation_report(report)
    print((OUTPUT_DIR / "reconciliation_report.md").read_text())
    print(f"Artifacts written to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
