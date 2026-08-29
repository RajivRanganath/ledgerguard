"""Offline regression tests for provider and route fallback behavior."""

from __future__ import annotations

import json

import httpx
import pytest

from ledgerguard.ai.fallback import FallbackProvider
from ledgerguard.ai.openai_compatible import OpenAICompatibleProvider
from ledgerguard.ai.schemas import InvestigationResult


def _success(model: str = "served-model") -> InvestigationResult:
    return InvestigationResult(
        hypothesis="insufficient_evidence",
        reason="A usable structured answer.",
        recommended_action="review",
        source="model",
        model_name=model,
    )


class ScriptedProvider:
    def __init__(self, name: str, *results) -> None:
        self.name = name
        self.results = list(results)
        self.calls = 0
        self.max_retries = 4

    def investigate(self, context: dict):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class JsonProvider(ScriptedProvider):
    def complete_json(self, system: str, user: str, schema: dict, name: str):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_fallback_rejects_an_invalid_retirement_threshold():
    with pytest.raises(ValueError, match="at least 1"):
        FallbackProvider([ScriptedProvider("provider:a", _success())], 0)


def test_failed_provider_falls_through_is_logged_and_success_is_attributed():
    primary = ScriptedProvider(
        "primary:requested-model", InvestigationResult.unavailable("quota exhausted")
    )
    backup = ScriptedProvider("backup:requested-model", _success("actual-served-model"))
    fallback = FallbackProvider([primary, backup])

    result = fallback.investigate({"case": "x"})

    assert result.model_name == "actual-served-model"
    assert primary.calls == backup.calls == 1
    report = fallback.report()
    assert report["served_by"] == {"backup:actual-served-model": 1}
    assert report["failures"] == [
        {
            "operation": "investigate",
            "provider": "primary:requested-model",
            "error": "quota exhausted",
        }
    ]


def test_all_provider_errors_are_preserved_in_the_exhaustion_result():
    fallback = FallbackProvider(
        [
            ScriptedProvider("primary:a", InvestigationResult.unavailable("quota")),
            ScriptedProvider("backup:b", InvestigationResult.unavailable("timeout")),
        ]
    )

    result = fallback.investigate({})

    assert result.source == "unavailable"
    assert "primary:a: quota" in result.error
    assert "backup:b: timeout" in result.error
    assert [entry["error"] for entry in fallback.report()["failures"]] == [
        "quota",
        "timeout",
    ]


def test_malformed_provider_return_falls_through_instead_of_crashing():
    malformed = ScriptedProvider("malformed:a", None)
    backup = ScriptedProvider("backup:b", _success())
    fallback = FallbackProvider([malformed, backup])

    result = fallback.investigate({})

    assert result.source == "model"
    assert "returned NoneType" in fallback.report()["failures"][0]["error"]


def test_provider_retires_after_threshold_and_is_skipped_afterward():
    primary = ScriptedProvider(
        "primary:a",
        InvestigationResult.unavailable("quota-1"),
        InvestigationResult.unavailable("quota-2"),
    )
    backup = ScriptedProvider("backup:b", _success(), _success(), _success())
    fallback = FallbackProvider([primary, backup], failures_before_retiring=2)

    fallback.investigate({"case": 1})
    fallback.investigate({"case": 2})
    fallback.investigate({"case": 3})

    assert primary.calls == 2
    assert backup.calls == 3
    assert fallback.report()["retired"] == {"primary": "quota-2"}


def test_structured_json_fallback_logs_failure_and_attributes_served_model():
    primary = JsonProvider("primary:a", (None, None, "bad json"))
    backup = JsonProvider("backup:b", ({"ok": True}, "actual-json-model", None))
    fallback = FallbackProvider([primary, backup])

    payload, served, error = fallback.complete_json("system", "user", {}, "result")

    assert (payload, served, error) == ({"ok": True}, "actual-json-model", None)
    report = fallback.report()
    assert report["served_by"] == {"backup:actual-json-model": 1}
    assert report["failures"][0] == {
        "operation": "complete_json",
        "provider": "primary:a",
        "error": "bad json",
    }


def test_openai_compatible_rotates_routes_and_credits_response_model():
    provider = OpenAICompatibleProvider(
        preset="openai_compatible",
        models=["requested-a", "requested-b"],
        api_key="test-key",
        base_url="https://provider.invalid/v1",
    )
    provider.max_retries = 0
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    valid = {
        "hypothesis": "insufficient_evidence",
        "reason": "Review required.",
        "required_evidence": [],
        "candidate_evidence_ids": [],
        "recommended_action": "review",
    }

    class RotatingClient:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return httpx.Response(429, text="quota", request=request)
            return httpx.Response(
                200,
                json={
                    "model": "actual-upstream-model",
                    "choices": [{"message": {"content": json.dumps(valid)}}],
                },
                request=request,
            )

    provider._client = RotatingClient()

    result = provider.investigate({"case": "rotation"})

    assert result.source == "model"
    assert result.model_name == "actual-upstream-model"
    assert provider.exhausted == {"requested-a": "api error 429: quota"}
    assert provider.model == "requested-b"
