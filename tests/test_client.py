"""GLPI client tests with respx (mocked HTTP).

Covers session init, transparent 401 re-init + retry, ticket creation payload,
category pagination (206), retry policy for idempotent vs. side-effecting calls,
and error surfacing.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from bot.glpi.client import (
    GlpiAuthError,
    GlpiClient,
    GlpiHTTPError,
    GlpiNetworkError,
)

pytestmark = pytest.mark.asyncio

BASE = "http://glpi.local/apirest.php"
APP_TOKEN = "app-tok"
USER_TOKEN = "user-tok"


def make_client(app_token: str = APP_TOKEN) -> GlpiClient:
    return GlpiClient(BASE, app_token, USER_TOKEN, timeout=1.0)


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


async def _init_route(mock, token: str = "sess-1"):
    return mock.get(f"{BASE}/initSession").mock(
        return_value=httpx.Response(200, json={"session_token": token})
    )


# --- init session -----------------------------------------------------------
async def test_init_session_sets_token_and_headers(mock):
    route = await _init_route(mock)
    client = make_client()
    token = await client.init_session()
    assert token == "sess-1"
    assert client._session_token == "sess-1"
    req = route.calls.last.request
    assert req.headers["App-Token"] == APP_TOKEN
    assert req.headers["Authorization"] == f"user_token {USER_TOKEN}"
    await client.close()


async def test_init_session_without_app_token_omits_header(mock):
    # Localhost client mode: no App-Token configured -> header must be absent.
    route = await _init_route(mock)
    client = make_client(app_token="")
    await client.init_session()
    req = route.calls.last.request
    assert "App-Token" not in req.headers
    assert req.headers["Authorization"] == f"user_token {USER_TOKEN}"
    await client.close()


async def test_init_session_bad_tokens_raises_auth(mock):
    mock.get(f"{BASE}/initSession").mock(
        return_value=httpx.Response(401, json=["ERROR", "bad tokens"])
    )
    client = make_client()
    with pytest.raises(GlpiAuthError) as ei:
        await client.init_session()
    assert ei.value.raw == ["ERROR", "bad tokens"]
    await client.close()


# --- create ticket -----------------------------------------------------------
async def test_create_ticket_sends_input_and_returns_id(mock):
    await _init_route(mock)
    route = mock.post(f"{BASE}/Ticket").mock(
        return_value=httpx.Response(201, json={"id": 42, "message": "ok"})
    )
    client = make_client()
    tid = await client.create_ticket(
        name="Принтер не печатает",
        content="Ошибка на 2 этаже",
        urgency=4,
        itilcategories_id=7,
        requester_users_id=99,
    )
    assert tid == 42
    body = route.calls.last.request.read().decode()
    import json

    payload = json.loads(body)["input"]
    assert payload["name"] == "Принтер не печатает"
    assert payload["urgency"] == 4
    assert payload["itilcategories_id"] == 7
    assert payload["_users_id_requester"] == 99
    # authenticated call carries session + app token
    headers = route.calls.last.request.headers
    assert headers["Session-Token"] == "sess-1"
    assert headers["App-Token"] == APP_TOKEN
    await client.close()


async def test_authenticated_request_without_app_token_omits_header(mock):
    # No App-Token configured -> authenticated calls carry only Session-Token.
    await _init_route(mock)
    route = mock.post(f"{BASE}/Ticket").mock(return_value=httpx.Response(201, json={"id": 1}))
    client = make_client(app_token="")
    assert await client.create_ticket(name="t", content="c", urgency=3) == 1
    headers = route.calls.last.request.headers
    assert "App-Token" not in headers
    assert headers["Session-Token"] == "sess-1"
    await client.close()


async def test_create_ticket_accepts_list_response(mock):
    await _init_route(mock)
    mock.post(f"{BASE}/Ticket").mock(
        return_value=httpx.Response(201, json=[{"id": 5, "message": "ok"}])
    )
    client = make_client()
    assert await client.create_ticket(name="t", content="c", urgency=3) == 5
    await client.close()


async def test_create_ticket_error_surfaces_raw(mock):
    await _init_route(mock)
    mock.post(f"{BASE}/Ticket").mock(
        return_value=httpx.Response(400, json=["ERROR_BAD_INPUT", "nope"])
    )
    client = make_client()
    with pytest.raises(GlpiHTTPError) as ei:
        await client.create_ticket(name="t", content="c", urgency=3)
    assert ei.value.raw == ["ERROR_BAD_INPUT", "nope"]
    assert ei.value.response is not None
    await client.close()


# --- 401 re-init + retry -----------------------------------------------------
async def test_expired_session_reinits_and_retries_once(mock):
    # First initSession -> sess-1; the Ticket POST 401s; re-init -> sess-2; retry ok.
    init = mock.get(f"{BASE}/initSession").mock(
        side_effect=[
            httpx.Response(200, json={"session_token": "sess-1"}),
            httpx.Response(200, json={"session_token": "sess-2"}),
        ]
    )
    ticket = mock.post(f"{BASE}/Ticket").mock(
        side_effect=[
            httpx.Response(401, json=["ERROR_SESSION_TOKEN_INVALID", "expired"]),
            httpx.Response(201, json={"id": 7}),
        ]
    )
    client = make_client()
    tid = await client.create_ticket(name="t", content="c", urgency=3)
    assert tid == 7
    assert init.call_count == 2
    assert ticket.call_count == 2
    # retry used the renewed token
    assert ticket.calls[1].request.headers["Session-Token"] == "sess-2"
    await client.close()


async def test_persistent_401_raises_auth(mock):
    mock.get(f"{BASE}/initSession").mock(
        side_effect=[
            httpx.Response(200, json={"session_token": "sess-1"}),
            httpx.Response(200, json={"session_token": "sess-2"}),
        ]
    )
    mock.post(f"{BASE}/Ticket").mock(return_value=httpx.Response(401, json=["ERR", "x"]))
    client = make_client()
    with pytest.raises(GlpiAuthError):
        await client.create_ticket(name="t", content="c", urgency=3)
    await client.close()


# --- categories + pagination -------------------------------------------------
async def test_list_categories_paginates_on_206(mock):
    await _init_route(mock)
    page1 = [{"id": i, "name": f"c{i}", "completename": f"c{i}"} for i in range(200)]
    page2 = [{"id": 200, "name": "c200", "completename": "root > c200"}]
    mock.get(f"{BASE}/ITILCategory").mock(
        side_effect=[
            httpx.Response(206, json=page1, headers={"Content-Range": "0-199/201"}),
            httpx.Response(200, json=page2, headers={"Content-Range": "200-200/201"}),
        ]
    )
    client = make_client()
    cats = await client.list_categories()
    assert len(cats) == 201
    assert cats[-1].id == 200
    assert cats[-1].completename == "root > c200"
    await client.close()


async def test_list_categories_single_page(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/ITILCategory").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "a", "completename": "a"}])
    )
    client = make_client()
    cats = await client.list_categories()
    assert [c.id for c in cats] == [1]
    await client.close()


# --- retry policy ------------------------------------------------------------
async def test_idempotent_get_retries_on_5xx_then_succeeds(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/ITILCategory").mock(
        side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(200, json=[{"id": 1, "name": "a", "completename": "a"}]),
        ]
    )
    client = make_client()
    cats = await client.list_categories()
    assert [c.id for c in cats] == [1]
    await client.close()


async def test_post_not_retried_on_5xx(mock):
    await _init_route(mock)
    route = mock.post(f"{BASE}/Ticket").mock(return_value=httpx.Response(503, text="down"))
    client = make_client()
    with pytest.raises(GlpiHTTPError):
        await client.create_ticket(name="t", content="c", urgency=3)
    # Side-effecting POST must not be retried on a 5xx.
    assert route.call_count == 1
    await client.close()


async def test_post_retried_on_connect_error(mock):
    await _init_route(mock)
    route = mock.post(f"{BASE}/Ticket").mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.Response(201, json={"id": 3}),
        ]
    )
    client = make_client()
    # Connect error is pre-execution, so retrying the POST is safe.
    assert await client.create_ticket(name="t", content="c", urgency=3) == 3
    assert route.call_count == 2
    await client.close()


async def test_network_error_exhausts_and_raises(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/ITILCategory").mock(side_effect=httpx.ConnectError("refused"))
    client = make_client()
    with pytest.raises(GlpiNetworkError):
        await client.list_categories()
    await client.close()


# --- users / groups (feature 2) ---------------------------------------------
async def test_find_user_by_login_exact_active_match(mock):
    await _init_route(mock)
    # searchText is a substring match: GLPI returns several candidates.
    mock.get(f"{BASE}/User").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 5, "name": "jdoe2", "is_active": 1, "is_deleted": 0},
                {
                    "id": 7,
                    "name": "jdoe",
                    "is_active": 1,
                    "is_deleted": 0,
                    "firstname": "John",
                    "realname": "Doe",
                },
            ],
        )
    )
    client = make_client()
    user = await client.find_user_by_login("jdoe")
    assert user is not None
    assert user.id == 7
    assert user.display_name == "John Doe"
    await client.close()


async def test_find_user_by_login_skips_inactive(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/User").mock(
        return_value=httpx.Response(
            200, json=[{"id": 7, "name": "jdoe", "is_active": 0, "is_deleted": 0}]
        )
    )
    client = make_client()
    assert await client.find_user_by_login("jdoe") is None
    # ...but active_only=False resolves it (used by admin /unlink).
    assert (await client.find_user_by_login("jdoe", active_only=False)).id == 7
    await client.close()


async def test_find_user_by_login_no_match(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/User").mock(return_value=httpx.Response(200, json=[]))
    client = make_client()
    assert await client.find_user_by_login("ghost") is None
    await client.close()


async def test_get_user_returns_none_on_404(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/User/99").mock(return_value=httpx.Response(404, json=["ERROR", "x"]))
    client = make_client()
    assert await client.get_user(99) is None
    await client.close()


async def test_get_user_parses_active_flags(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/User/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "name": "jdoe", "is_active": 1})
    )
    client = make_client()
    user = await client.get_user(7)
    assert user.is_usable is True
    await client.close()


async def test_user_in_group_membership(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/User/7/Group_User").mock(
        return_value=httpx.Response(200, json=[{"groups_id": 3}, {"groups_id": 9}])
    )
    client = make_client()
    assert await client.user_in_group(7, 9) is True
    await client.close()


async def test_user_not_in_group(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/User/7/Group_User").mock(
        return_value=httpx.Response(200, json=[{"groups_id": 3}])
    )
    client = make_client()
    assert await client.user_in_group(7, 9) is False
    await client.close()


async def test_kill_session_clears_token(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/killSession").mock(return_value=httpx.Response(200, json={}))
    client = make_client()
    await client.init_session()
    await client.kill_session()
    assert client._session_token is None
    await client.close()
