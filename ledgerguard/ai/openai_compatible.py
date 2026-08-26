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
        "model": "openai/gpt-oss-120b",
        "json_schema": True,
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "env": "CEREBRAS_API_KEY",
        "model": "gpt-oss-120b",
        "json_schema": True,
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "env": "NVIDIA_API_KEY",
        "model": "meta/llama-3.3-70b-instruct",
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
        "model": "auto/best-reasoning",
        "json_schema": True,
    },
    "openai_compatible": {
        "base_url": None,          # from LEDGERGUARD_BASE_URL
        "env": "LEDGERGUARD_API_KEY",
        "model": None,             # from LEDGERGUARD_MODEL
        "json_schema": True,
    },
}


class OpenAICompatibleProvider:
    """Chat-completions provider (Groq, Cerebras, NVIDIA, or any compatible host)."""

    def __init__(
        self,
        preset: str = "groq",
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        cfg = PRESETS.get(preset, PRESETS["openai_compatible"])
        self.base_url = (base_url or cfg["base_url"] or os.environ.get("LEDGERGUARD_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get(cfg["env"], "")
        self.model = model or cfg["model"] or os.environ.get("LEDGERGUARD_MODEL", "")
        self.supports_json_schema = bool(cfg["json_schema"])
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.preset = preset
        self.reasoning_effort = os.environ.get("LEDGERGUARD_REASONING_EFFORT") or None
        self.retries = 0
        #: Models that actually answered, when they differ from the one asked for.
        self.served_models: set[str] = set()
        if not (self.base_url and self.api_key and self.model):
            raise ValueError(f"provider {preset!r} is not fully configured")
        self.name = f"{preset}:{self.model}"
        self._client = httpx.Client(timeout=timeout)

    def _post_with_retries(self, url: str, headers: dict, body: dict):
        """POST, retrying rate limits and transient 5xx. Returns (response, failure)."""
        last = "unknown error"
        for attempt in range(MAX_RETRIES + 1):
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
                if attempt < MAX_RETRIES:
                    self.retries += 1
                    time.sleep(_retry_delay(response, attempt))
                    continue
                break
            if attempt < MAX_RETRIES:
                self.retries += 1
                time.sleep(min(2.0 * (2 ** attempt), MAX_RETRY_WAIT_SECONDS))
        return None, InvestigationResult.unavailable(last)

    def _response_format(self) -> dict:
        if self.supports_json_schema:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": "investigation",
                    "strict": True,
                    "schema": INVESTIGATION_SCHEMA,
                },
            }
        return {"type": "json_object"}

    def investigate(self, context: dict) -> InvestigationResult:
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
            "temperature": 0,
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

        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort

        response, failure = self._post_with_retries(
            f"{self.base_url}/chat/completions",
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            body,
        )
        if response is None:
            return failure
        try:
            payload = response.json()
            raw = payload["choices"][0]["message"]["content"]
        except Exception as exc:
            return InvestigationResult.invalid(f"unexpected response envelope: {exc}")

        served = payload.get("model") or self.model
        if served != self.model:
            self.served_models.add(served)
        return _parse(raw, self.model, served_model=served)


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
