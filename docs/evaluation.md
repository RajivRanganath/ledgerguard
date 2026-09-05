# Evaluation

## How to reproduce

```bash
.venv/bin/python -m ledgerguard.evaluation.benchmark
```

The canonical benchmark runs through the default chain with
`LEDGERGUARD_NO_CACHE=1`. Groq is first in the chain and does not cache, so it
serves the whole run in practice; the flag guards against OmniRoute's
`temperature: 0` response cache in the event the run falls through to it. The
benchmark prints which provider served how many investigations, and how many
came from a cache, so the run is self-describing either way.

Options: `--seed`, `--count`, `--split {holdout,dev,all}`, and
`--provider {auto,fallback,groq,gemini,nvidia,omniroute,openai_compatible,stub,none}`.

To compare investigators on the same holdout:

```bash
.venv/bin/python -m ledgerguard.evaluation.model_comparison --providers groq,gemini,stub
```

## Dataset and split

320 lifecycles from seed `20260905`, generator version `1.0.0`. Ground truth is
computed before any fault is injected and stored in a structure the matcher, the
investigator and the gate never receive.

The split is 75/25, **stratified by fault class**. Stratifying matters: an
unstratified 25% slice of a 320-case dataset can easily contain zero F6 cases,
which would make the headline safety metric meaningless. Paired F6 lifecycles
(the victim and the donor whose refund it is) are always kept on the same side.

| Fault class | Total | Holdout |
|---|---|---|
| Clean | 218 | 54 |
| F1 missing settlement | 15 | 4 |
| F2 duplicate record | 15 | 4 |
| F3 unlinked partial refund | 29 | 9 |
| F4 fee or tax mismatch | 15 | 4 |
| F5 delayed settlement | 16 | 4 |
| F6 incorrect linkage | 12 | 6 |

## Freezing

The holdout is frozen to `evaluation/frozen/holdout_{seed}_{count}.json`,
recording the lifecycle ids and the batch SHA-256. Every later run regenerates
the data and compares. A mismatch aborts with `FROZEN HOLDOUT MISMATCH` rather
than silently scoring against changed data.

Manifests are keyed per `(seed, count)`. An earlier version used a single file,
which meant running with a different `--count` silently overwrote the manifest an
earlier result had been scored against — a result would then be reported against
a holdout that no longer existed. That was a real hole, and keying the manifest
by configuration is what closed it.

No system was tuned using individual holdout answers.

## Reproducibility

Verified, not assumed:

- Two `build_dataset()` calls produce a batch with identical SHA-256
  (`c320c559e8ff564a` for the full set, `3e28d4572d4cfc95` for the holdout).
- `unresolved_exceptions.csv` is byte-identical across two benchmark runs.
- `benchmark.json` is identical across runs excluding the four wall-clock timing
  fields (`wall_seconds`, `throughput_per_second`, `latency_p50_ms`,
  `latency_p95_ms`), which vary by machine and by run and are not decisions.

These guarantees cover the **dataset and the deterministic layer**, and they
hold with `--provider stub` or `--provider none`. They do **not** extend to a
model run: five `groq` runs on the identical holdout resolved between 5 and 7
of the 9 F3 cases at `temperature: 0`. The historical notes report stable safety across all five
(0 false auto resolutions, INR 0.00 falsely closed, all 6 F6 escalated); the
variance is confined to how much is safely closed. Report the range, not the
best run.

## Metrics

Defined in `evaluation/metrics.py`. Every figure in every artifact is rendered
from the `Metrics` dataclass; no literal number appears in any template.

The two reported first, deliberately:

- **`false_auto_resolutions`** — cases closed automatically that ground truth
  says required a human. This is the failure mode the whole architecture exists
  to prevent.
- **`false_auto_resolved_value_paise`** — the rupee value behind those closures.

Also reported: total cases, match rate, exceptions raised, false exceptions,
missed faults, exception-type accuracy, disposition accuracy, auto resolved,
exceptions correctly/incorrectly resolved, correct abstentions, unnecessary
abstentions, human review required, unresolved, value left unresolved,
throughput, investigations per 100 records, model calls per 100 records, and
latency p50/p95.

