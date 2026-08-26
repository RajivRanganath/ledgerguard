"""Investigator providers for OpenAI-compatible APIs and for Gemini.

The controller depends on exactly one shape --
``investigate(context) -> InvestigationResult`` -- so adding a provider is
additive and touches nothing else. That was the point of the interface, and
these classes are the proof of it.

Every provider here is held to the same contract as the Anthropic one:

  * the model is asked for structured JSON against a strict schema
  * anything that does not validate becomes ``InvestigationResult.invalid``
  * any transport or provider failure becomes ``InvestigationResult.unavailable``
  * neither of those can ever become a resolution

so a badly behaved provider degrades to abstention rather than to a wrong close.
"""

from __future__ import annotations

import json
import os
import random
import time

import httpx

from ..reconciliation.exceptions import PERMITTED_HYPOTHESES
from .schemas import InvestigationResult, InvestigatorOutput

DEFAULT_TIMEOUT_SECONDS = 60.0

#: Enough for the JSON payload plus a short chain of reasoning. Reasoning
#: models bill their thinking against rate limits, so an oversized ceiling here
#: turns into 429s rather than into better answers.
DEFAULT_MAX_TOKENS = 1200

#: Free tiers on these hosts are tight (Groq is 8000 tokens/minute). A 429 is a
#: normal, expected condition, not a provider failure -- so it is retried with
#: the server's own Retry-After before being allowed to degrade to abstention.
MAX_RETRIES = 4
MAX_RETRY_WAIT_SECONDS = 45.0

#: Hand-written rather than derived from Pydantic: strict-mode JSON schema
#: support varies between providers, and several reject `$ref`/`$defs`. The
#: hypothesis enum is pulled from the taxonomy so the two cannot drift.
INVESTIGATION_SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string", "enum": list(PERMITTED_HYPOTHESES)},
        "reason": {"type": "string"},
        "required_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "expected_relationship": {"type": "string"},
                },
                "required": ["type", "expected_relationship"],
                "additionalProperties": False,
            },
        },
        "candidate_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "recommended_action": {"type": "string", "enum": ["resolve", "review"]},
    },
    "required": [
        "hypothesis",
        "reason",
        "required_evidence",
        "candidate_evidence_ids",
        "recommended_action",
    ],
    "additionalProperties": False,
}


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honour Retry-After when the server sends one; otherwise back off."""
    header = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if header:
        try:
            return min(float(header) + 0.5, MAX_RETRY_WAIT_SECONDS)
        except ValueError:
            pass
    return min(2.0 * (2 ** attempt) + random.uniform(0, 0.5), MAX_RETRY_WAIT_SECONDS)


def _strip_fences(text: str) -> str:
    """Some models wrap JSON in markdown fences even when told not to."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
        text = text.removeprefix("json").strip()
    return text.strip()


def _parse(raw: str, model: str, served_model: str | None = None) -> InvestigationResult:
    """Validate a raw model response into the structured result type.

    ``served_model`` is what the host says actually answered, which is not
    always what was asked for: routers resolve alias routes to whatever upstream
    is currently live, so requesting `auto/claude-opus` can be served by an
    entirely different model. The report records what answered, not what was
    requested -- attributing a result to the wrong model would be the most
    embarrassing possible failure in a project about verifying claims.
    """
    text = _strip_fences(raw)
    if not text:
        return InvestigationResult.invalid("model returned an empty response")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return InvestigationResult.invalid(f"response was not valid JSON: {exc}")
    try:
        out = InvestigatorOutput.model_validate(payload)
    except Exception as exc:
        # Includes a hypothesis outside the permitted taxonomy.
        return InvestigationResult.invalid(f"response failed schema validation: {exc}")
    try:
        return InvestigationResult(
            hypothesis=out.hypothesis,
            reason=out.reason,
            required_evidence=out.required_evidence,
            candidate_evidence_ids=out.candidate_evidence_ids,
            recommended_action=out.recommended_action,
            source="model",
            model_name=served_model or model,
        )
    except Exception as exc:
        # A schema problem is a bad response, not a dead provider. Reporting it
        # as "unavailable" would hide a real defect behind a transport-looking
        # error message.
        return InvestigationResult.invalid(f"result failed runtime validation: {exc}")


