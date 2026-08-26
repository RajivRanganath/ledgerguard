# Analyses

Every analysis writes its own artifact under `ledgerguard/evaluation/outputs/`,
and none of them tunes anything. Where a result is unflattering it is left in.

| Analysis | Command | Artifact | Question it answers |
|---|---|---|---|
| Benchmark | `evaluation.benchmark` | `benchmark.{md,json}` | Does the AI layer earn its place on top of the rules? |
| Ablation | `evaluation.ablation` | `ablation.{md,json}` | What is each component actually worth? |
| Investigator comparison | `evaluation.model_comparison` | `model_comparison.{md,json}` | Does the result depend on which model is used? |
| Calibration | `evaluation.calibration` | `calibration.{md,json}` | Is the Verification Score meaningful? |
| Drift | `evaluation.drift` | `drift.{md,json}` | Which assumptions are load-bearing? |
| Replay | `evaluation.replay` | `decision_log.json` | Can every decision be re-derived from the record? |
| Scale | `evaluation.scale` | `scale.{md,json}` | Does the deterministic path hold at 12,000 lifecycles? |
| Report export | `evaluation.report` | `reconciliation_report.{csv,md}`, `evidence_ledger.csv` | Can a human audit a closure from a file? |

---

## Ablation — what each component is worth

Five arms over the same frozen holdout, each removing one thing. The arm that
matters is `hybrid_no_gate`: identical to the full system except that the
investigator is taken at its word.

Removing the Evidence Gate turns a system with **zero** false closures into one
that closes six cases it should not have. That is the safety claim, in rupees,
rather than as an argument.

Removing the Shadow Ledger is a different failure entirely. Without an
independent expectation, the only checks left are "does the settlement's own
arithmetic add up" and "does the bank match what the settlement claimed". Both
hold for a settlement that is internally tidy and simply wrong, so the arm goes
*blind* rather than *wrong*: it misses most faults and closes nothing falsely.
Those are not equally bad, and the table shows both.

The `llm_only` arm also carries the cost argument: it needs one model call per
record, where the hybrid needs one per ambiguous exception.

## Calibration — is the Verification Score meaningful?

Run on the development split; the holdout is untouched, because a calibration
curve fitted on the holdout would contaminate every other number here.

Two things are calibrated against the same ground truth: the investigator's own
request to resolve, and the Verification Score. The gap between them is the
argument for showing a checklist instead of a confidence number.

A partial score is deliberately **not** a weaker yes. The gate treats a failed
linkage check as disproof, so cases below a full score are not "probably fine" —
they are cases where something specific was shown to be wrong or absent. That is
why the score is rendered as named checks and never as a percentage.

The sample per bucket is small and comes from one synthetic fixture. No
threshold anywhere in the system was tuned from it, and none should be.

## Drift — which assumptions are load-bearing?

Clean lifecycles are rewritten under a shifted world and re-run against the
*unchanged* controller, so every exception raised is a false positive caused by
the controller's assumptions no longer matching reality.

The results are blunt: **any** change to the fee rate — 2.00% to 2.25%, to 2.50%,
even downward to 1.75% — makes every single clean lifecycle raise a fee
mismatch. The fee schedule is the most load-bearing assumption in the system and
it has no tolerance at all.

The settlement window has exactly one day of tolerance, and the evaluation finds
its edge precisely: T+3 is absorbed, T+4 is not. Ticket sizes 10x larger are
fully absorbed.

Worth stating plainly: a 100% false-positive rate here means the controller
fails **loudly**, not silently. Flagging everything is recoverable; quietly
accepting wrong fees is not. But it does mean a fee change would bury a finance
team in exceptions until the schedule was updated, and that is a real
operational cost, not a rounding error.

## Replay — can a decision be re-derived?

"Why did you close that case in March" has to be answerable in August, from the
record, without the answer depending on which model happened to be reachable
that day.

The decision log captures the batch, the investigator's output, and every
decision. Replay reloads it **from disk** and re-derives every decision through
the matcher, Shadow Ledger, Evidence Gate and safety gate. The investigator is
not deterministic, so its output is replayed *as data* rather than regenerated —
the deterministic surface is what is being verified.

A replay that cannot fail proves nothing, so there is a test that tampers with a
stored decision and asserts the replay catches it.

## Scale — was 320 lifecycles a choice or a limit?

12,000 lifecycles (50,799 records) reconcile in well under a second with zero
false exceptions and zero missed faults, and per-case cost grows roughly
linearly across a 37x increase in size.

The published dataset stays at 320. Volume is not what makes the evaluation
meaningful — known ground truth against controlled faults is — and this page
exists so that "we kept it small on purpose" is a demonstrated claim rather than
an excuse.

## Report export — can a human audit a closure?

`reconciliation_report.csv` is one row per case. `evidence_ledger.csv` is one
row per evidence check the gate ran, with the check name, its kind, whether it
passed, the detail, and the records it was run against.

That second file is the point. Every automatic closure in this system is backed
by named checks against named records, and this writes them out so the question
"why was this closed" is answerable from a file rather than from a running
process.

## Finance Q&A — deliberately not the centerpiece

Not a chatbot. The architecture's rule applies unchanged: the model may choose
the question, but it never produces the number. Every answer is computed by
ordinary code over the reconciled records and carries the record ids it came
from.

A question outside the supported set is refused rather than improvised. An
invented total is worse than no answer, and a finance tool that guesses once is
a finance tool nobody can trust again.
