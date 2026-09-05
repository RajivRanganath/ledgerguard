# Architecture

## The one-sentence version

Deterministic accounting decides what is true; the AI only proposes candidate
explanations for what the accounting could not decide; an independent gate
re-derives every proposal from records before it can change any state.

## Data flow

```
                         Batch (orders, payments, refunds, settlements, bank)
                                            |
                              +-------------+-------------+
                              |                           |
                      ShadowLedger                   deterministic
                  (expected movement)                  matcher
                              |                           |
                              +------------+--------------+
                                           |
                                  6 invariants I1..I6
                                           |
                             +-------------+--------------+
                             |                            |
                     provable outcome              genuinely ambiguous
                (MATCHED / typed exception)       (UNEXPLAINED_SHORTFALL,
                             |                     BANK_MISMATCH, ...)
                             |                            |
                             |                    AI investigator
                             |                  (structured JSON only)
                             |                            |
                             |                     Evidence Gate
                             |               (7 deterministic checks)
                             |                            |
                             |                  uniqueness pass
                             +-------------+--------------+
                                           |
                                  safety / action gate
                       AUTO_RESOLVED | RECOMMEND_REVIEW
                       HUMAN_REVIEW_REQUIRED | UNRESOLVED
```

## Components

### `ledger/money.py`
Every monetary value is an `int` count of paise. `Decimal` appears only inside
rate application, and the result is quantised back to an integer with
`ROUND_HALF_UP` at the paise boundary — the single place a derived money value
is allowed to round. `apply_rate` raises on a non-`int` input, so a float can
never enter the money path by accident.

Fee model: 2% platform fee on the captured amount, 18% GST on that fee.

### `ledger/shadow_ledger.py`
Indexes a batch and reconstructs, per captured payment:

```
expected_net = gross − fees − tax − valid_refunds + valid_adjustments
```

A refund counts as *valid* only if it is `PROCESSED` **and** carries an explicit
`payment_id` linking it to this payment. Refunds with no linkage are held
separately as `orphan_refunds` and are never silently absorbed.

`expected_for_payment(payment, extra_refunds=[...])` is the hypothetical form:
"if this refund really did belong to this payment, would the ledger balance?"
It mutates nothing. This is the mechanism the Evidence Gate uses for its
decisive check, and the reason the AI can influence *which* record is tested
without influencing *how* the test is computed.

### `ledger/invariants.py`
Six named predicates, each independently testable:

| | Invariant | Catches |
|---|---|---|
| I1 | settlement arithmetic is internally consistent | corrupted settlement rows |
| I2 | fees and tax match the independent fee schedule | fee/tax overcharge (F4) |
| I3 | observed net equals the Shadow Ledger's net | any unexplained shortfall (F3, F6) |
| I4 | settlement gross equals the captured amount | capture/settlement mismatch |
| I5 | settlement date falls inside T+2 ±1d | delayed settlement (F5) |
| I6 | one bank credit equals the settlement net | missing/duplicated bank credit (F2) |

I2 is what catches a settlement that is internally consistent but wrong — a fee
overcharge keeps I1 holding, so only an independently computed fee schedule
finds it.

### `reconciliation/matcher.py`
Runs before the model, always. Duplicate pre-pass over the whole batch, then one
case per captured payment: no settlement → `MISSING_SETTLEMENT`; more than one →
`DUPLICATE_RECORD`; otherwise run all six invariants and map the failures to a
specific exception type, most severe first.

The duplicate pre-pass compares **content, not just identifiers**. Two rows
sharing a `payment_id` are only a duplicate when every other field agrees; if
they disagree they are two contradictory versions of one payment, which the
batch cannot adjudicate, and the case is escalated rather than collapsed. The
same rule applies to settlements. Bank entries carry no identifier that survives
the trip between systems, so they are matched on content alone. The distinction
matters because `DUPLICATE_RECORD` is one of only three exception types the
safety gate closes without a human — it closes on the strength of the copies
being the same record, so "the same record" has to be something that was
actually checked.

`exceptions.py` splits the taxonomy into `DETERMINISTICALLY_PROVEN` (never
reaches the model) and `NEEDS_INVESTIGATION`. On the frozen holdout, 16 of the
31 exceptions are proven outright and never cost a model call.

### `ai/`
`InvestigatorProvider.investigate(context) -> InvestigationResult`
(`ai/provider.py`) is the entire surface the rest of the system depends on, so
the controller is model independent. `investigate_case(ledger, case, provider)`
in `ai/investigator.py` is the only caller: it builds the context, enforces the
timeout, type-checks whatever comes back, and strips any evidence id the model
did not receive as a candidate. A provider that raises, and a provider that
returns the wrong type, both become `unavailable` rather than an exception the
batch has to survive.

`ai/errors.py` carries one distinction the chain builder depends on:
`ProviderNotConfigured` means "no key, or the endpoint is not reachable", which
is a documented way to run on a subset and stays silent. Every other
construction failure is a defect, is recorded in `CHAIN_BUILD_ERRORS`, and is
announced on stderr — a run that silently used two providers when four were
configured is not the run it claims to be.

The wire schema (`InvestigatorOutput`) is separate from the runtime type
(`InvestigationResult`) so the runtime-only fields — `source`, `model_name`,
`error` — cannot be written by the model. `hypothesis` is a closed `Literal` set.
There is no confidence field anywhere; adding one would create something the rest
of the system could be tempted to trust.