#: base_url, API key env var, default model, and whether the endpoint accepts a
#: strict `json_schema` response format (verified by probing each provider).
PRESETS: dict[str, dict] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env": "GROQ_API_KEY",
        "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b"],
        "json_schema": True,
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "env": "CEREBRAS_API_KEY",
        "models": ["gpt-oss-120b", "gemma-4-31b"],
        "json_schema": True,
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "env": "NVIDIA_API_KEY",
        # The meta/llama-* endpoints this originally used are end-of-life
        # (HTTP 410 Gone). The replacements below exist in the account's model
        # list but are not provisioned for the key in use ("Function ... Not
        # found for account"), so NVIDIA sits at the tail of the chain and is
        # retired after two failures. Left configured because the code path is
        # correct and only the entitlement is missing.
        "models": [
            "nvidia/llama-3.1-nemotron-70b-instruct",
            "mistralai/mistral-large-2-instruct",
        ],
        "json_schema": False,
    },
    "omniroute": {
        # OmniRoute is a *local* router (`omniroute` CLI), not a hosted service.
        # It fronts many upstreams behind one OpenAI-compatible endpoint, which
        # is how Claude gets measured here without a direct Anthropic key.
        "base_url": "http://localhost:20128/v1",
        "env": "OMNIROUTE_API_KEY",
        # `auto/*` routes resolve to whatever upstream is currently live, so the
        # served model is recorded from the response rather than assumed. Point
        # LEDGERGUARD_MODEL at a concrete route (e.g.
        # `openrouter/anthropic/claude-opus-5`) when the matching upstream
        # credential is active in OmniRoute.
        # OmniRoute fronts several upstreams under its own credentials, which
        # are separate from the direct keys above -- so its Groq and NVIDIA
        # routes are genuine fallbacks, not duplicates. The list interleaves
        # upstreams deliberately: one dead upstream then costs one hop, not the
        # whole link.
        #
        # Every route below was individually verified to answer with strict
        # json_schema. Excluded after testing: all Claude routes (oc/* 401,
        # openrouter/anthropic/* "No active credentials"), nvidia/deepseek-v4-pro
        # and nvidia/z-ai/glm-5.2 (410 Gone), groq/llama-3.3-70b-versatile and
        # gemini/gemini-2.5-flash (404, retired upstream), gemini/gemini-3.5-flash
        # (ignores the schema and answers in prose), and the auto/* aliases
        # (slow free models, rarely parseable).
        "models": [
            "mistral/mistral-large-latest",
            "nvidia/nvidia/nemotron-3-super-120b-a12b",
            "groq/openai/gpt-oss-120b",
            "mistral/mistral-medium-3-5",
            "nvidia/openai/gpt-oss-120b",
            "mistral/mistral-small-latest",
        ],
        "json_schema": True,
    },
    "openai_compatible": {
        "base_url": None,          # from LEDGERGUARD_BASE_URL
        "env": "LEDGERGUARD_API_KEY",
        "models": [],              # from LEDGERGUARD_MODEL
        "json_schema": True,
    },
}


