from app.llm_client import CircuitBreaker


def test_opens_after_threshold(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("app.llm_client.time.monotonic", lambda: clock["t"])
    b = CircuitBreaker(threshold=2, cooldown_sec=600)
    assert not b.is_open()
    b.record_failure()
    assert not b.is_open()          # одна неудача — ещё не паттерн
    b.record_failure()
    assert b.is_open()
    clock["t"] += 601
    assert not b.is_open()          # кулдаун истёк — пробуем снова


def test_success_resets(monkeypatch):
    monkeypatch.setattr("app.llm_client.time.monotonic", lambda: 0.0)
    b = CircuitBreaker(threshold=2, cooldown_sec=600)
    b.record_failure()
    b.record_success()
    b.record_failure()
    assert not b.is_open()