`correct_abstentions` vs `unnecessary_abstentions` is the distinction that makes
abstention scoreable: escalating a case ground truth says must be escalated is
correct behaviour; escalating one the evidence could have closed is a missed
opportunity. Neither is an error in the way a false auto resolution is.

## Reading the result

The headline is not "97.7% vs 89.4%". The headline is the per-fault-class table
(canonical run, `fallback(groq->gemini->nvidia->omniroute)`, served by
`openai/gpt-oss-120b`):

| Fault class | n | Rules only correct | LedgerGuard correct | LG auto resolved | LG false auto |
|---|---|---|---|---|---|
| Clean | 54 | 54 | 54 | 0 | 0 |
| F1 missing settlement | 4 | 4 | 4 | 0 | 0 |
| F2 duplicate record | 4 | 4 | 4 | 4 | 0 |
| F3 unlinked partial refund | 9 | 0 | 7 | 7 | 0 |
| F4 fee or tax mismatch | 4 | 4 | 4 | 4 | 0 |
| F5 delayed settlement | 4 | 4 | 4 | 4 | 0 |
| F6 incorrect linkage | 6 | 6 | 6 | 0 | 0 |

Every point of difference between the two systems is in the F3 row. That is the
entire contribution of the AI layer, and it is exactly what it should be: F1,
F2, F4 and F5 are provable without a model, and F6 is not provable at all. The
two F3 cases the model left open are *unnecessary abstentions* — safe, but a
missed opportunity, counted separately from correct abstentions so the cost of
caution stays visible.

The F6 row is where the architecture is actually tested. In this committed run,
`gpt-oss-120b` proposed `unlinked_partial_refund` on five of the six cases and
asked to resolve on **four** of them; the gate positively disproved **five** on
linkage. On the sixth it returned `insufficient_evidence` and declined the case
on its own — the right answer, arrived at without help. Per-case detail is in
`outputs/cases.json`; count it yourself rather than taking this paragraph's word
for it.

Two things follow, and they pull in opposite directions, which is why both are
here. The model is capable of getting this right. It is not *reliably* capable:
the same model on the hand-built adversarial fixture, and the same model in
other runs, asks to close cases it should not. The deliberately naive offline
stub asked to resolve 6 of 6 and was rejected 6 of 6. Nothing in the model's
output distinguishes the case it declined from the four it wanted closed — that
indistinguishability is the entire argument for the gate.

Without the gate, that column reads four false auto resolutions for this run.
Across all five investigators it reads 25 of 30; see
`outputs/no_gate_ablation.md`.

**These per-run counts move.** Model output is not reproducible, so the number
asked to close is a sample and will differ on a rerun. What has not moved in any
recorded run: false auto resolutions 0, value falsely closed INR 0.00, all six
F6 cases escalated.

See `docs/model_comparison.md` for the five-investigator table, where
`gpt-oss-20b` via Groq did ask to close 6 of 6.

## Compound failure cases

Five cases, hand designed, each layering two of the six fault classes on one
lifecycle. They live in `ledgerguard/synthetic/compound.py` as a **separate
batch**, deliberately not mixed into the frozen holdout, so none of the
published holdout numbers move when they change. Pinned by
`ledgerguard/tests/test_p1_compound.py`.

Each case declares its expected disposition and the reasoning for it *in the
source*, before the system is run — the same discipline used for the original
six single-fault classes.

| Case | Faults | Expected | Actual | |
|---|---|---|---|---|
| C1 | F5 + F3 — delayed settlement + unlinked partial refund | `AUTO_RESOLVED` | `AUTO_RESOLVED` | match |
| C2 | F4 + F6 — fee overcharge + unexplained deduction | `HUMAN_REVIEW_REQUIRED` | `HUMAN_REVIEW_REQUIRED` | match |
| C3 | F2 + F5 — duplicate bank credit + delayed settlement | `AUTO_RESOLVED` | `AUTO_RESOLVED` | match |
| C4 | F1 + F3 — missing settlement + orphan refund, same customer | `HUMAN_REVIEW_REQUIRED` | `HUMAN_REVIEW_REQUIRED` | match |
| C5 | F4 + F6 — fee overcharge + wrong-linkage deduction | `HUMAN_REVIEW_REQUIRED` | `HUMAN_REVIEW_REQUIRED` | match |

