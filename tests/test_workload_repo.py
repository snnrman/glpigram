"""Workload observations and exclusions against real SQLite, with no external I/O."""

from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import ValidationError

from bot.config import Settings
from bot.db.repo import Repo

DAY = 86400


@pytest.fixture
async def repo(tmp_path):
    repo = Repo(str(tmp_path / "workload.sqlite3"))
    await repo.connect()
    try:
        yield repo
    finally:
        await repo.close()


async def _observations(repo):
    async with repo._conn.execute(
        "SELECT * FROM workload_observations ORDER BY ticket_id"
    ) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def test_waiting_ids_include_all_unfinished_and_rejected_approval_paths_in_one_query(repo):
    async with repo._tx() as db:
        await db.executemany(
            "INSERT INTO access_approvals "
            "(ticket_id,validation_id,lead_glpi_id,lead_tg_id,requester_tg_id,status,requested_at) "
            "VALUES (?,1,2,3,4,?,100)",
            [(10, 0), (11, -1), (12, 1), (13, 1), (20, 0)],
        )
        await db.executemany(
            "INSERT INTO tech_approval_requests(ticket_id,token,payload,phase,created_at) "
            "VALUES (?,?, '{}',?,100)",
            [
                (tid, f"token-{tid}", phase)
                for tid, phase in [
                    (20, "prepared"),
                    (21, "posting"),
                    (22, "registered"),
                    (23, "delivery_pending"),
                    (24, "future_phase"),
                    (30, "done"),
                    (31, "failed"),
                    (32, "cancelled"),
                    (11, "done"),
                ]
            ],
        )
        await db.executemany(
            "INSERT INTO claude_approvals(ticket_id,request_text,stage,stage_started_at) "
            "VALUES (?,'request',?,100)",
            [
                (40, "lead"),
                (41, "extra"),
                (42, "approve_pending"),
                (43, "reject_pending"),
                (44, "rejected"),
                (45, "cancelled"),
                (46, "approved"),
                (13, "extra"),
                (12, "approved"),
            ],
        )
    # An AI suggestion is not an approval workflow and must not exclude work.
    await repo.save_ticket_intake(
        60, {"category": "accounts", "category_id": 7, "approval_required": True}, 100
    )
    statements = []
    await repo._conn.set_trace_callback(statements.append)
    try:
        waiting = await repo.get_workload_waiting_ticket_ids()
    finally:
        await repo._conn.set_trace_callback(None)
    assert waiting == {10, 11, 13, 20, 21, 22, 23, 24, 40, 41, 42, 43, 44, 45}
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT")
    assert all(type(value) is int for value in waiting)


async def test_waiting_ids_follow_current_approval_state_without_a_cache(repo):
    assert await repo.get_workload_waiting_ticket_ids() == set()
    await repo.create_claude_approval(
        77,
        validation_id=10,
        lead_glpi_id=11,
        lead_tg_id=12,
        lead_name="Lead",
        requester_tg_id=13,
        requester_glpi_id=14,
        request_text="Request",
        members=[],
        now=100,
    )
    await repo.reserve_tech_approval(77, "token-77", "{}", 100)
    assert await repo.get_workload_waiting_ticket_ids() == {77}
    await repo.finish_claude(77, accepted=True)
    # The lead/service approval alone cannot hide an unfinished registration.
    assert await repo.get_workload_waiting_ticket_ids() == {77}
    await repo.update_tech_approval(77, "done")
    assert await repo.get_workload_waiting_ticket_ids() == set()


async def test_observation_upsert_preserves_requester_and_leaves_business_records_unchanged(repo):
    await repo.upsert_link(
        tg_id=990948, glpi_users_id=42, display_name="Pilot", is_tech=True, now=100
    )
    await repo.track_ticket(
        ticket_id=77, requester_tg_id=990948, requester_glpi_id=42, status=2, now=100
    )
    await repo.create_approval(
        77,
        validation_id=1,
        lead_glpi_id=2,
        lead_tg_id=3,
        lead_name="Lead",
        requester_tg_id=990948,
        now=100,
    )
    before_user = await repo.get_by_tg(990948)
    before_ticket = await repo.get_tracked_ticket(77)
    before_approval = dict(await repo.get_approval(77))
    await repo.save_workload_observation(
        77, 990948, 200, {"level": "Высокая", "active": 7, "ids": [1, 2]}
    )
    await repo.save_workload_observation(77, 123, 300, {"level": "Средняя", "active": 5})

    rows = await _observations(repo)
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == 77
    assert rows[0]["requester_tg_id"] == 990948
    assert rows[0]["observed_at"] == 300
    assert json.loads(rows[0]["data"]) == {"level": "Средняя", "active": 5}
    assert "Средняя" in rows[0]["data"]
    assert await repo.get_by_tg(990948) == before_user
    assert await repo.get_tracked_ticket(77) == before_ticket
    assert dict(await repo.get_approval(77)) == before_approval


async def test_observation_retention_removes_only_rows_older_than_thirty_days(repo):
    now = 90 * DAY
    cutoff = now - 30 * DAY
    await repo.save_workload_observation(1, 990948, cutoff - 1, {"level": "old"})
    await repo.save_workload_observation(2, 990948, cutoff, {"level": "boundary"})
    await repo.save_workload_observation(3, 990948, cutoff + 1, {"level": "recent"})
    await repo.save_workload_observation(4, 990948, now, {"level": "current"})

    assert [row["ticket_id"] for row in await _observations(repo)] == [2, 3, 4]


