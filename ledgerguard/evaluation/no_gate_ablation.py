"""What the Evidence Gate actually stopped, recomputed from recorded runs.

    python -m ledgerguard.evaluation.no_gate_ablation

This makes no model calls and runs no benchmark. It reads two artifacts that
already exist and asks one counterfactual question of them: for every
wrong-linkage (F6) case that an investigator asked to close, what would have
happened if the raw hypothesis had been trusted instead of re-derived?

`investigator_wanted_to_close_f6` in the comparison artifact is exactly that
number -- the investigator's own recommendation, recorded before the gate ruled
on it. Summing it across investigators gives the no-gate count directly, with no
new sampling and no re-run.

The gate's rupee cost is not derivable from the comparison artifact, because
with the gate in place every provider falsely closed INR 0.00 and the value of
what it *blocked* is never realised. That figure comes from the ablation study,
which actually ran a `hybrid_no_gate` arm end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..ledger.money import to_rupees_str

OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
COMPARISON = OUTPUT_DIR / "model_comparison.json"
ABLATION = OUTPUT_DIR / "ablation.json"


def _load(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"{path.name} is missing. This script recomputes over recorded runs "
            f"and does not generate them. Run the benchmark that produces it first."
        )
    return json.loads(path.read_text())


def compute() -> dict:
    comparison = _load(COMPARISON)

    # The rules-only baseline has no investigator, so it cannot have proposed a
    # close. Including it would dilute the rate with a row that was never at risk.
    arms = [
        a for a in comparison["providers"]
        if a.get("investigator_wanted_to_close_f6") is not None
    ]
    wanted = sum(a["investigator_wanted_to_close_f6"] for a in arms)
    total = sum(a["f6_total"] for a in arms)
    with_gate = sum(a["false_auto_resolutions"] for a in arms)

    result = {
        "source_artifacts": [COMPARISON.name],
        "note": "recomputed from recorded runs; no model calls were made",
        "investigators": len(arms),
        "f6_cases_per_investigator": arms[0]["f6_total"] if arms else 0,
        "attempts": total,
        "would_auto_close_without_gate": wanted,
        "did_auto_close_with_gate": with_gate,
        "per_investigator": [
            {
                "investigator": a["provider"],
                "wanted_to_close_f6": a["investigator_wanted_to_close_f6"],
                "f6_total": a["f6_total"],
                "false_auto_with_gate": a["false_auto_resolutions"],
            }
            for a in arms
        ],
    }

    # Corroboration, and the only place the money figure exists.
    if ABLATION.exists():
        ablation = _load(ABLATION)
        for row in ablation.get("arms", []):
            if row.get("name") == "hybrid_no_gate":
                result["ablation_arm"] = {
                    "investigator": ablation.get("provider"),
                    "false_auto_resolutions": row.get("false_auto"),
                    # Money stays integer paise until the moment it is rendered.
                    "value_falsely_closed_inr": to_rupees_str(
                        row.get("false_auto_value_paise") or 0
                    ),
                }
    return result


def render_markdown(r: dict) -> str:
    lines = [
        "# With the gate versus without it",
        "",
        "Recomputed from `model_comparison.json`, an already-recorded run. No model",
        "calls were made and no benchmark was re-run to produce this file.",
        "",
        "Every investigator's raw hypothesis for every wrong-linkage (F6) case was",
        "recorded before the Evidence Gate ruled on it. Trusting those hypotheses",
        "directly is what a system without a gate would do.",
        "",
        f"| | Count |",
        f"|---|---|",
        f"| Investigators compared | {r['investigators']} |",
        f"| F6 cases each | {r['f6_cases_per_investigator']} |",
        f"| **Attempts** | **{r['attempts']}** |",
        f"| Would have auto closed with **no gate** | "
        f"**{r['would_auto_close_without_gate']} of {r['attempts']}** |",
        f"| Actually auto closed **with the gate** | "
        f"**{r['did_auto_close_with_gate']} of {r['attempts']}** |",
        "",
        "## Per investigator",
        "",
        "| Investigator | Asked to close an F6 | Closed with the gate |",
        "|---|---|---|",
    ]
    for row in r["per_investigator"]:
        lines.append(
            f"| `{row['investigator']}` | {row['wanted_to_close_f6']}/{row['f6_total']} "
            f"| {row['false_auto_with_gate']} |"
        )
    arm = r.get("ablation_arm")
    if arm:
        lines += [
            "",
            "## What it would have cost",
            "",
            "The count above is not the money. With the gate in place nothing was",
            "falsely closed, so the value of what it blocked is never realised in",
            "that run. `evaluation/ablation.py` measures it directly by running a",
            f"`hybrid_no_gate` arm end to end with `{arm['investigator']}`: "
            f"**{arm['false_auto_resolutions']} false auto resolutions, "
            f"INR {arm['value_falsely_closed_inr']} falsely closed** on the same holdout.",
        ]
    lines += [
        "",
        "## What this does and does not show",
        "",
        "It shows the gate is load-bearing rather than decorative: the investigators",
        "did propose closing the adversarial cases, repeatedly, and the gate is the",
        "only reason none of them closed. It does not show the gate is sufficient.",
        "The denominator is small and one-sided -- 6 hand-built wrong-linkage cases",
        "in a single fault taxonomy -- so this is evidence that the gate catches the",
        "failure it was designed for, not that it catches every failure.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    r = compute()
    (OUTPUT_DIR / "no_gate_ablation.json").write_text(json.dumps(r, indent=2) + "\n")
    md = render_markdown(r)
    (OUTPUT_DIR / "no_gate_ablation.md").write_text(md)
    print(md)
    print(f"Artifacts written to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
