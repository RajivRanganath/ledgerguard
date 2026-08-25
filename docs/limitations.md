# Limitations

Stated plainly, because a controller that cannot describe its own boundaries is
not a controller.

## What the model results do and do not establish

The hybrid column is now a real measurement — `groq:openai/gpt-oss-120b`, 15
live investigations, 15 of 15 completed. Four separate limitations apply to it.

**Model output is not reproducible.** Three runs on the identical frozen holdout
gave 95.3%, 96.5% and 97.7% disposition accuracy at `temperature: 0`. Any single
accuracy figure from a model run is a sample, not a measurement, and I report the
range rather than the best one. The deterministic layer *is* reproducible, and
the safety figures (0 false auto resolutions, INR 0.00 falsely closed, 6/6 F6
escalated) held across all three runs.

**One model on one dataset is thin evidence.** `docs/model_comparison.md` widens
it to four investigators, but Gemini's row is a rate limit rather than a
capability measurement (13 of 15 calls returned 429 on an exhausted free-tier
quota), NVIDIA was excluded for wall-clock reasons, and Cerebras returned HTTP
402 on the available key. So the honest statement is: one model measured
properly, one measured badly, one heuristic, and a baseline.

**The offline stub scores highest, and that is a caution about the dataset.**
It reaches 100% because this fixture's ambiguity is exactly the shape its single
heuristic assumes. It is well-matched, not competent. If a naive amount-matcher
tops the table, headline accuracy on this data is close to meaningless — which is
why false auto resolutions and rupee exposure are the reported headline instead.

**Cost was not measured.** All providers used here were free tiers, which is
also why the latency figures (p50 10.8s, p95 19.7s, with rate-limit retries
included) are not representative of a paid tier. No cost-per-100-records figure
is claimed.

`AnthropicProvider` (`claude-opus-5`, `messages.parse`) is implemented and is
first in the auto-detection order, but no Anthropic key was available, so Claude
was never measured here.

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
- **The two throughput figures measure different things.** The rules-only column
  (~25,000 cases/s) is the deterministic engine with no network call. The hybrid
  column (0.6 cases/s) is dominated entirely by 15 sequential model calls on a
  rate-limited free tier. Neither is a production throughput number, and the
  investigations are not batched or parallelised.

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
