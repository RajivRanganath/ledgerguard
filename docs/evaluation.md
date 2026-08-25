# Evaluation

## How to reproduce

```bash
.venv/bin/python -m ledgerguard.evaluation.benchmark
```

Options: `--seed`, `--count`, `--split {holdout,dev,all}`,
`--provider {anthropic,stub,none}`.

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
earlier result had been scored against. That was a real hole and is recorded in
`BUILD_STATUS.md`.

No system was tuned using individual holdout answers.

## Reproducibility

Verified, not assumed:

- Two `build_dataset()` calls produce a batch with identical SHA-256
  (`c320c559e8ff564a` for the full set, `3e28d4572d4cfc95` for the holdout).
- `unresolved_exceptions.csv` is byte-identical across two benchmark runs.
- `benchmark.json` is identical across runs excluding the four wall-clock timing
  fields (`wall_seconds`, `throughput_per_second`, `latency_p50_ms`,
  `latency_p95_ms`), which vary by machine and by run and are not decisions.

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

The headline is not "100% vs 89.4%". The headline is the per-fault-class table:

| Fault class | n | Rules only correct | LedgerGuard correct | LG auto resolved | LG false auto |
|---|---|---|---|---|---|
| Clean | 54 | 54 | 54 | 0 | 0 |
| F1 missing settlement | 4 | 4 | 4 | 0 | 0 |
| F2 duplicate record | 4 | 4 | 4 | 4 | 0 |
| F3 unlinked partial refund | 9 | 0 | 9 | 9 | 0 |
| F4 fee or tax mismatch | 4 | 4 | 4 | 4 | 0 |
| F5 delayed settlement | 4 | 4 | 4 | 4 | 0 |
| F6 incorrect linkage | 6 | 6 | 6 | 0 | 0 |

Every point of difference between the two systems is in the F3 row. That is the
entire contribution of the AI layer, and it is exactly what it should be: F1,
F2, F4 and F5 are provable without a model, and F6 is not provable at all.

The F6 row is where the architecture is actually tested. The stub investigator
proposed `unlinked_partial_refund` with `recommended_action: resolve` for all six
of those cases. All six were rejected by the gate on linkage. Without the gate,
that column would read six false auto resolutions.

## Artifacts

| File | Contents |
|---|---|
| `outputs/benchmark.md` | the comparison table |
| `outputs/benchmark.json` | every metric for both systems, plus dataset provenance |
| `outputs/cases.json` | full per-case detail: invariants, investigation, verification, decision |
| `outputs/unresolved_exceptions.csv` | every case not closed, with exposure and blocking reason |
| `frozen/holdout_20260905_320.json` | the frozen holdout manifest |
