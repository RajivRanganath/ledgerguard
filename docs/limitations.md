# Limitations

Stated plainly, because a controller that cannot describe its own boundaries is
not a controller.

## What the model results do and do not establish

The hybrid column is now a real measurement — `groq:openai/gpt-oss-120b`, 15
live investigations, 15 of 15 completed. Four separate limitations apply to it.

**Model output is not reproducible.** Five runs on the identical frozen holdout
gave 95.3%–97.7% disposition accuracy at `temperature: 0`. Any single
accuracy figure from a model run is a sample, not a measurement, and I report the
range rather than the best one. The deterministic layer *is* reproducible, and
the safety figures (0 false auto resolutions, INR 0.00 falsely closed, 6/6 F6
escalated) held across all five runs.

**Four vendors measured, not one.** `docs/model_comparison.md` runs
`gemini-2.5-flash`, `gpt-oss-20b` via Groq, `mistral-medium-3-5` via OmniRoute
and `gpt-oss-120b` via NVIDIA over the same frozen holdout, uncached, plus the
offline stub and the rules-only baseline — every provider in the automatic
chain, each on its own free tier. Not one of them falsely auto-resolved
anything: INR 0.00 in every row. That is a far better-supported claim than any
single accuracy figure. Deliberately out of scope: any paid API. Cerebras was in the
chain early on and was removed when its key began returning HTTP 402
`payment required`; it has not been in the automatic order since, and none
of the numbers published here were ever produced by it. NVIDIA was unmeasurable for most of this
build and is not any more -- see below.

**What the safety result does and does not prove.** Two separate claims live
here and I do not merge them. That the investigator *cannot* close a case is
architectural: `decide()` reaches `AUTO_RESOLVED` only from a deterministic
proven cause or a `VERIFIED` gate verdict, and the model's recommended action
can only downgrade a close, never create one. That holds for any model, and I
stress-tested it rather than assuming it — an investigator fabricating a record
id on every case closed nothing on the invented evidence, and an invalid key
degraded its cases to human review without stopping the batch. That *no
investigator falsely closed anything* is empirical, and its denominator is
small: 6 wrong-linkage cases on one 85-lifecycle holdout, 5 investigators, 30
attempts. Six adversarial cases in one fault taxonomy is evidence, not a
guarantee, and the limiting factor is the taxonomy rather than the gate.

**The offline stub ties for highest, and that is a caution about the dataset.**
It reaches 100% -- level with Gemini -- because this fixture's ambiguity is
exactly the shape its single heuristic assumes. It is well-matched, not competent. If a naive amount-matcher
tops the table, headline accuracy on this data is close to meaningless — which is
why false auto resolutions and rupee exposure are the reported headline instead.

**Cost was not measured.** All providers used here were free tiers, which is
also why the latency figures (p50 10.8s, p95 12.8s in the committed benchmark,
with rate-limit retries included) are not representative of a paid tier. No cost-per-100-records figure
is claimed.

**Free tiers only, by design — so no paid frontier model is measured here.**
Every investigator in this repository runs on a free tier: Groq, Gemini, NVIDIA
NIM, and the local OmniRoute router. No paid API key is used, and none is
required to reproduce any number in this repository. That is a deliberate
constraint, not a missing piece: the architectural claim is that the Evidence
Gate holds *regardless* of how good the investigator is, and a claim of that
shape is better supported by four ordinary free-tier models than by one
expensive one.

`AnthropicProvider` remains implemented because the provider interface is meant
to be model-independent and it is the reference implementation of that
interface. It is not in the chain and no Claude column is published. The one
data point that exists is an anecdote, and is recorded as one: on the hand-built
adversarial pair reached through OmniRoute before that upstream credential
lapsed, Claude Opus 5 declined **both** halves with `insufficient_evidence`,
citing the CUST-0077 / CUST-0001 mismatch unprompted. One pair is not a
measurement.

**OmniRoute caches, so runs through it are replays, not repeat measurements.**
It returns `x-omniroute-cache-hit: true` for repeated `temperature: 0` requests,
serves the identical response id, and does **not** honour `Cache-Control:
no-cache`. A full benchmark through the chain came back with 15 of 15
investigations served from cache in 6ms each. The provider now counts cache hits
and the benchmark prints them, because a latency number or a "second run"
claim would otherwise be fiction. `LEDGERGUARD_NO_CACHE=1` omits `temperature`
to force real calls, at the cost of deterministic sampling. **The canonical
benchmark therefore runs against Groq directly, which does not cache** — the
fallback chain is the runtime resilience mechanism, not the measurement path.

**Alias routes can be served by a different vendor.** Requesting
`auto/claude-opus` on OmniRoute returned a response whose own `model` field read
`gemini-3.1-flash-lite`. The provider now records the served model from the
response rather than the requested route. Any provider-attributed number in this
repository is the model that *answered*, not the one that was asked for.

