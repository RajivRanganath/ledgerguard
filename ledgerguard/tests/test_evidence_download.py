"""Download must describe the displayed run, not a fresh favourable sample."""

import hashlib
import json

from fastapi.testclient import TestClient

from ledgerguard.ai.provider import HeuristicProvider
from ledgerguard.backend import app as dashboard


def test_download_matches_displayed_run_without_new_investigation(monkeypatch):
    calls = []

    def provider():
        calls.append(1)
        return HeuristicProvider()

    monkeypatch.setattr(dashboard, "get_provider", provider)
    dashboard._cached_state.cache_clear()
    try:
        with TestClient(dashboard.app) as client:
            state = client.get("/api/state").json()
            first = client.get("/api/evidence-download")
            second = client.get("/api/evidence-download")
        assert first.status_code == 200
        assert first.json() == state
        assert first.content == second.content
        assert len(first.json()["cases"]) == 85
        assert first.json()["unresolved"] == state["unresolved"]
        assert len(calls) == 1
        digest = hashlib.sha256(first.content).hexdigest()
        assert first.headers["x-content-sha256"] == digest
        assert digest[:16] in first.headers["content-disposition"]
        assert first.headers["cache-control"] == "no-store"
    finally:
        dashboard._cached_state.cache_clear()


def test_download_keeps_untrusted_text_as_data(monkeypatch):
    state = {"provider": "test", "reason": '<script>alert("x")</script> ₹', "cases": []}
    monkeypatch.setattr(dashboard, "_state", lambda: state)
    response = dashboard.api_evidence_download()
    assert json.loads(response.body) == state
    assert response.media_type == "application/json"
    assert response.headers["content-disposition"].startswith("attachment;")