Providers, all returning the same type:

| Provider | Transport | Default model | Structured output |
|---|---|---|---|
| `AnthropicProvider` | `anthropic` SDK, `messages.parse` | `claude-opus-5` | native schema — reference implementation only, never called (free tiers only) |
| `OpenAICompatibleProvider` | chat completions over httpx | Groq `openai/gpt-oss-120b`, NVIDIA `openai/gpt-oss-120b`, OmniRoute `mistral/mistral-large-latest` | strict `json_schema`, falling back to `json_object` (NVIDIA uses `json_object`) |
| `GeminiProvider` | `generateContent` over httpx | `gemini-2.5-flash` | `responseSchema` |
| `HeuristicProvider` | none | — | offline stand-in, deliberately naive |
| `UnavailableProvider` | none | — | models a dead provider |

Adding the second and third of these touched no other module, which is the
interface doing its job. The JSON schema is hand-written rather than derived
from Pydantic, because strict-mode support varies between hosts and several
reject `$ref`/`$defs`; the hypothesis enum is pulled from the taxonomy so the
two cannot drift.

Two operational details that came out of running against real hosts rather than
being anticipated:

- **Rate limits are a normal condition, not a failure.** Free tiers on these
  hosts are tight (Groq is 8000 tokens/minute), so 429 and transient 5xx are
  retried up to four times honouring the server's `Retry-After` before being
  allowed to degrade to abstention.
- **Reasoning tokens bill against the output ceiling.** Gemini truncated every
  response mid-string until thinking was disabled, and Groq's reasoning model
  exhausted the token-per-minute budget until `max_completion_tokens` was cut to
  1200 and reasoning effort lowered. Both failures surfaced as
  `invalid_response` / `unavailable` and abstained correctly — the system was
  never wrong, only unproductive, which is the intended failure direction.

After the call, `investigate_case` drops any evidence id that was not offered as
candidate evidence, and records the drop. That covers invented identifiers and
also real identifiers of the wrong kind — Gemini repeatedly included the
payment's own id alongside the refund id, and it was dropped before the gate saw
it.

### `evidence/verifier.py` — the Evidence Gate
For each permitted hypothesis there is a fixed battery of deterministic checks.
For `unlinked_partial_refund`:

| Check | Kind | Question |
|---|---|---|
| E1 | existence | do the proposed records actually exist? |
| E2 | existence | is the refund `PROCESSED`? |
| E3 | **linkage** | is it already owned by a different payment? |
| E4 | **linkage** | does its reference identify *this* order's customer? |
| E5 | amount | does it equal the shortfall? |
| E6 | timing | is it after capture and before settlement +1d? |
| E7 | invariant | re-running the Shadow Ledger with it applied, does I3 hold exactly? |

A failure of a **linkage** check produces `REJECTED` — positive disproof.
Anything else produces `UNVERIFIED` — absence of proof. That distinction is the
whole design: a refund whose amount matches but whose customer does not is not
weak evidence, it is counter-evidence.

E4 compares *normalised* strings (non-alphanumerics stripped, uppercased), which
is why the deterministic matcher cannot do this job itself: the matcher does
exact identifier matching, and the reference names a **customer**, not an order.
A customer has several orders in this dataset, so even a parsed reference does
not identify the payment. Selecting the payment is the investigator's job;
proving the selection is the gate's.

`resolve_evidence_conflicts` then runs across all cases: if two would-be
`VERIFIED` cases claim the same record, both are downgraded. It is a separate
pass precisely so the result cannot depend on the order cases were investigated
in.

### `evidence/safety.py`
Maps a verification outcome onto four states. `AUTO_RESOLVED` requires every
check to pass, the ledger to balance, and the investigator to have asked for
resolution. `REJECTED` and `UNVERIFIED` both escalate.

Three guards sit ahead of the deterministic close, each for the same reason — a
proven cause is not a fully explained case:

| Guard | Refuses to close when |
|---|---|
| `residual_paise` | correcting the proven fee variance still leaves money unattributed |
| `window_undecidable` | there is no capture timestamp, so the timing check never ran |
| `content_conflict` | the "duplicate" rows disagree, so no content match was proved |

Note that `MISSING_SETTLEMENT` escalates even though it is fully proven: the
cause is known, but closing it is not within the system's authority. Proof and
authority are separate questions.

### `pipeline.py`
`run(batch, use_ai=True|False)`. The baseline is the same call with the
investigation step switched off — not a different code path — which is what
makes the comparison fair.

## Deliberate omissions

- **No planner, manager, supervisor, critic or verification *agent*.** One
  investigator, one deterministic engine, one independent gate. A verification
  agent would be a second model asked to grade the first; the gate re-derives
  from records instead, which is strictly stronger.
- **No calibrated confidence.** A Verification Score is a count of named checks,
  and that is what the user is shown. Calibration was later *measured*
  (`evaluation/calibration.py`, on the dev split under the offline stub) to ask
  whether the score means anything, but nothing in the system is tuned from it
  and no threshold is derived from it. Measuring a score is not the same as
  turning it into a probability, and the score is never rendered as one.
- **No database.** SQLite would add a persistence layer nothing in the demo
  needs; the controller is a pure function of a batch.