class OpenAICompatibleProvider:
    """Chat-completions provider (Groq, Cerebras, NVIDIA, or any compatible host)."""

    def __init__(
        self,
        preset: str = "groq",
        model: str | None = None,
        models: list[str] | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        cfg = PRESETS.get(preset, PRESETS["openai_compatible"])
        self.base_url = (base_url or cfg["base_url"] or os.environ.get("LEDGERGUARD_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get(cfg["env"], "")

        # A route can die mid-run -- a rate limit resets, an upstream credential
        # is deactivated, a free tier runs out. Rather than degrade every
        # remaining case to abstention, the provider carries an ordered list of
        # routes and advances to the next one when the current is exhausted.
        chosen = model or os.environ.get("LEDGERGUARD_MODEL") or None
        if chosen:
            self.models = [chosen]
        else:
            self.models = list(models or cfg.get("models") or [])
            if not self.models and cfg.get("model"):
                self.models = [cfg["model"]]
        self._index = 0
        #: Routes retired this run, with the failure that retired them.
        self.exhausted: dict[str, str] = {}
        self.supports_json_schema = bool(cfg["json_schema"])
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.preset = preset
        self.reasoning_effort = os.environ.get("LEDGERGUARD_REASONING_EFFORT") or None
        self.retries = 0
        #: Retry budget for rate limits and transient 5xx. Lowered by
        #: FallbackProvider for non-terminal links: waiting out a rate limit is
        #: only worth it when there is nothing else to ask.
        self.max_retries = MAX_RETRIES
        #: Optional request parameters this host has rejected. Sending a tuning
        #: knob that a model does not support is our bug, not the route's, so
        #: the parameter is dropped and the route retried rather than retired.
        self._disabled_params: set[str] = set()
        #: Models that actually answered, when they differ from the one asked for.
        self.served_models: set[str] = set()
        #: Token usage as reported by the host, accumulated across the run.
        #: Providers that omit `usage` simply contribute nothing, which is why
        #: the benchmark reports "tokens reported by the provider" rather than
        #: implying a measurement we did not take.
        self.prompt_tokens = 0
        self.completion_tokens = 0
        #: Responses the host served from its own cache rather than the model.
        #: OmniRoute caches temperature-0 requests and does not honour
        #: Cache-Control: no-cache, so repeated runs over a deterministic
        #: dataset are replays, not independent samples. Counting them is the
        #: only way a latency figure or a "second run" claim stays honest.
        self.cache_hits = 0
        if not (self.base_url and self.api_key and self.models):
            raise ValueError(f"provider {preset!r} is not fully configured")
        self._client = httpx.Client(timeout=timeout)

    @property
    def name(self) -> str:
        """Preset plus the route currently in use.

        Deliberately not frozen at construction: after rotation the provider is
        no longer the model it started as, and a report that still named the
        first route would attribute results to a model that never answered.
        """
        return f"{self.preset}:{self.model}"

    #: Optional request parameters that may be dropped if a host rejects them.
    OPTIONAL_PARAMS = ("reasoning_effort", "temperature", "top_p")

    def _rejected_parameter(self, error: str) -> str | None:
        """Which optional parameter, if any, this 400 is complaining about."""
        if "api error 400" not in error:
            return None
        lowered = error.lower()
        for param in self.OPTIONAL_PARAMS:
            if param in lowered and param not in self._disabled_params:
                return param
        return None

    @property
    def model(self) -> str:
        """The route currently in use."""
        return self.models[min(self._index, len(self.models) - 1)]

    def _retire_current(self, reason: str) -> bool:
        """Retire the current route and move to the next. False if none left."""
        self.exhausted.setdefault(self.model, reason)
        self._index += 1
        return self._index < len(self.models)

    def _post_with_retries(self, url: str, headers: dict, body: dict):
        """POST, retrying rate limits and transient 5xx. Returns (response, failure)."""
        last = "unknown error"
        budget = getattr(self, "max_retries", MAX_RETRIES)
        for attempt in range(budget + 1):
            try:
                response = self._client.post(url, headers=headers, json=body)
            except httpx.TimeoutException as exc:
                last = f"timeout after {self.timeout}s: {exc}"
            except httpx.HTTPError as exc:
                last = f"transport error: {type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return response, None
                last = f"api error {response.status_code}: {response.text[:200]}"
                if response.status_code not in (408, 429, 500, 502, 503, 504):
                    break
                if attempt < budget:
                    self.retries += 1
                    time.sleep(_retry_delay(response, attempt))
                    continue
                break
            if attempt < budget:
                self.retries += 1
                time.sleep(min(2.0 * (2 ** attempt), MAX_RETRY_WAIT_SECONDS))
        return None, InvestigationResult.unavailable(last)

    def _response_format(self, schema: dict | None = None, name: str = "investigation") -> dict:
        if self.supports_json_schema:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "strict": True,
                    "schema": schema or INVESTIGATION_SCHEMA,
                },
            }
        return {"type": "json_object"}

    def complete_json(
        self, system: str, user: str, schema: dict, name: str = "response"
    ) -> tuple[dict | None, str | None, str | None]:
        """One structured-JSON completion against an arbitrary schema.

        Returns ``(payload, served_model, error)``. Used by the ablation study,
        which needs a different output shape from the investigator's. Shares the
        same transport, retry, rate-limit and parameter-dropping behaviour so
        the arms are not accidentally measured under different conditions.
        """
        if not self.supports_json_schema:
            system = (
                system
                + "\n\nRespond with a single JSON object and nothing else, matching "
                "exactly this schema:\n" + json.dumps(schema)
            )
        body = {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            **({} if os.environ.get("LEDGERGUARD_NO_CACHE") else {"temperature": 0}),
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": self._response_format(schema, name),
        }
        if self.reasoning_effort and "reasoning_effort" not in self._disabled_params:
            body["reasoning_effort"] = self.reasoning_effort
        if "temperature" in self._disabled_params:
            body.pop("temperature", None)

        response, failure = self._post_with_retries(
            f"{self.base_url}/chat/completions",
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            body,
        )
        if response is None:
            error = failure.error or "request failed"
            rejected = self._rejected_parameter(error)
            if rejected:
                self._disabled_params.add(rejected)
                return self.complete_json(system, user, schema, name)
            if self._retire_current(error):
                return self.complete_json(system, user, schema, name)
            return None, None, error

        try:
            payload = response.json()
            raw = payload["choices"][0]["message"]["content"]
            served = payload.get("model") or self.model
        except Exception as exc:
            return None, None, f"unexpected response envelope: {exc}"
        if str(response.headers.get("x-omniroute-cache-hit", "")).lower() == "true":
            self.cache_hits += 1
        try:
            return json.loads(_strip_fences(raw)), served, None
        except json.JSONDecodeError as exc:
            if self._retire_current(f"unparseable output: {exc}"):
                return self.complete_json(system, user, schema, name)
            return None, served, f"response was not valid JSON: {exc}"

    def investigate(self, context: dict) -> InvestigationResult:
        """Investigate, advancing to the next route if the current one is spent."""
        last: InvestigationResult | None = None
        while True:
            outcome, retire_reason = self._investigate_once(context)
            if retire_reason is None:
                return outcome
            last = outcome
            if not self._retire_current(retire_reason):
                # Every route is spent. Report the last failure honestly; the
                # safety gate reads this as "cannot resolve", never as a close.
                return last

    def _investigate_once(
        self, context: dict
    ) -> tuple[InvestigationResult, str | None]:
        """One attempt on the current route.

        Returns ``(result, retire_reason)``. A non-None ``retire_reason`` means
        this route is spent -- rate limited past its retries, out of quota, or
        producing output that will not parse -- and the caller should advance.
        """
        from .provider import SYSTEM_PROMPT

        system = SYSTEM_PROMPT
        if not self.supports_json_schema:
            system += (
                "\n\nRespond with a single JSON object and nothing else, matching "
                "exactly this schema:\n" + json.dumps(INVESTIGATION_SCHEMA)
            )

        body = {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            # OmniRoute caches temperature-0 requests and ignores
            # Cache-Control: no-cache, so a deterministic dataset replays
            # instead of re-measuring. LEDGERGUARD_NO_CACHE=1 omits temperature
            # to force real calls, at the cost of deterministic sampling.
            **({} if os.environ.get("LEDGERGUARD_NO_CACHE") else {"temperature": 0}),
            # Some routers stream by default and emit keep-alive comments into
            # the body, which makes the response unparseable as JSON. Ask for a
            # single response explicitly rather than relying on the default.
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": "Investigate this reconciliation exception.\n\n"
                    + json.dumps(context, indent=2, default=str),
                },
            ],
            "response_format": self._response_format(),
        }
        if self.reasoning_effort and "reasoning_effort" not in self._disabled_params:
            body["reasoning_effort"] = self.reasoning_effort
        if "temperature" in self._disabled_params:
            body.pop("temperature", None)

        response, failure = self._post_with_retries(
            f"{self.base_url}/chat/completions",
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            body,
        )
        if response is None:
            error = failure.error or "request failed"
            # A 400 that names an optional tuning parameter means we sent
            # something this model does not accept. Drop the parameter and try
            # the same route again -- retiring a working model over our own
            # request shape would silently shrink the usable pool.
            rejected = self._rejected_parameter(error)
            if rejected:
                self._disabled_params.add(rejected)
                return self._investigate_once(context)
            # Transient failures were already retried inside the post helper, so
            # reaching here means this route is not going to work.
            return failure, error

        try:
            payload = response.json()
            raw = payload["choices"][0]["message"]["content"]
        except Exception as exc:
            return (
                InvestigationResult.invalid(f"unexpected response envelope: {exc}"),
                f"unusable response envelope: {exc}",
            )

        if str(response.headers.get("x-omniroute-cache-hit", "")).lower() == "true":
            self.cache_hits += 1

        usage = payload.get("usage") or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)

        served = payload.get("model") or self.model
        if served != self.model:
            self.served_models.add(served)
        result = _parse(raw, self.model, served_model=served)

        if result.source == "invalid_response":
            # A route that cannot produce parseable structured output is not
            # going to start; retire it rather than spending the whole run on it.
            return result, f"unparseable output: {result.error}"
        return result, None