async def test_failed_cleanup_rolls_back_the_observation_before_other_writes(repo):
    await repo.save_workload_observation(1, 990948, 1, {"level": "old"})
    async with repo._tx() as db:
        await db.execute(
            "CREATE TRIGGER reject_workload_delete BEFORE DELETE ON workload_observations "
            "BEGIN SELECT RAISE(ABORT, 'cleanup failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="cleanup failed"):
        await repo.save_workload_observation(2, 990948, 31 * DAY, {"level": "new"})
    # A later commit from an unrelated operation must not persist half an audit write.
    await repo.set_cursor("workload-test", 42)
    assert [row["ticket_id"] for row in await _observations(repo)] == [1]


async def test_observation_migration_is_additive_idempotent_and_survives_restart(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE users (tg_id INTEGER PRIMARY KEY, glpi_users_id INTEGER NOT NULL, "
            "display_name TEXT NOT NULL DEFAULT '', is_tech INTEGER NOT NULL DEFAULT 0, "
            "linked_at INTEGER NOT NULL, checked_at INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute("INSERT INTO users VALUES (990948,42,'Existing user',1,10,20)")
    repo = Repo(path)
    await repo.connect()
    try:
        existing = await repo.get_by_tg(990948)
        assert existing.display_name == "Existing user"
        assert existing.checked_at == 20
        # The audit has no FK that creates or requires a bot_tickets row.
        await repo.save_workload_observation(77, 990948, 100, {"level": "low"})
        assert await repo.get_tracked_ticket(77) is None
    finally:
        await repo.close()
    await repo.connect()
    try:
        assert await repo.get_by_tg(990948) == existing
        rows = await _observations(repo)
        assert len(rows) == 1
        assert json.loads(rows[0]["data"]) == {"level": "low"}
        assert await repo.get_workload_waiting_ticket_ids() == set()
    finally:
        await repo.close()


def _settings(**overrides):
    return Settings(
        _env_file=None,
        telegram_bot_token="test",
        glpi_api_url="https://glpi.invalid/apirest.php",
        glpi_user_token="test",
        **overrides,
    )


def test_workload_defaults_are_disabled_and_explicitly_scoped():
    settings = _settings()
    assert settings.workload_notice_enabled is False
    assert settings.workload_notice_pilot_tg_ids == set()
    assert settings.workload_notice_entity_ids == {0}
    assert settings.workload_excluded_ticket_ids == set()
    assert settings.workload_notice_timeout == 3.0
    assert settings.workload_notice_cache_ttl == 30.0


def test_enabling_workload_requires_its_own_nonempty_pilot_list():
    with pytest.raises(ValidationError, match="WORKLOAD_NOTICE_PILOT_TG_IDS"):
        _settings(workload_notice_enabled=True, ai_intake_pilot_tg_ids={990948})
    settings = _settings(workload_notice_enabled=True, workload_notice_pilot_tg_ids={990948})
    assert settings.workload_notice_enabled and not settings.ai_intake_enabled


@pytest.mark.parametrize("field", ["workload_notice_pilot_tg_ids", "workload_excluded_ticket_ids"])
@pytest.mark.parametrize("invalid", [[0], [-1], [True], ["990948"], [1.5]])
def test_workload_user_and_ticket_ids_must_be_positive_strict_integers(field, invalid):
    with pytest.raises(ValidationError):
        _settings(**{field: invalid})


@pytest.mark.parametrize("invalid", [[], [-1], [True], ["0"], [1.5]])
def test_workload_entity_scope_cannot_be_empty_or_contain_invalid_ids(invalid):
    with pytest.raises(ValidationError):
        _settings(workload_notice_entity_ids=invalid)


@pytest.mark.parametrize("entities", [{0}, {0, 1}, {7}])
def test_workload_entity_scope_accepts_root_and_selected_entities(entities):
    assert _settings(workload_notice_entity_ids=entities).workload_notice_entity_ids == entities


@pytest.mark.parametrize(
    "field,invalid",
    [
        ("workload_notice_timeout", 0.99),
        ("workload_notice_timeout", 5.01),
        ("workload_notice_cache_ttl", -0.01),
        ("workload_notice_cache_ttl", 60.01),
    ],
)
def test_workload_deadline_and_cache_are_bounded(field, invalid):
    with pytest.raises(ValidationError):
        _settings(**{field: invalid})


@pytest.mark.parametrize("timeout,cache_ttl", [(1, 0), (5, 60)])
def test_workload_deadline_and_cache_boundaries_are_valid(timeout, cache_ttl):
    settings = _settings(workload_notice_timeout=timeout, workload_notice_cache_ttl=cache_ttl)
    assert settings.workload_notice_timeout == timeout
    assert settings.workload_notice_cache_ttl == cache_ttl


def test_workload_json_environment_configuration(monkeypatch):
    monkeypatch.setenv("WORKLOAD_NOTICE_ENABLED", "true")
    monkeypatch.setenv("WORKLOAD_NOTICE_PILOT_TG_IDS", "[990948]")
    monkeypatch.setenv("WORKLOAD_EXCLUDED_TICKET_IDS", "[714,713]")
    monkeypatch.setenv("WORKLOAD_NOTICE_ENTITY_IDS", "[0,2]")
    monkeypatch.setenv("WORKLOAD_NOTICE_TIMEOUT", "2.5")
    monkeypatch.setenv("WORKLOAD_NOTICE_CACHE_TTL", "0")
    settings = _settings()
    assert settings.workload_notice_enabled
    assert settings.workload_notice_pilot_tg_ids == {990948}
    assert settings.workload_excluded_ticket_ids == {713, 714}
    assert settings.workload_notice_entity_ids == {0, 2}
    assert settings.workload_notice_timeout == 2.5
    assert settings.workload_notice_cache_ttl == 0
