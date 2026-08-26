"""Empirical calibration of the Verification Score.

The dashboard shows "5 of 7 evidence checks passed" instead of a model
confidence number, on the argument that a transparent count is more trustworthy
than a generated probability. That is an argument, not evidence. This measures
it.

Two things are calibrated against the same ground truth:

  * the **investigator's own request** -- how often is "please resolve this"
    actually right?
  * the **Verification Score** -- does a higher count of passed checks actually
    predict a correct closure?

Run on the development split. The holdout is not used, because a calibration
curve fitted on the holdout would contaminate every other number in the project.
Nothing here tunes the system; it only reports.

    python -m ledgerguard.evaluation.calibration
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from ..ai.provider import get_provider
from ..pipeline import run
from ..synthetic.fault_injector import build_dataset, split
from ..synthetic.generator import Dataset

OUTPUT_DIR = Path(__file__).parent / "outputs"
GT_AUTO = "AUTO_RESOLVED"


@dataclass
class Bucket:
    label: str
    n: int
    closing_would_be_correct: int
    correct_rate: float


def _truth(dataset: Dataset) -> dict:
    return {
        lc.payment.payment_id: dataset.ground_truth[lc.lifecycle_id]
        for lc in dataset.lifecycles
    }


def _bucketise(pairs: dict[str, list[bool]]) -> list[Bucket]:
    out = []
    for label in sorted(pairs, reverse=True):
        values = pairs[label]
        correct = sum(1 for v in values if v)
        out.append(
            Bucket(
                label=label,
                n=len(values),
                closing_would_be_correct=correct,
                correct_rate=round(correct / len(values), 4) if values else 0.0,
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verification Score calibration")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--count", type=int, default=320)
    args = parser.parse_args(argv)

    full = build_dataset(seed=args.seed, count=args.count)
    dev, _holdout = split(full)
    provider = get_provider(args.provider)

    report = run(
        dev.batch(), use_ai=True, provider=provider,
        lifecycle_by_payment=dev.lifecycle_by_payment(),
    )
    truth = _truth(dev)

    by_score: dict[str, list[bool]] = defaultdict(list)
    by_request: dict[str, list[bool]] = defaultdict(list)
    by_verdict: dict[str, list[bool]] = defaultdict(list)

    for outcome in report.outcomes:
        if outcome.investigation is None or outcome.verification is None:
            continue
        gt = truth[outcome.case.payment_id]
        # The counterfactual: if this case were closed now, would that be right?
        closing_correct = gt.expected_disposition == GT_AUTO

        ver = outcome.verification
        label = (
            f"{ver.passed_count}/{ver.total_count}" if ver.total_count else "no checks"
        )
        by_score[label].append(closing_correct)
        by_request[outcome.investigation.result.recommended_action].append(closing_correct)
        by_verdict[ver.verdict].append(closing_correct)

    score_buckets = _bucketise(by_score)
    request_buckets = _bucketise(by_request)
    verdict_buckets = _bucketise(by_verdict)

    def table(title: str, buckets: list[Bucket], first_col: str) -> list[str]:
        rows = [
            "",
            f"## {title}",
            "",
            f"| {first_col} | cases | closing would be correct | rate |",
            "|---|---|---|---|",
        ]
        rows += [
            f"| `{b.label}` | {b.n} | {b.closing_would_be_correct} | {b.correct_rate:.1%} |"
            for b in buckets
        ]
        return rows

    investigated = sum(b.n for b in score_buckets)
    lines = [
        "# Calibration of the Verification Score",
        "",
        f"Development split, {len(dev.lifecycles)} lifecycles, {investigated} investigated cases.",
        f"Investigator: `{report.provider_name}`.",
        "",
        "\"Closing would be correct\" is the counterfactual: if the case were closed",
        "at this point, would ground truth agree? Nothing here tunes the system.",
    ]
    lines += table("The investigator's own request", request_buckets, "model asked to")
    lines += table("The Verification Score", score_buckets, "checks passed")
    lines += table("The gate's verdict", verdict_buckets, "verdict")

    resolve = next((b for b in request_buckets if b.label == "resolve"), None)
    full_score = next((b for b in score_buckets if b.label.startswith("7/7")), None)
    lines += ["", "## Reading it", ""]
    if resolve:
        lines.append(
            f"- When the investigator asked to resolve, closing was correct "
            f"**{resolve.correct_rate:.1%}** of the time ({resolve.closing_would_be_correct}"
            f"/{resolve.n}). Its request is a hypothesis, not a probability, and the"
            f" numbers say so."
        )
    if full_score:
        lines.append(
            f"- When every evidence check passed (`{full_score.label}`), closing was correct "
            f"**{full_score.correct_rate:.1%}** of the time "
            f"({full_score.closing_would_be_correct}/{full_score.n})."
        )
    lines += [
        "- A partial score is not a weaker yes. The gate treats a failed **linkage**",
        "  check as disproof, so cases below a full score are not 'probably fine' --",
        "  they are cases where something specific was shown to be wrong or absent.",
        "  That is why the score is displayed as a checklist and never as a percentage.",
        "",
        "**Caveat:** these rates come from one run on one synthetic dataset with a",
        "single investigator. They describe this fixture, not reconciliation in",
        "general, and the sample per bucket is small. No threshold anywhere in the",
        "system was tuned from this table, and none should be without far more data.",
    ]
    md = "\n".join(lines) + "\n"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "calibration.md").write_text(md)
    (OUTPUT_DIR / "calibration.json").write_text(
        json.dumps(
            {
                "provider": report.provider_name,
                "investigated": investigated,
                "by_request": [asdict(b) for b in request_buckets],
                "by_verification_score": [asdict(b) for b in score_buckets],
                "by_verdict": [asdict(b) for b in verdict_buckets],
            },
            indent=2,
        ) + "\n"
    )
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