class GeminiProvider:
    """Google Gemini via generateContent with a response schema."""

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.name = f"gemini:{model}"
        self.retries = 0
        self._client = httpx.Client(timeout=timeout)

    def complete_json(
        self, system: str, user: str, schema: dict, name: str = "response"
    ) -> tuple[dict | None, str | None, str | None]:
        """Structured JSON completion against an arbitrary schema."""
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0,
                "thinkingConfig": {"thinkingBudget": 0},
                "maxOutputTokens": self.max_tokens,
                "responseMimeType": "application/json",
                "responseSchema": self._clean_schema(schema),
            },
        }
        response, failure = OpenAICompatibleProvider._post_with_retries(
            self, url,
            {"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            body,
        )
        if response is None:
            return None, None, (failure.error if failure else "request failed")
        try:
            payload = response.json()
            raw = payload["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            return None, None, f"unexpected response envelope: {exc}"
        try:
            return json.loads(_strip_fences(raw)), self.model, None
        except json.JSONDecodeError as exc:
            return None, self.model, f"response was not valid JSON: {exc}"

    @staticmethod
    def _clean_schema(schema: dict) -> dict:
        """Gemini's schema dialect rejects additionalProperties."""

        def clean(node):
            if isinstance(node, dict):
                return {k: clean(v) for k, v in node.items() if k != "additionalProperties"}
            if isinstance(node, list):
                return [clean(v) for v in node]
            return node

        return clean(schema)

    @staticmethod
    def _schema() -> dict:
        """Gemini's schema dialect rejects additionalProperties."""

        def clean(node):
            if isinstance(node, dict):
                return {
                    k: clean(v) for k, v in node.items() if k != "additionalProperties"
                }
            if isinstance(node, list):
                return [clean(v) for v in node]
            return node

        return clean(INVESTIGATION_SCHEMA)

    def investigate(self, context: dict) -> InvestigationResult:
        from .provider import SYSTEM_PROMPT

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": "Investigate this reconciliation exception.\n\n"
                            + json.dumps(context, indent=2, default=str)
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                # Gemini bills thinking against maxOutputTokens, so leaving
                # thinking on at this ceiling truncates the JSON mid-string and
                # every response comes back as invalid. The investigator's job
                # here is selection against a small candidate set, not deep
                # reasoning, so thinking is switched off and the whole budget
                # goes to the structured answer.
                "thinkingConfig": {"thinkingBudget": 0},
                "maxOutputTokens": self.max_tokens,
                "responseMimeType": "application/json",
                "responseSchema": self._schema(),
            },
        }
        response, failure = OpenAICompatibleProvider._post_with_retries(
            self, url, {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}, body
        )
        if response is None:
            return failure
        try:
            payload = response.json()
            raw = payload["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            return InvestigationResult.invalid(f"unexpected response envelope: {exc}")

        return _parse(raw, self.model)