**A provider's model list is not a list of what you may call.** NVIDIA NIM was
recorded here for most of this build as unusable: the two routes configured for
it returned `Function ... Not found for account`, and I attributed that to a key
that had never been provisioned. When a working key replaced it, `GET /v1/models`
returned 200 and **83 models** — and calling them one by one showed that **36 of
the 57 chat models it advertises still return the same 404 for this account**.
Both previously configured routes were in that unentitled set. The catalogue is
global; entitlement is per account, and the API exposes no field that
distinguishes them.

So the diagnosis in the earlier version of this file was half wrong. The key was
one problem; the other was configuring routes from a catalogue instead of from a
call. The NVIDIA routes now listed are the ones I invoked against the
adversarial pair and kept only where they returned parseable structured output
and the correct verdict on both halves. `nvidia/nemotron-3-super-120b-a12b` is
entitled, answered, and was still rejected — it replied in prose and one
response was not JSON at all. It degraded safely to human review, which is the
fallback working, but a route that reliably fails to emit JSON only buys
latency.

This is the same failure the Evidence Gate exists to catch, one layer down: a
provider *claiming* a capability is not the same as the capability being there,
and the only way to tell is to check it against the thing itself.

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

## What the extra analyses do and do not settle

**The ablation is the strongest evidence here, and it is still one dataset.**
Removing the Evidence Gate produces six false closures worth INR 25,754.40 on
this holdout. That demonstrates the gate does something real on data built to
contain exactly the trap it is designed for. It does not establish a rate for
production traffic.

**The LLM-only arm was measured on 40 of 85 cases, not all of them.** It needs
one model call per record, and four free tiers were exhausted getting that far.
The finding — that a model with no independent expectation escalates 36 of 40
cases — is stable enough to report, but the sample is stated everywhere it
appears and the arm is not directly comparable to the 85-case arms.

**Calibration comes from one run on one fixture** with small per-bucket samples.
No threshold in the system was tuned from it.

**The drift evaluation is deliberately harsh.** It shifts one parameter at a
time against a controller that is not allowed to adapt. A real deployment would
update its fee schedule. What it establishes is which assumptions are
load-bearing, not how often they break.

**Replay covers the deterministic surface only.** The investigator's output is
replayed as data, not regenerated, because it is not deterministic. So replay
proves "the same records and the same investigation produce the same decision",
not "the same records produce the same investigation".

**The scale check exercises the deterministic path only.** The AI layer is
bounded by rate limits rather than data size, which today is a much harder
ceiling.

## Method

- **`accuracy` is disposition accuracy** — did the case end in the state ground
  truth says it should. It is not a claim about a resolution being *correct in
  detail*.
- **Ground truth defines the safe outcome, not the maximal one.** F1 and F6 are
  labelled "should escalate". A system that resolved them would score worse here
  by design.
- **The Verification Score is not a probability.** It is a count of named checks
  that passed, and it is never displayed as one. Calibration *was* later measured
  (`evaluation/calibration.py`) but nothing in the system is tuned from it, and
  the score shown to a user is still the checklist, not a number.
- **The LLM-only baseline is a later addition and a partial one.** The headline
  benchmark remains rules-only vs hybrid. An LLM-only arm was added in the
  ablation and measured on 40 of 85 cases; see the P2 section above for what it
  does and does not settle.
- **The two throughput figures measure different things.** The rules-only column
  (22,395 cases/s in the committed run) is the deterministic engine with no
  network call. The hybrid column (0.7 cases/s) is dominated entirely by 15
  sequential model calls on a rate-limited free tier. Neither is a production throughput number, and the
  investigations are not batched or parallelised.

## Integration

- **Razorpay Test Mode integration was declined, not attempted and abandoned.**
  Two reasons, both true: no Razorpay Test Mode credentials were available, and
  it was a P1 item gated on P0 being stable that stayed behind more valuable
  work. Track 04 asks only for a synthetic batch of 50+ records, which is
  satisfied, and section 2 of the build brief permits skipping it. No Razorpay
  API behaviour, endpoint or response shape is claimed or simulated anywhere in
  this repository.
- **No production data, no live API calls, no real financial transfers of any
  kind are performed.** "Auto resolved" means a reconciliation exception was
  classified and closed inside LedgerGuard. It never means money moved.

## Frontend

The dashboard is a single dependency-free HTML file rather than React or
Next.js. One read-only page, no build step, no CDN fetch, no node toolchain —
chosen for demo reliability over stack conformance.

## Secrets

One class of secret: free-tier provider API keys (`GROQ_API_KEY`,
`GEMINI_API_KEY`, `NVIDIA_API_KEY`, `OMNIROUTE_API_KEY`), each read from the
environment and used for nothing but an investigator call. No paid API key is
used anywhere. There are no Razorpay
credentials, no database password and no payment-rail access, because no real
money is ever moved. `.env` is gitignored, `.env.example` carries placeholders
only. No key, token or credential is committed anywhere in this repository or
its history.
