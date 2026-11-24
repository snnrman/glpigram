"""Workload warnings count active work and fail quietly within a fixed budget."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from bot import texts
from bot.glpi.client import GlpiError
from bot.glpi.models import WorkloadTicket
from bot.schedule import WorkSchedule
from bot.services.workload import WorkloadService, classify_workload
from bot.texts import workload as workload_texts

PILOT = 990948
CURRENT = 999
KGD = ZoneInfo("Europe/Kaliningrad")
WORKING_NOW = datetime(2026, 11, 24, 12, 0, tzinfo=KGD)


def row(
    ident,
    *,
    status=1,
    urgency=3,
    validation=1,
    assignees=(),
    entity_id=0,
    deleted=False,
):
    return WorkloadTicket(
        id=ident,
        status=status,
        urgency=urgency,
        global_validation=validation,
        assignee_ids=assignees,
        entity_id=entity_id,
        is_deleted=deleted,
    )


def classify(tickets, *, waiting=(), excluded=()):
    return classify_workload(
        tickets,
        ticket_id=CURRENT,
        waiting_ticket_ids=set(waiting),
        excluded_ticket_ids=set(excluded),
        entity_ids={0},
    )


@pytest.fixture
def env():
    settings = SimpleNamespace(
        workload_notice_enabled=True,
        workload_notice_pilot_tg_ids={PILOT},
        workload_excluded_ticket_ids=set(),
        workload_notice_entity_ids={0},
        workload_notice_timeout=3.0,
        workload_notice_cache_ttl=30.0,
    )
    client = SimpleNamespace(
        list_workload_tickets=AsyncMock(return_value=[row(i) for i in (1, 2, 3)])
    )
    repo = SimpleNamespace(
        get_workload_waiting_ticket_ids=AsyncMock(return_value=set()),
        save_workload_observation=AsyncMock(),
    )
    clock = SimpleNamespace(now=WORKING_NOW)
    schedule = WorkSchedule.from_config("09:00-18:00", "1-5", tz_name="Europe/Kaliningrad")
    return SimpleNamespace(
        settings=settings, client=client, repo=repo, clock=clock, schedule=schedule
    )


def service(env, *, use_default_clock=False):
    return WorkloadService(
        env.client,
        env.repo,
        env.settings,
        schedule=env.schedule,
        now_provider=None if use_default_clock else lambda: env.clock.now,
    )


async def notice(svc, *, ticket_id=CURRENT, requester_tg_id=PILOT):
    return await svc.notice_for(ticket_id=ticket_id, requester_tg_id=requester_tg_id)


def observation(env):
    args = env.repo.save_workload_observation.await_args.args
    assert args[:3] == (CURRENT, PILOT, int(env.clock.now.timestamp()))
    return args[3]


def test_classifier_filters_nonwork_and_uses_real_assignment():
    snapshot = classify(
        [
            row(1),
            row(2, status=2, urgency=5),
            row(3, urgency=5, assignees=(42,)),
            row(4, status=2, assignees=(9, 11)),
            row(5, status=3, urgency=5),
            row(6, status=4, urgency=5),
            row(7, status=5),
            row(8, status=6),
            row(9, validation=2),
            row(10, validation=4),
            row(11),
            row(12),
            row(CURRENT, urgency=5),
            row(13, deleted=True),
            row(14, entity_id=1),
            row(15, validation=3),
            row(16, status=0),
            row(17, urgency=6),
        ],
        waiting={11},
        excluded={12},
    )

    assert snapshot.queue_ids == {1, 2, 15, 17}
    assert snapshot.assigned_ids == {3, 4}
    assert snapshot.urgent_unassigned_ids == {2}
    assert snapshot.urgent_assigned_ids == {3}


def test_duplicates_are_one_ticket_and_split_assignments_are_combined():
    snapshot = classify(
        [
            row(1),
            row(1),
            row(2, urgency=5),
            row(2, urgency=5, assignees=(9,)),
            row(2, urgency=5, assignees=(11,)),
            row(3),
            row(3, status=4),
        ]
    )

    assert snapshot.queue_ids == {1}
    assert snapshot.assigned_ids == {2}
    assert snapshot.urgent_unassigned_ids == set()
    assert snapshot.urgent_assigned_ids == {2}


@pytest.mark.parametrize("urgency", [1, 2, 3, 4, 5, 6])
def test_only_exact_urgency_five_counts_as_production_incident(urgency):
    snapshot = classify([row(1, urgency=urgency), row(2, urgency=urgency, assignees=(9,))])
    assert snapshot.urgent_unassigned_ids == ({1} if urgency == 5 else set())
    assert snapshot.urgent_assigned_ids == ({2} if urgency == 5 else set())


@pytest.mark.parametrize(
    ("count", "prefix"), [(0, None), (1, None), (2, None), (3, "🟡"), (5, "🟡"), (6, "🟠")]
)
def test_queue_thresholds_never_issue_a_green_notice(count, prefix):
    result = workload_texts.notice(queue_count=count)
    if prefix is None:
        assert result is None
    else:
        assert result.startswith(prefix)
        assert f"В очереди ещё {count}" in result
        assert "Ответ может занять больше времени" in result
    assert "🟢" not in (result or "")


async def test_incidents_do_not_change_queue_notice(env):
    env.client.list_workload_tickets.return_value = [
        row(1, urgency=5),
        row(2, urgency=5),
        row(3),
        row(4, urgency=5, assignees=(9,)),
    ]
    result = await notice(service(env))
    assert result == workload_texts.notice(queue_count=3)
    assert "авари" not in result.lower() and "🚨" not in result
    assert "вся команда" not in result
    assert "все техники" not in result
    assert "перед вами" not in result
    assert "минут" not in result


@pytest.mark.parametrize(("unassigned", "assigned"), [(1, 0), (0, 1)])
async def test_incidents_alone_do_not_trigger_a_notice(env, unassigned, assigned):
    env.client.list_workload_tickets.return_value = (
        [row(1, urgency=5)] if unassigned else [row(2, urgency=5, assignees=(9,))]
    )
    assert await notice(service(env)) is None
    assert observation(env)["notice_prepared"] is False


def test_copy_supports_english(monkeypatch):
    monkeypatch.setattr(texts, "_lang", "en")
    result = workload_texts.notice(queue_count=6)
    assert result.startswith("🟠")
    assert "6 other tickets" in result
    assert "incident" not in result and "technicians" not in result


@pytest.mark.parametrize(
    ("enabled", "pilots", "requester"),
    [
        (False, {PILOT}, PILOT),
        (True, set(), PILOT),
        (True, {PILOT}, 123),
        (True, {PILOT}, str(PILOT)),
        (True, {1}, True),
    ],
)
async def test_pilot_eligibility_is_explicit_and_strict(env, enabled, pilots, requester):
    env.settings.workload_notice_enabled = enabled
    env.settings.workload_notice_pilot_tg_ids = pilots

    assert await notice(service(env), requester_tg_id=requester) is None

    env.client.list_workload_tickets.assert_not_awaited()
    env.repo.get_workload_waiting_ticket_ids.assert_not_awaited()
    env.repo.save_workload_observation.assert_not_awaited()


@pytest.mark.parametrize(
    "moment",
    [
        WORKING_NOW.replace(hour=8, minute=59),
        WORKING_NOW.replace(hour=18),
        datetime(2026, 11, 28, 12, tzinfo=KGD),
        datetime(2026, 11, 24, 16, tzinfo=UTC),
    ],
)
async def test_outside_working_hours_never_fetches_or_notifies(env, moment):
    env.clock.now = moment

    assert await notice(service(env)) is None

    env.client.list_workload_tickets.assert_not_awaited()
    env.repo.get_workload_waiting_ticket_ids.assert_not_awaited()


async def test_clock_defaults_to_schedule_timezone(env, monkeypatch):
    monkeypatch.setattr(WorkSchedule, "now", lambda self: WORKING_NOW)
    assert (await notice(service(env, use_default_clock=True))).startswith("🟡")
    assert observation(env)["status"] == "ok"


async def test_current_ticket_and_configured_test_tickets_do_not_inflate_load(env):
    env.settings.workload_excluded_ticket_ids = {752, 753}
    env.client.list_workload_tickets.return_value = [
        row(CURRENT, urgency=5),
        row(752, urgency=5),
        row(753),
        row(1),
        row(2),
    ]

    assert await notice(service(env)) is None

    data = observation(env)
    assert data["queue_ids"] == [1, 2]
    assert data["urgent_unassigned_count"] == 0
    assert data["excluded_ticket_ids"] == [752, 753]
    assert data["notice_prepared"] is False


async def test_observation_contains_counts_and_ids_without_titles_or_assignee_names(env):
    env.client.list_workload_tickets.return_value += [row(4, urgency=5, assignees=(9, 11))]

    result = await notice(service(env))

    assert "В очереди ещё 3 заявки" in result
    assert "авари" not in result.lower()
    env.client.list_workload_tickets.assert_awaited_once_with(entity_ids={0})
    data = observation(env)
    assert data["queue_count"] == 3
    assert data["assigned_count"] == 1
    assert data["assigned_ids"] == [4]
    assert data["snapshot_age_seconds"] == 0
    assert data["cache_hit"] is False
    assert "name" not in json.dumps(data)
    assert "title" not in json.dumps(data)
    assert "assignee_ids" not in data


async def test_local_approval_is_fresh_even_when_glpi_snapshot_is_cached(env):
    svc = service(env)
    assert (await notice(svc)).startswith("🟡")
    env.clock.now += timedelta(seconds=10)
    env.repo.get_workload_waiting_ticket_ids.return_value = {1}

    assert await notice(svc) is None

    env.client.list_workload_tickets.assert_awaited_once()
    assert env.repo.get_workload_waiting_ticket_ids.await_count == 2
    data = observation(env)
    assert data["snapshot_age_seconds"] == 10
    assert data["cache_hit"] is True
    assert data["queue_ids"] == [2, 3]
    assert data["local_waiting_ticket_ids"] == [1]


async def test_cache_expires_after_ttl_and_includes_the_exact_boundary(env):
    svc = service(env)
    await notice(svc)
    env.clock.now += timedelta(seconds=30)
    await notice(svc)
    assert env.client.list_workload_tickets.await_count == 1
    env.clock.now += timedelta(microseconds=1)
    await notice(svc)
    assert env.client.list_workload_tickets.await_count == 2


@pytest.mark.parametrize("clock_backwards", [False, True])
async def test_disabled_cache_or_backward_clock_forces_refresh(env, clock_backwards):
    if not clock_backwards:
        env.settings.workload_notice_cache_ttl = 0
    svc = service(env)
    await notice(svc)
    if clock_backwards:
        env.clock.now -= timedelta(seconds=1)
    await notice(svc)
    assert env.client.list_workload_tickets.await_count == 2


async def test_failed_refresh_never_falls_back_to_a_previous_snapshot(env, caplog):
    svc = service(env)
    assert await notice(svc) is not None
    env.clock.now += timedelta(seconds=31)
    env.client.list_workload_tickets.side_effect = GlpiError("secret-token-in-error")

    assert await notice(svc) is None

    data = observation(env)
    assert data == {"status": "error", "error_type": "GlpiError", "notice_prepared": False}
    assert "secret-token" not in caplog.text
    assert "Traceback" not in caplog.text
    env.client.list_workload_tickets.side_effect = None
    env.client.list_workload_tickets.return_value = []
    assert await notice(svc) is None
    assert env.client.list_workload_tickets.await_count == 3
    assert observation(env)["queue_count"] == 0


async def test_concurrent_refreshes_share_one_glpi_request(env):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(**kwargs):
        entered.set()
        await release.wait()
        return [row(1), row(2), row(3)]

    env.client.list_workload_tickets.side_effect = fetch
    svc = service(env)
    first = asyncio.create_task(notice(svc))
    await entered.wait()
    second = asyncio.create_task(notice(svc, ticket_id=CURRENT + 1))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)

    assert all(result.startswith("🟡") for result in results)
    env.client.list_workload_tickets.assert_awaited_once()
    assert env.repo.get_workload_waiting_ticket_ids.await_count == 2


@pytest.mark.parametrize("failing_step", ["api", "local_approval"])
async def test_total_timeout_is_bounded_and_audited_without_a_notice(env, failing_step):
    env.settings.workload_notice_timeout = 0.1

    async def never_finishes(*args, **kwargs):
        await asyncio.Event().wait()

    if failing_step == "api":
        env.client.list_workload_tickets.side_effect = never_finishes
    else:
        env.repo.get_workload_waiting_ticket_ids.side_effect = never_finishes
    start = asyncio.get_running_loop().time()

    assert await notice(service(env)) is None

    assert asyncio.get_running_loop().time() - start < 0.25
    assert observation(env)["error_type"] == "TimeoutError"


async def test_local_approval_error_suppresses_cached_evidence(env, caplog):
    svc = service(env)
    assert await notice(svc) is not None
    env.repo.get_workload_waiting_ticket_ids.side_effect = RuntimeError("private database path")

    assert await notice(svc) is None

    assert observation(env)["error_type"] == "RuntimeError"
    assert "private database path" not in caplog.text


@pytest.mark.parametrize("hangs", [False, True])
async def test_audit_failure_never_suppresses_an_available_notice(env, caplog, hangs):
    env.settings.workload_notice_timeout = 0.1
    if hangs:

        async def stuck(*args):
            await asyncio.Event().wait()

        env.repo.save_workload_observation.side_effect = stuck
    else:
        env.repo.save_workload_observation.side_effect = RuntimeError("private audit detail")
    start = asyncio.get_running_loop().time()

    assert (await notice(service(env))).startswith("🟡")

    assert asyncio.get_running_loop().time() - start < 0.25
    assert "private audit detail" not in caplog.text


@pytest.mark.parametrize("cancel_step", ["api", "local_approval", "audit"])
async def test_cancellation_propagates_from_every_await(env, cancel_step):
    target = {
        "api": env.client.list_workload_tickets,
        "local_approval": env.repo.get_workload_waiting_ticket_ids,
        "audit": env.repo.save_workload_observation,
    }[cancel_step]
    target.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await notice(service(env))
