"""Per-customer limits, token budget, metrics, and their place in the turn flow."""

import json

import boto3
import pytest
from botocore.exceptions import ClientError
from csagent_agents import service as svc
from csagent_agents.entrypoint import handle_invocation
from csagent_agents.metrics import CloudWatchMetrics, turn_metrics
from csagent_agents.ratelimit import (
    DynamoRateLimiter,
    LimiterUnavailable,
    Limits,
    RateLimited,
)
from csagent_agents.service_errors import RequestRejected
from moto import mock_aws
from test_guardrails import FakeSupervisor, ScriptedChecker, _token

TABLE = "ratelimit"
T0 = 1_800_000_000  # a fixed instant, aligned below to minute/hour/day starts where needed


class Clock:
    def __init__(self, now: float = T0 - (T0 % 86400)) -> None:  # start of a UTC day
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ddb(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "bucket", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "bucket", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def limiter(ddb, clock, **limits):
    return DynamoRateLimiter(ddb, TABLE, Limits(**limits), clock)


def count(ddb, customer, code, window):
    item = ddb.get_item(TableName=TABLE, Key={"bucket": {"S": f"{customer}#{code}#{window}"}})
    return int(item["Item"]["count"]["N"]) if "Item" in item else 0


# ---------------- limits ----------------


def test_minute_limit_blocks_then_resets_next_minute(ddb):
    clock = Clock()
    lim = limiter(ddb, clock, per_minute=3, per_hour=100, per_day=100)
    for _ in range(3):
        lim.acquire("cust-1")
    clock.now += 10
    with pytest.raises(RateLimited) as exc:
        lim.acquire("cust-1")
    assert exc.value.scope == "minute" and exc.value.retry_after == 50
    assert "too quickly" in str(exc.value) and "50 seconds" in str(exc.value)
    clock.now += 60
    lim.acquire("cust-1")  # new window


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(1, "1 second "), (50, "50 seconds"), (600, "10 minutes"), (7200, "2 hours")],
)
def test_wait_message_is_grammatical(seconds, text):
    assert text in str(RateLimited("minute", seconds)) + " "


def test_hour_and_day_limits_apply_across_minutes(ddb):
    clock = Clock()
    lim = limiter(ddb, clock, per_minute=100, per_hour=3, per_day=100)
    for _ in range(3):
        lim.acquire("cust-1")
        clock.now += 61
    with pytest.raises(RateLimited) as exc:
        lim.acquire("cust-1")
    assert exc.value.scope == "hour"

    clock = Clock()
    day_limited = limiter(ddb, clock, per_minute=100, per_hour=100, per_day=2)
    day_limited.acquire("cust-2")
    day_limited.acquire("cust-2")
    with pytest.raises(RateLimited) as exc:
        day_limited.acquire("cust-2")
    assert exc.value.scope == "day"


