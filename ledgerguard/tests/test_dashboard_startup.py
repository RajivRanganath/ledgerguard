"""A cold dashboard must not duplicate live provider work."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep

from ledgerguard.ai.provider import HeuristicProvider
from ledgerguard.backend import app as dashboard


def test_concurrent_dashboard_requests_share_one_initialization(monkeypatch):
    calls = []
    ready = Barrier(4)

    def provider():
        calls.append(1)
        sleep(0.05)
        return HeuristicProvider()

    monkeypatch.setattr(dashboard, "get_provider", provider)
    dashboard._cached_state.cache_clear()

    def request():
        ready.wait(timeout=5)
        return dashboard._state()

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            states = list(pool.map(lambda _: request(), range(4)))
        assert len(calls) == 1
        assert all(state is states[0] for state in states)
        assert len(states[0]["cases"]) == 85
        assert states[0]["adversarial"]["victim"]["state"] == "HUMAN_REVIEW_REQUIRED"
    finally:
        dashboard._cached_state.cache_clear()
