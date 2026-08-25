# Limitations

Stated plainly, because a controller that cannot describe its own boundaries is
not a controller.

## The AI investigator was never run against a real model

**This is the most important limitation on this page.** No `ANTHROPIC_API_KEY`
and no `ant` credential profile existed in the environment this was built in, so
every measured number in the README, `BUILD_STATUS.md` and
`evaluation/outputs/` was produced by `HeuristicProvider` — an offline
stand-in, not Claude.

What that does and does not invalidate:

- **Not affected:** the deterministic engine, the Shadow Ledger, the invariants,
  the Evidence Gate, the safety gate, the fault taxonomy, the holdout freeze, and
  the rules-only baseline. None of these involve a model.
- **Affected:** the hybrid column's claim about what *an LLM* contributes. What
  was measured is what a naive amount-matching investigator contributes when
  every proposal is passed through the Evidence Gate.
- **Direction of the bias:** the stub is deliberately worse than a competent
  model on selection — it proposes `unlinked_partial_refund` with
  `recommended_action: resolve` for every amount match, including all twelve
  wrong-linkage cases. So it stresses the gate harder than a careful model
  would. It is not a flattering substitute, but it is also not a model, and the
  latency and cost figures from a stub run are meaningless.

`AnthropicProvider` implements the same interface against `claude-opus-5` with
structured outputs. Set `ANTHROPIC_API_KEY` and rerun
`python -m ledgerguard.evaluation.benchmark`; every artifact and the dashboard
both name the provider that produced them, so a stub run can never be mistaken
for a model run.

## Data

- **Synthetic settlement data.** No real Razorpay data of any kind. Amounts,
  timing, references and fault rates are invented and internally consistent, not
  drawn from production distributions.
- **One settlement per payment.** Real settlements are batched per merchant per
  cycle. Modelling them per lifecycle makes the invariants exact and the
  exceptions attributable, at the cost of realism. A batched model would make
  attribution genuinely harder and is the first thing I would change.
- **Simplified fee model.** Flat 2% platform fee, 18% GST on the fee. Real
  pricing varies by method, MDR tier, international status and negotiated rates.
- **Limited fault taxonomy.** Six classes. F7 (bank amount mismatch) and F8
  (dirty or missing reference) are implemented as exception *types* but are not
  injected. Real reconciliation failure modes are far more varied.
- **320 lifecycles, 85 in the frozen holdout.** Enough for the fault classes to
  be meaningfully represented; small enough that a single case moves a
  percentage point. The per-fault-class table is the honest way to read the
  results, not the headline accuracy.
- **The fault rate is ~32%.** Real batches are far cleaner. A cleaner batch
  would raise the match rate and make the headline accuracy look better without
  the system being any better, which is exactly why false auto resolutions and
  rupee exposure are reported first.

## Method

- **`accuracy` is disposition accuracy** — did the case end in the state ground
  truth says it should. It is not a claim about a resolution being *correct in
  detail*.
- **Ground truth defines the safe outcome, not the maximal one.** F1 and F6 are
  labelled "should escalate". A system that resolved them would score worse here
  by design.
- **No calibration.** The Verification Score is a count of named checks that
  passed, not a probability. It is not calibrated and is never displayed as one.
- **No LLM-only baseline.** Only rules-only vs hybrid. An LLM-only arm would be
  informative but was P2 and was not built, so no claim is made about it.
- **Throughput figures come from a run with no network call.** They measure the
  deterministic engine, and will drop by orders of magnitude with a real model in
  the loop. Latency p50/p95 from a stub run are not meaningful.

## Integration

- **Razorpay Test Mode integration was not attempted.** Track 04 asks only for a
  synthetic batch of 50+ records, which is satisfied. It was a P1 item gated on
  P0 being stable, and it stayed behind the more valuable work. No Razorpay API
  behaviour, endpoint or response shape is claimed or simulated anywhere in this
  repository.
- **No production data, no live API calls, no real financial transfers of any
  kind are performed.** "Auto resolved" means a reconciliation exception was
  classified and closed inside LedgerGuard. It never means money moved.

## Frontend

The dashboard is a single dependency-free HTML file rather than React or
Next.js. One read-only page, no build step, no CDN fetch, no node toolchain —
chosen for demo reliability over stack conformance.

## Secrets

One secret: `ANTHROPIC_API_KEY`, read from the environment. `.env` is gitignored,
`.env.example` carries placeholders only. No key, token or credential is
committed anywhere in this repository or its history.