def test_rejected_request_consumes_no_quota_anywhere(ddb):
    clock = Clock()
    lim = limiter(ddb, clock, per_minute=100, per_hour=100, per_day=2)
    lim.acquire("cust-1")
    lim.acquire("cust-1")
    with pytest.raises(RateLimited):
        lim.acquire("cust-1")
    now = int(clock.now)
    # Atomic transaction: the day limit failed, so minute and hour were NOT incremented.
    assert count(ddb, "cust-1", "m", now // 60) == 2
    assert count(ddb, "cust-1", "h", now // 3600) == 2
    assert count(ddb, "cust-1", "d", now // 86400) == 2


def test_customers_are_limited_independently(ddb):
    clock = Clock()
    lim = limiter(ddb, clock, per_minute=1, per_hour=10, per_day=10)
    lim.acquire("cust-1")
    lim.acquire("cust-2")
    with pytest.raises(RateLimited):
        lim.acquire("cust-1")


def test_counters_expire_via_ttl_in_the_future(ddb):
    clock = Clock()
    limiter(ddb, clock).acquire("cust-1")
    items = ddb.scan(TableName=TABLE)["Items"]
    assert len(items) == 3
    assert all(int(i["expires_at"]["N"]) > clock.now + 3600 for i in items)


# ---------------- token budget ----------------


def test_daily_token_budget_blocks_without_consuming_turn_quota(ddb):
    clock = Clock()
    lim = limiter(ddb, clock, daily_tokens=1000)
    lim.acquire("cust-1")
    lim.record_tokens("cust-1", 600)
    lim.acquire("cust-1")
    lim.record_tokens("cust-1", 500)  # budget now exceeded
    before = count(ddb, "cust-1", "m", int(clock.now) // 60)
    with pytest.raises(RateLimited) as exc:
        lim.acquire("cust-1")
    assert exc.value.scope == "tokens" and "usage limit" in str(exc.value)
    assert count(ddb, "cust-1", "m", int(clock.now) // 60) == before
    clock.now += 86400  # next UTC day
    lim.acquire("cust-1")


def test_zero_tokens_are_not_recorded(ddb):
    limiter(ddb, Clock()).record_tokens("cust-1", 0)
    assert ddb.scan(TableName=TABLE)["Items"] == []


# ---------------- failure policy ----------------


class BrokenClient:
    def __init__(self, code):
        self.code = code

    def _fail(self, *a, **k):
        raise ClientError({"Error": {"Code": self.code, "Message": "x"}}, "Op")

    get_item = transact_write_items = update_item = _fail


def test_limiter_fails_closed_when_the_store_is_down():
    lim = DynamoRateLimiter(BrokenClient("ProvisionedThroughputExceededException"), TABLE)
    with pytest.raises(LimiterUnavailable):
        lim.acquire("cust-1")


def test_token_accounting_failure_never_breaks_a_turn():
    DynamoRateLimiter(BrokenClient("InternalServerError"), TABLE).record_tokens("c", 100)


# ---------------- turn flow ----------------


class RecordingMetrics:
    def __init__(self):
        self.batches: list[list[tuple]] = []

    def emit(self, metrics):
        self.batches.append(metrics)

    def names(self):
        return {name for batch in self.batches for name, _, _ in batch}

    def value(self, name):
        return next(v for batch in self.batches for n, v, _ in batch if n == name)


class FakeLimiter:
    def __init__(self, raises=None):
        self.raises, self.acquired, self.tokens = raises, [], []

    def acquire(self, customer_id):
        if self.raises:
            raise self.raises
        self.acquired.append(customer_id)

    def record_tokens(self, customer_id, tokens):
        self.tokens.append((customer_id, tokens))


@pytest.fixture
def make_service(monkeypatch):
    supervisor = FakeSupervisor()
    monkeypatch.setattr(svc, "make_model", lambda cfg: object())
    monkeypatch.setattr(svc, "build_supervisor", lambda model: supervisor)

    def make(limiter, checker=None, metrics=None):
        cfg = svc.AgentConfig(api_base_url="https://api.example.com/")
        service = svc.AgentService(
            cfg, checker=checker or ScriptedChecker(), limiter=limiter,
            metrics=metrics or RecordingMetrics(),
        )
        return service, supervisor

    return make


def _turn(service):
    return service.handle_turn(prompt="where is my laptop", session_id="s" * 40, id_token=_token())


def test_rate_limited_customer_never_reaches_guardrail_or_model(make_service):
    checker, metrics = ScriptedChecker(), RecordingMetrics()
    service, supervisor = make_service(FakeLimiter(RateLimited("minute", 42)), checker, metrics)
    with pytest.raises(RateLimited):
        _turn(service)
    assert checker.calls == [] and supervisor.prompts == []  # no paid work happened
    assert metrics.names() == {"RateLimited"}


def test_entrypoint_returns_a_machine_readable_rate_limit_error(make_service):
    service, _ = make_service(FakeLimiter(RateLimited("minute", 42)))
    out = handle_invocation({"prompt": "hi"}, "s" * 40, {"Authorization": f"Bearer {_token()}"},
                            service)
    assert out["error"]["code"] == "rate_limited" and out["error"]["retry_after_seconds"] == 42
    assert "42 seconds" in out["error"]["message"]


def test_successful_turn_acquires_records_tokens_and_emits_metrics(make_service):
    limiter, metrics = FakeLimiter(), RecordingMetrics()
    service, _ = make_service(limiter, metrics=metrics)
    out = _turn(service)
    assert limiter.acquired == ["cust-1"]
    expected = out["usage"]["input_tokens"] + out["usage"]["output_tokens"]
    assert limiter.tokens == [("cust-1", expected)]
    assert metrics.value("Turns") == 1 and metrics.value("LatencyMs") >= 0
    assert metrics.value("ToolErrors") == 0 and metrics.value("GuardrailInputBlocked") == 0


def test_a_crash_is_counted_and_reraised(make_service):
    from csagent_agents.guard import GuardrailUnavailable

    metrics = RecordingMetrics()
    service, _ = make_service(FakeLimiter(), ScriptedChecker(fail=True), metrics)
    with pytest.raises(GuardrailUnavailable):
        _turn(service)
    assert metrics.names() == {"TurnFailed"}


def test_other_rejections_are_counted_separately_from_rate_limits(make_service):
    metrics = RecordingMetrics()
    service, _ = make_service(FakeLimiter(), metrics=metrics)
    with pytest.raises(RequestRejected):
        service.handle_turn(prompt="   ", session_id="s" * 40, id_token=_token())
    assert metrics.names() == {"Rejected"}


# ---------------- metrics ----------------


def test_turn_metrics_reflect_trace_and_stop_reason():
    turn = {
        "stop_reason": "guardrail_input",
        "escalation_suggested": False,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "trace": [{"tool": "guardrail:input", "status": "blocked"}],
    }
    values = {n: v for n, v, _ in turn_metrics(turn, 1400)}
    assert values["GuardrailInputBlocked"] == 1 and values["Turns"] == 1
    assert values["LatencyMs"] == 1400 and values["LimitHit"] == 0

    busy = {
        "stop_reason": "end_turn",
        "escalation_suggested": True,
        "usage": {"input_tokens": 9000, "output_tokens": 700},
        "trace": [
            {"tool": "get_shipment_status", "status": "error"},
            {"tool": "search_company_policies", "status": "error"},
            {"tool": "claim_check", "status": "unverified"},
            {"tool": "guardrail:output", "status": "blocked"},
        ],
    }
    values = {n: v for n, v, _ in turn_metrics(busy, 100)}
    assert values["ToolErrors"] == 2 and values["ClaimCheckTriggered"] == 1
    assert values["GuardrailOutputBlocked"] == 1 and values["EscalationSuggested"] == 1
    assert values["InputTokens"] == 9000 and values["OutputTokens"] == 700


def test_cloudwatch_emitter_sends_only_environment_dimension_and_never_raises():
    sent = {}

    class Client:
        def put_metric_data(self, **kw):
            sent.update(kw)

    CloudWatchMetrics(Client(), "prod").emit([("Turns", 1, "Count")])
    assert sent["Namespace"] == "CSAgent"
    assert sent["MetricData"][0]["Dimensions"] == [{"Name": "Environment", "Value": "prod"}]
    assert "cust" not in json.dumps(sent)

    class Down:
        def put_metric_data(self, **kw):
            raise RuntimeError("cloudwatch down")

    CloudWatchMetrics(Down(), "prod").emit([("Turns", 1, "Count")])  # must not raise