Recorded from a run with the offline heuristic stub, and re-confirmed against a
live `fallback(groq->gemini->nvidia->omniroute)` run: 5 of 5 on both.

What the interesting ones actually exercise:

- **C1** closes on the refund and still reports the delay, rather than letting
  the resolved shortfall silently absorb a second finding.
- **C2 and C5** are the ones that matter. Both have a *deterministically proven*
  fee variance sitting on top of an unexplained deduction. Correcting the proven
  part leaves INR 650.00 (C2) and INR 900.00 (C5) unattributed, and the case is
  escalated on the residual. A proven cause does not license closing the case.
- **C4** refuses to net a same-customer orphan refund against a settlement that
  never arrived.

### What this does and does not demonstrate

It demonstrates that one proven fault does not license closing an unproven one —
the residual check is doing real work, and C2 and C5 would close incorrectly
without it.

It does **not** demonstrate that the fault taxonomy is deep. Three honest caveats:

1. **Five hand-built cases, designed by the same person who wrote the resolution
   rules.** 5 of 5 measures the rules against their author's intent, not against
   reality. A case I did not think of is not represented here.
2. **Four of the five never reach the investigator at all.** Layering a second
   fault usually surfaces a deterministic exception type first (`FEE_MISMATCH`,
   `DUPLICATE_RECORD`, `MISSING_SETTLEMENT`), which the engine proves without a
   model call. Only C1 was AI-investigated. So this set says much more about the
   deterministic layer than about the AI path.
3. **Only pairs.** No case layers three or more faults, and no case combines two
   faults that both require investigation — the shape most likely to break the
   residual logic, and the obvious next thing to build.

## Artifacts

| File | Contents |
|---|---|
| `outputs/benchmark.md` | the comparison table |
| `outputs/benchmark.json` | every metric for both systems, plus dataset provenance |
| `outputs/cases.json` | full per-case detail: invariants, investigation, verification, decision |
| `outputs/unresolved_exceptions.csv` | every case not closed, with exposure and blocking reason |
| `outputs/model_comparison.md` | four investigators on the same holdout |
| `outputs/model_comparison.json` | the same, machine readable |
| `outputs/no_gate_ablation.md` | with-gate vs without-gate, recomputed from the above; no new run |
| `outputs/no_gate_ablation.json` | the same, machine readable |
| `outputs/reconciliation_report.md` | exportable summary — **a separate run from `benchmark.md`** |
| `outputs/reconciliation_report.csv` | one row per case, from that same report run |
| `outputs/evidence_ledger.csv` | one row per evidence check, from that same report run |
| `frozen/holdout_20260905_320.json` | the frozen holdout manifest |

One stale field, recorded rather than hand-corrected: `benchmark.json`'s
`investigator_chain.chain` names `nvidia/llama-3.1-nemotron-70b-instruct`, a
route the NVIDIA preset no longer carries (see the comment in
`ai/openai_compatible.py` — it was in the set this account is not entitled to
invoke, and was replaced). The chain list describes the chain as it stood when
that run was made. It did not affect the run: `served_by` shows Groq answered
all 15 investigations, and no NVIDIA route was ever reached. Artifacts are not
edited by hand here, so it stays as recorded until the benchmark is next
regenerated.

The benchmark artifacts and the report artifacts are produced by two different
commands and therefore two different live runs. Each names its investigator on
its first lines. Their accuracy figures differ by sampling (19 vs 20 exceptions
closed); their safety figures do not (0 false auto resolutions, INR 0.00 falsely
closed, 6/6 F6 escalated in both).
