from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from bot.ai_shadow_report import report
from bot.db.repo import Repo
from bot.services.ai_shadow import (
    MAX_INPUT_CHARS,
    PROMPT_VERSION,
    Classification,
    ClassifierError,
    Observation,
    OpenAIClassifier,
    ShadowClassifier,
    prepare_input,
)

URL = "https://api.openai.com/v1/responses"
MODEL = "gpt-5-mini-2025-08-07"


def response_body(**overrides):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"category":"paid_access","service":"chatgpt"}',
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 30},
        **overrides,
    }


@pytest.fixture
async def repo(tmp_path):
    db = Repo(str(tmp_path / "test.sqlite3"))
    await db.connect()
    yield db
    await db.close()


async def row(repo, ticket_id=1):
    async with repo._conn.execute("SELECT * FROM ai_shadow WHERE ticket_id=?", (ticket_id,)) as cur:
        return await cur.fetchone()


def fake_client():
    client = AsyncMock()
    client.model = MODEL
    client.classify.return_value = Observation(
        Classification(category="paid_access", service="chatgpt"), 100, 30
    )
    return client


def test_input_masks_identifiers_and_credentials_but_retains_service():
    key = "sk-proj-" + "x" * 40
    payload = prepare_input(
        "Вход",
        f"ChatGPT {key} пароль: secret123 "
        "user@example.com @employee +7 (900) 111-22-33 10.2.3.4 "
        "https://internal.example/path?token=private https://claude.ai/new?session=hidden",
    )
    for secret in (
        key,
        "secret123",
        "user@example.com",
        "employee",
        "900",
        "10.2.3.4",
        "internal.example",
        "private",
        "hidden",
    ):
        assert secret not in payload
    assert "ChatGPT" in payload and "Claude" in payload
    assert set(json.loads(payload)) == {"title", "description"}


def test_long_input_is_skipped_instead_of_silently_cutting_important_context():
    with pytest.raises(ClassifierError, match="input_too_long"):
        prepare_input("", "x" * (MAX_INPUT_CHARS + 1))


def test_evaluation_set_has_valid_unique_labels():
    cases = json.loads((Path(__file__).parent / "fixtures/ai_shadow_cases.json").read_text())
    assert len(cases) == len({c["id"] for c in cases}) >= 25
    assert {c["expected"]["category"] for c in cases} == {
        "paid_access",
        "technical_issue",
        "other",
        "unclear",
    }
    for case in cases:
        Classification.model_validate(case["expected"])
        prepare_input(case["title"], case["description"])


@respx.mock
async def test_structured_response_and_request_have_no_tools_or_storage():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=response_body()))
    client = OpenAIClassifier("test-secret", model=MODEL)
    try:
        result = await client.classify(prepare_input("T", "Нужен ChatGPT"))
    finally:
        await client.close()
    sent = json.loads(route.calls[0].request.content)
    assert sent["store"] is False
    assert sent["max_output_tokens"] == 512
    assert sent["text"]["format"]["strict"] is True
    assert "tools" not in sent and "test-secret" not in json.dumps(sent)
    assert result.classification.category == "paid_access"
    assert (result.input_tokens, result.output_tokens) == (100, 30)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503, 302])
@respx.mock
async def test_http_errors_are_not_retried_or_leaked(status):
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            status, json={"error": {"message": "test-secret sensitive request text"}}
        )
    )
    client = OpenAIClassifier("test-secret", model=MODEL)
    try:
        with pytest.raises(ClassifierError) as exc:
            await client.classify("{}")
        assert str(exc.value) == f"http_{status}"
        assert route.call_count == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "body,code",
    [
        (response_body(status="incomplete"), "incomplete"),
        (response_body(output=[{"content": [{"type": "refusal", "refusal": "x"}]}]), "refusal"),
        (response_body(output=[]), "invalid_response"),
        (
            response_body(output=[{"content": [{"type": "output_text", "text": "{}"}]}]),
            "invalid_response",
        ),
        (
            response_body(
                output=[
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"category":"approved","service":"claude"}',
                            }
                        ]
                    }
                ]
            ),
            "invalid_response",
        ),
        (response_body(usage={"input_tokens": -1, "output_tokens": 3}), "invalid_usage"),
        ([], "invalid_response"),
    ],
)
@respx.mock
async def test_invalid_refused_and_incomplete_results_are_not_decisions(body, code):
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    client = OpenAIClassifier("test-secret", model=MODEL)
    try:
        with pytest.raises(ClassifierError, match=code):
            await client.classify("{}")
    finally:
        await client.close()


