"""Knowledge base warm-up: fires once per new session, never blocks or fails a turn."""

import threading

from csagent_agents import service as svc


class FakeKbClient:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail
        self.done = threading.Event()

    def retrieve(self, **kwargs):
        self.calls += 1
        self.done.set()
        if self.fail:
            raise RuntimeError("DB cluster is resuming")
        return {"retrievalResults": []}


def _service(monkeypatch, client, kb_id="KB1"):
    monkeypatch.setattr(svc, "make_model", lambda cfg: object())
    monkeypatch.setattr(svc, "build_supervisor", lambda model: object())
    monkeypatch.setattr(svc.boto3, "client", lambda *a, **k: client)
    cfg = svc.AgentConfig(api_base_url="https://api.example.com/", knowledge_base_id=kb_id)
    return svc.AgentService(cfg)


def test_warm_up_runs_once_per_new_session(monkeypatch):
    client = FakeKbClient()
    service = _service(monkeypatch, client)
    service._session_for("session-a", "cust-1")
    assert client.done.wait(2)
    service._session_for("session-a", "cust-1")  # existing session: no new warm-up
    assert client.calls == 1


def test_warm_up_errors_are_swallowed(monkeypatch):
    client = FakeKbClient(fail=True)
    service = _service(monkeypatch, client)
    service._session_for("session-b", "cust-1")  # must not raise
    assert client.done.wait(2)


def test_no_knowledge_base_means_no_warm_up(monkeypatch):
    client = FakeKbClient()
    service = _service(monkeypatch, client, kb_id=None)
    service._session_for("session-c", "cust-1")
    assert not client.done.wait(0.3)
