"""Complete scoped workload snapshots; every HTTP request is mocked."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
import respx

from bot.glpi.client import GlpiClient, GlpiError, GlpiHTTPError
from bot.glpi.models import WorkloadTicket

pytestmark = pytest.mark.asyncio
BASE = "http://glpi.local/apirest.php"


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/initSession").respond(200, json={"session_token": "test-session"})
        yield router


@pytest_asyncio.fixture
async def client(api):
    value = GlpiClient(BASE, "test-app", "test-user", timeout=1)
    try:
        yield value
    finally:
        await value.close()


def _row(ticket_id=1, **overrides):
    return {"2": ticket_id, "12": 1, "10": 3, "52": 1, "5": None, **overrides}


def _page(rows, *, total=None, start=0):
    total = len(rows) if total is None else total
    return {
        "totalcount": total,
        "count": len(rows),
        "sort": [2],
        "order": ["ASC"],
        "content-range": f"{start}-{start + len(rows) - 1}/{total}",
        "data": rows,
    }


async def test_reads_all_pages_beyond_latest_hundred_with_strict_filters(api, client):
    rows = [_row(i) for i in range(1, 302)]

    def response(request):
        params = request.url.params
        assert params["criteria[0][field]"] == "12"
        assert params["criteria[0][value]"] == "notclosed"
        assert params["criteria[0][searchtype]"] == "equals"
        assert params["criteria[1][link]"] == "AND"
        assert params["criteria[1][field]"] == "80"
        assert params["criteria[1][searchtype]"] == "equals"
        assert params["criteria[1][value]"] == "0"
        assert params["is_deleted"] == "0"
        assert params["sort"] == "2" and params["order"] == "ASC"
        assert params["expand_dropdowns"] == "false"
        assert [params[f"forcedisplay[{i}]"] for i in range(5)] == ["2", "12", "10", "52", "5"]
        start = int(params["range"].split("-")[0])
        assert params["range"] == f"{start}-{start + 199}"
        return httpx.Response(206, json=_page(rows[start : start + 200], total=301, start=start))

    route = api.get(f"{BASE}/search/Ticket").mock(side_effect=response)
    result = await client.list_workload_tickets(entity_ids={0})
    assert [ticket.id for ticket in result] == list(range(1, 302))
    assert route.call_count == 2
    assert len(api.calls) == 3  # Session plus two searches; no actor lookups.


async def test_numeric_actors_are_deduplicated_without_name_lookups(api, client):
    rows = [
        _row(1, **{"5": [124, "42", "124"], "12": "2", "10": "5", "52": "2"}),
        _row(2, **{"5": "134"}),
        _row(3, **{"5": []}),
        _row(4, **{"5": None, "52": 0}),
    ]
    api.get(f"{BASE}/search/Ticket").respond(200, json=_page(rows))
    result = await client.list_workload_tickets(entity_ids={0})
    assert result[0] == WorkloadTicket(1, 2, 5, 2, (42, 124), 0)
    assert [ticket.assignee_ids for ticket in result[1:]] == [(134,), (), ()]
    assert all(ticket.is_deleted is False for ticket in result)
    assert len(api.calls) == 2


async def test_entity_id_comes_from_exact_query_not_expanded_entity_name(api, client):
    def response(request):
        entity = int(request.url.params["criteria[1][value]"])
        return httpx.Response(200, json=_page([_row(entity + 1, **{"80": "Same name"})]))

    route = api.get(f"{BASE}/search/Ticket").mock(side_effect=response)
    result = await client.list_workload_tickets(entity_ids={0, 3})
    assert [(ticket.id, ticket.entity_id) for ticket in result] == [(1, 0), (4, 3)]
    assert [call.request.url.params["criteria[1][value]"] for call in route.calls] == ["0", "3"]


@pytest.mark.parametrize("data", ["missing", None, []])
async def test_empty_search_supports_actual_glpi_response_without_data(api, client, data):
    payload = _page([])
    if data == "missing":
        del payload["data"]
    else:
        payload["data"] = data
    api.get(f"{BASE}/search/Ticket").respond(200, json=payload)
    assert await client.list_workload_tickets(entity_ids={0}) == []


@pytest.mark.parametrize(
    "scope", [set(), {-1}, {True}, {"0"}, {1.5}, list(range(2)), set(range(21))]
)
async def test_bad_entity_scope_fails_before_any_request(api, client, scope):
    with pytest.raises(GlpiError, match="entity scope"):
        await client.list_workload_tickets(entity_ids=scope)
    assert len(api.calls) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("2", True),
        ("2", 0),
        ("2", 1.5),
        ("2", "١"),
        ("2", "9999999999999999999999"),
        ("12", 0),
        ("12", 6),
        ("12", False),
        ("10", 0),
        ("10", 6),
        ("10", None),
        ("52", -1),
        ("52", 5),
        ("52", None),
        ("5", "Technician name"),
        ("5", ""),
        ("5", 0),
        ("5", -1),
        ("5", True),
        ("5", 1.5),
        ("5", {"id": 124}),
        ("5", [124, None]),
        ("5", [[124]]),
        ("5", [124, "unknown"]),
    ],
)
async def test_invalid_columns_fail_whole_snapshot(api, client, field, value):
    api.get(f"{BASE}/search/Ticket").respond(200, json=_page([_row(**{field: value})]))
    with pytest.raises(GlpiHTTPError, match="incomplete or invalid"):
        await client.list_workload_tickets(entity_ids={0})


@pytest.mark.parametrize("field", ["2", "12", "10", "52", "5"])
async def test_missing_required_column_does_not_turn_into_default_zero(api, client, field):
    row = _row()
    del row[field]
    api.get(f"{BASE}/search/Ticket").respond(200, json=_page([row]))
    with pytest.raises(GlpiHTTPError):
        await client.list_workload_tickets(entity_ids={0})


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {},
        {"totalcount": 1, "count": 1},
        {"totalcount": 1, "count": 1, "data": None},
        {"totalcount": 1, "count": 1, "data": {}},
        {"totalcount": 1, "count": 1, "data": [None]},
        {"totalcount": 1, "count": 0, "data": []},
        {"totalcount": 2, "count": 1, "data": [_row()]},
        {"totalcount": 1, "count": 2, "data": [_row()]},
        {"totalcount": "1", "count": 1, "data": [_row()]},
        {"totalcount": True, "count": 1, "data": [_row()]},
        {"totalcount": -1, "count": 0, "data": []},
        {"totalcount": 1, "count": True, "data": [_row()]},
        {"totalcount": 1, "count": 1.0, "data": [_row()]},
        {"totalcount": 1, "count": -1, "data": [_row()]},
        {**_page([_row()]), "content-range": "1-1/1"},
        {**_page([_row()]), "content-range": []},
        {**_page([]), "content-range": "0-1/2"},
    ],
)
async def test_bad_counts_rows_and_ranges_never_return_partial_snapshot(api, client, payload):
    api.get(f"{BASE}/search/Ticket").respond(200, json=payload)
    with pytest.raises(GlpiHTTPError):
        await client.list_workload_tickets(entity_ids={0})


async def test_non_json_search_reply_is_safe_glpi_error(api, client):
    api.get(f"{BASE}/search/Ticket").respond(200, text="private invalid JSON")
    with pytest.raises(GlpiHTTPError) as error:
        await client.list_workload_tickets(entity_ids={0})
    assert "private" not in str(error.value)


@pytest.mark.parametrize("failure", ["total_changed", "empty", "duplicate", "http"])
async def test_second_page_failure_never_returns_successful_first_page(api, client, failure):
    first = _page([_row(i) for i in range(1, 201)], total=201)
    second = _page([_row(201)], total=201, start=200)
    if failure == "total_changed":
        second = _page([_row(201), _row(202)], total=202, start=200)
    elif failure == "empty":
        second = _page([], total=201, start=200)
    elif failure == "duplicate":
        second = _page([_row(200)], total=201, start=200)
    response = httpx.Response(403) if failure == "http" else httpx.Response(200, json=second)
    route = api.get(f"{BASE}/search/Ticket").mock(
        side_effect=[httpx.Response(206, json=first), response]
    )
    with pytest.raises(GlpiError):
        await client.list_workload_tickets(entity_ids={0})
    assert route.call_count == 2


@pytest.mark.parametrize("ids", [[1, 1], [2, 1]])
async def test_duplicate_or_unordered_ids_fail_instead_of_hiding_rows(api, client, ids):
    api.get(f"{BASE}/search/Ticket").respond(200, json=_page([_row(i) for i in ids]))
    with pytest.raises(GlpiHTTPError):
        await client.list_workload_tickets(entity_ids={0})


async def test_duplicate_ticket_across_exact_entities_invalidates_scope(api, client):
    api.get(f"{BASE}/search/Ticket").respond(200, json=_page([_row()]))
    with pytest.raises(GlpiHTTPError):
        await client.list_workload_tickets(entity_ids={0, 1})


async def test_over_limit_snapshot_fails_without_reading_every_page(api, client):
    route = api.get(f"{BASE}/search/Ticket").respond(
        206, json=_page([_row(i) for i in range(1, 201)], total=2001)
    )
    with pytest.raises(GlpiHTTPError):
        await client.list_workload_tickets(entity_ids={0})
    assert route.call_count == 1


async def test_exact_limit_is_allowed_and_limit_applies_across_entities(api, client):
    def response(request):
        params = request.url.params
        if params["criteria[1][value]"] == "1":
            return httpx.Response(200, json=_page([_row(2001)]))
        start = int(params["range"].split("-")[0])
        return httpx.Response(
            206,
            json=_page([_row(i) for i in range(start + 1, start + 201)], total=2000, start=start),
        )

    api.get(f"{BASE}/search/Ticket").mock(side_effect=response)
    assert len(await client.list_workload_tickets(entity_ids={0})) == 2000
    with pytest.raises(GlpiHTTPError):
        await client.list_workload_tickets(entity_ids={0, 1})