@respx.mock
async def test_total_timeout():
    entered = 0

    async def slow(request):
        nonlocal entered
        entered += 1
        await asyncio.sleep(1)
        return httpx.Response(200, json=response_body())

    respx.post(URL).mock(side_effect=slow)
    client = OpenAIClassifier("test-secret", model=MODEL, timeout=0.01)
    try:
        with pytest.raises(ClassifierError, match="timeout"):
            await client.classify("{}")
        assert entered == 1
    finally:
        await client.close()


async def test_observe_never_calls_api_and_worker_only_writes_observation(repo):
    client = fake_client()
    shadow = ShadowClassifier(repo, client)
    await shadow.observe(1, "new", "Доступ", "Нужен ChatGPT user@example.com")
    client.classify.assert_not_awaited()
    assert "user@example.com" not in (await row(repo))["payload"]
    assert await shadow.tick()
    saved = await row(repo)
    assert saved["state"] == "done" and saved["payload"] == ""
    assert saved["category"] == "paid_access" and saved["prompt_version"] == PROMPT_VERSION
    assert saved["input_tokens"] == 100 and saved["output_tokens"] == 30
    for table in ("bot_tickets", "ticket_cards", "access_approvals", "claude_approvals"):
        async with repo._conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            assert (await cur.fetchone())[0] == 0


async def test_duplicate_tickets_and_restart_do_not_call_twice(repo):
    client = fake_client()
    shadow = ShadowClassifier(repo, client)
    await shadow.observe(1, "new", "T", "D")
    await shadow.observe(1, "new", "T", "D")
    await shadow.tick()
    await repo.close()
    await repo.connect()
    await repo.recover_ai_shadow(int(time.time()))
    await shadow.observe(1, "access", "changed", "changed")
    assert not await shadow.tick()
    client.classify.assert_awaited_once()
    assert (await row(repo))["source"] == "new"


async def test_failed_requests_consume_daily_limit_and_do_not_retry(repo):
    client = fake_client()
    client.classify.side_effect = ClassifierError("http_429")
    shadow = ShadowClassifier(repo, client, daily_limit=1)
    await shadow.observe(1, "new", "T", "D")
    await shadow.observe(2, "new", "T", "D")
    await shadow.tick()
    assert not await shadow.tick()
    assert (await row(repo, 1))["state"] == "error"
    assert (await row(repo, 1))["payload"] == ""
    assert (await row(repo, 2))["error_code"] == "daily_limit"
    client.classify.assert_awaited_once()


async def test_budget_reservation_is_atomic_and_resets_at_utc_day(repo):
    now = 2 * 86400 + 10
    for ticket_id in (1, 2):
        await repo.enqueue_ai_shadow(ticket_id, "new", "{}", now)
    claimed = await asyncio.gather(
        *[repo.claim_ai_shadow(now, 1, MODEL, PROMPT_VERSION) for _ in range(2)]
    )
    assert sum(r is not None for r in claimed) == 1
    await repo.enqueue_ai_shadow(3, "new", "{}", now + 86400)
    assert (await repo.claim_ai_shadow(now + 86400, 1, MODEL, PROMPT_VERSION))["ticket_id"] == 3


async def test_interrupted_call_is_not_replayed_and_pending_survives(repo):
    now = int(time.time())
    await repo.enqueue_ai_shadow(1, "new", "{}", now)
    await repo.enqueue_ai_shadow(2, "new", "{}", now)
    await repo.claim_ai_shadow(now, 100, MODEL, PROMPT_VERSION)
    await repo.close()
    await repo.connect()
    await repo.recover_ai_shadow(now)
    assert (await row(repo, 1))["error_code"] == "interrupted"
    assert (await row(repo, 1))["payload"] == ""
    assert (await row(repo, 2))["state"] == "pending"


async def test_enqueue_failure_is_isolated_and_logs_no_text(repo, monkeypatch, caplog):
    monkeypatch.setattr(repo, "enqueue_ai_shadow", AsyncMock(side_effect=RuntimeError("secret")))
    await ShadowClassifier(repo, fake_client()).observe(1, "new", "private text", "secret")
    assert "ai_shadow_enqueue_failed" in caplog.text
    assert "private text" not in caplog.text and "secret" not in caplog.text


async def test_oversize_job_and_report_do_not_expose_text(repo):
    client = fake_client()
    await ShadowClassifier(repo, client).observe(1, "new", "", "x" * (MAX_INPUT_CHARS + 1))
    saved = await row(repo)
    assert saved["state"] == "skipped" and saved["payload"] == ""
    assert saved["error_code"] == "input_too_long"
    await repo.enqueue_ai_shadow(2, "new", "private text", int(time.time()))
    rendered = json.dumps(report(repo._db_path))
    assert "private text" not in rendered and '"payload"' not in rendered
    client.classify.assert_not_awaited()
