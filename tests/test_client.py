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
    # the message (what logs print via %s) must carry the GLPI body, not just the code
    msg = str(ei.value)
    assert "400" in msg
    assert "ERROR_BAD_INPUT" in msg and "nope" in msg
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


async def test_search_users_by_name_matches_all_tokens_active_only(mock):
    await _init_route(mock)
    # Server-side searchText narrows on the longest token; both fields are probed.
    # Pool includes a namesake (different surname) and a deactivated user.
    pool = [
        {"id": 42, "name": "oleg.k", "firstname": "Олег", "realname": "Каленский", "is_active": 1},
        {"id": 41, "name": "oleg.m", "firstname": "Олег", "realname": "Максимов", "is_active": 1},
        {"id": 40, "name": "old", "firstname": "Олег", "realname": "Каленский", "is_active": 0},
    ]
    mock.get(f"{BASE}/User").mock(return_value=httpx.Response(200, json=pool))
    client = make_client()
    res = await client.search_users_by_name("Олег Каленский")
    # only the active user whose combined name contains BOTH tokens
    assert [u.id for u in res] == [42]
    await client.close()


async def test_search_users_by_name_multiple_and_limit(mock):
    await _init_route(mock)
    pool = [
        {"id": i, "name": f"u{i}", "firstname": "Олег", "realname": f"Ф{i}", "is_active": 1}
        for i in range(8)
    ]
    mock.get(f"{BASE}/User").mock(return_value=httpx.Response(200, json=pool))
    client = make_client()
    res = await client.search_users_by_name("Олег", limit=5)
    assert len(res) == 5  # capped at limit
    await client.close()


async def test_search_users_by_name_blank_query(mock):
    await _init_route(mock)
    client = make_client()
    assert await client.search_users_by_name("   ") == []
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


# --- tickets / followups (feature 4) ----------------------------------------
async def test_list_recent_tickets_sorts_desc_and_parses(mock):
    await _init_route(mock)
    route = mock.get(f"{BASE}/Ticket").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 9, "name": "b", "status": 2},
                {"id": 8, "name": "a", "status": 1},
            ],
        )
    )
    client = make_client()
    tickets = await client.list_recent_tickets(limit=50)
    assert [t.id for t in tickets] == [9, 8]
    # newest-first sort is requested from GLPI. getAllItems sorts by COLUMN name
    # ("id"), not a searchOption id — a numeric sort here is HTTP 400 in GLPI 11.
    params = route.calls.last.request.url.params
    assert params["order"] == "DESC"
    assert params["sort"] == "id"
    assert params["range"] == "0-49"
    await client.close()


async def test_get_ticket_none_on_404(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/Ticket/77").mock(return_value=httpx.Response(404, json=["ERR", "x"]))
    client = make_client()
    assert await client.get_ticket(77) is None
    await client.close()


async def test_list_followups_parses_flags(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/Ticket/10/ITILFollowup").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "items_id": 10, "content": "hi", "users_id": 5, "is_private": 0},
                {"id": 2, "items_id": 10, "content": "psst", "users_id": 6, "is_private": 1},
            ],
        )
    )
    client = make_client()
    fups = await client.list_followups(10)
    assert [f.id for f in fups] == [1, 2]
    assert fups[0].is_private is False
    assert fups[1].is_private is True
    await client.close()


# --- ticket mutations (feature 5) -------------------------------------------
async def test_set_ticket_status_puts_status(mock):
    import json

    await _init_route(mock)
    route = mock.put(f"{BASE}/Ticket/5").mock(return_value=httpx.Response(200, json={"id": 5}))
    client = make_client()
    await client.set_ticket_status(5, 2)
    payload = json.loads(route.calls.last.request.read().decode())["input"]
    assert payload["status"] == 2
    await client.close()


async def test_assign_ticket_links_then_sets_status(mock):
    import json

    await _init_route(mock)
    link = mock.post(f"{BASE}/Ticket_User").mock(return_value=httpx.Response(201, json={"id": 1}))
    status = mock.put(f"{BASE}/Ticket/5").mock(return_value=httpx.Response(200, json={"id": 5}))
    client = make_client()
    await client.assign_ticket(5, technician_users_id=42)
    link_input = json.loads(link.calls.last.request.read().decode())["input"]
    assert link_input == {"tickets_id": 5, "users_id": 42, "type": 2}  # type 2 = assignee
    status_input = json.loads(status.calls.last.request.read().decode())["input"]
    assert status_input["status"] == 2  # processing (assigned)
    await client.close()


async def test_add_followup_sends_input_and_returns_id(mock):
    import json

    await _init_route(mock)
    route = mock.post(f"{BASE}/ITILFollowup").mock(
        return_value=httpx.Response(201, json={"id": 88})
    )
    client = make_client()
    fid = await client.add_followup(10, "hello", is_private=False)
    assert fid == 88
    payload = json.loads(route.calls.last.request.read().decode())["input"]
    assert payload == {"itemtype": "Ticket", "items_id": 10, "content": "hello", "is_private": 0}
    await client.close()


async def test_add_solution_sends_input_and_returns_id(mock):
    import json

    await _init_route(mock)
    route = mock.post(f"{BASE}/ITILSolution").mock(
        return_value=httpx.Response(201, json=[{"id": 3, "message": "ok"}])
    )
    client = make_client()
    sid = await client.add_solution(10, "fixed it")
    assert sid == 3  # tolerates list-wrapped create response
    payload = json.loads(route.calls.last.request.read().decode())["input"]
    assert payload == {"itemtype": "Ticket", "items_id": 10, "content": "fixed it"}
    await client.close()


# --- my tickets (feature 3) -------------------------------------------------
async def test_search_user_open_tickets_filters_closed_and_resolves_assignee(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/search/Ticket").mock(
        return_value=httpx.Response(
            200,
            json={
                "totalcount": 4,
                "data": [
                    {"2": 9, "1": "Открытая", "12": 2, "5": "42"},  # open, assignee id 42
                    {"2": 8, "1": "Закрытая", "12": 6, "5": ""},  # closed -> dropped
                    {"2": 7, "1": "Без исполнителя", "12": 1, "5": None},  # open, no assignee
                    {"2": 6, "1": "Решённая", "12": 5, "5": ""},  # solved -> KEPT (closable)
                ],
            },
        )
    )
    mock.get(f"{BASE}/User/42").mock(
        return_value=httpx.Response(
            200, json={"id": 42, "name": "tech", "firstname": "Иван", "realname": "Петров"}
        )
    )
    client = make_client()
    tickets = await client.search_user_open_tickets(99)
    assert [t.id for t in tickets] == [9, 7, 6]  # only the closed one is filtered out
    assert tickets[0].status == 2
    assert tickets[0].assignee == "Иван Петров"  # id resolved to a name
    assert tickets[1].assignee is None
    assert tickets[2].status == 5  # solved stays so the requester can close it
    await client.close()


async def test_search_user_open_tickets_empty(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/search/Ticket").mock(return_value=httpx.Response(200, json={"totalcount": 0}))
    client = make_client()
    assert await client.search_user_open_tickets(99) == []
    await client.close()


async def test_get_ticket_assignees_only_type_2(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/Ticket/10/Ticket_User").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"users_id": 7, "type": 1},  # requester -> ignored
                {"users_id": 42, "type": 2},  # technician
            ],
        )
    )
    mock.get(f"{BASE}/User/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "name": "tech"})
    )
    client = make_client()
    assert await client.get_ticket_assignees(10) == ["tech"]
    await client.close()


async def test_get_ticket_requester_type_1(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/Ticket/23/Ticket_User").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"users_id": 99, "type": 2},  # assignee -> ignored
                {"users_id": 42, "type": 1},  # requester
            ],
        )
    )
    mock.get(f"{BASE}/User/42").mock(
        return_value=httpx.Response(
            200, json={"id": 42, "name": "jdoe", "firstname": "Иван", "realname": "Петров"}
        )
    )
    client = make_client()
    assert await client.get_ticket_requester(23) == (42, "Иван Петров")
    await client.close()


async def test_get_ticket_requester_none_when_absent(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/Ticket/23/Ticket_User").mock(
        return_value=httpx.Response(200, json=[{"users_id": 99, "type": 2}])
    )
    client = make_client()
    assert await client.get_ticket_requester(23) is None
    await client.close()


# --- attachments (feature 6) ------------------------------------------------
async def test_upload_document_sends_multipart_and_returns_id(mock):
    await _init_route(mock)
    route = mock.post(f"{BASE}/Document").mock(return_value=httpx.Response(201, json={"id": 55}))
    client = make_client()
    doc_id = await client.upload_document("photo.jpg", b"\xff\xd8\xff data", mime="image/jpeg")
    assert doc_id == 55
    req = route.calls.last.request
    # multipart body: httpx sets the boundary Content-Type, not application/json
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.read()
    assert b'name="uploadManifest"' in body
    assert b'"_filename"' in body
    assert b'name="filename[0]"' in body
    assert b"\xff\xd8\xff data" in body
    await client.close()


async def test_link_document_posts_document_item(mock):
    import json

    await _init_route(mock)
    route = mock.post(f"{BASE}/Document_Item").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    client = make_client()
    await client.link_document(55, "Ticket", 10)
    payload = json.loads(route.calls.last.request.read().decode())["input"]
    assert payload == {"documents_id": 55, "itemtype": "Ticket", "items_id": 10}
    await client.close()


async def test_attach_document_to_ticket_uploads_then_links(mock):
    await _init_route(mock)
    upload = mock.post(f"{BASE}/Document").mock(return_value=httpx.Response(201, json={"id": 55}))
    linkr = mock.post(f"{BASE}/Document_Item").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    client = make_client()
    doc_id = await client.attach_document_to_ticket(
        10, "f.pdf", b"pdfbytes", mime="application/pdf"
    )
    assert doc_id == 55
    assert upload.called and linkr.called
    await client.close()


async def test_kill_session_clears_token(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/killSession").mock(return_value=httpx.Response(200, json={}))
    client = make_client()
    await client.init_session()
    await client.kill_session()
    assert client._session_token is None
    await client.close()


# --- session renewal race (reviewer finding 1.1) ------------------------------
async def test_reauth_reuses_concurrent_renewal(mock):
    # A 401 retry must reuse a token renewed by a concurrent coroutine instead
    # of opening yet another session (or worse, nulling the fresh token).
    route = await _init_route(mock)
    client = make_client()
    await client.init_session()  # sess-1
    client._session_token = "sess-2"  # a concurrent caller already renewed
    assert await client._reauth(stale="sess-1") == "sess-2"
    assert route.call_count == 1  # no extra initSession
    await client.close()


async def test_reauth_renews_when_own_token_is_stale(mock):
    mock.get(f"{BASE}/initSession").mock(
        side_effect=[
            httpx.Response(200, json={"session_token": "sess-1"}),
            httpx.Response(200, json={"session_token": "sess-2"}),
        ]
    )
    client = make_client()
    await client.init_session()
    assert await client._reauth(stale="sess-1") == "sess-2"
    await client.close()


async def test_session_rejected_as_http_400_also_renews(mock):
    # Live GLPI 11.0.4 returns 400 ERROR_SESSION_TOKEN_MISSING (not 401) for a
    # dead token; the transparent renewal must cover that shape too.
    init = mock.get(f"{BASE}/initSession").mock(
        side_effect=[
            httpx.Response(200, json={"session_token": "sess-1"}),
            httpx.Response(200, json={"session_token": "sess-2"}),
        ]
    )
    ticket = mock.get(f"{BASE}/Ticket/5").mock(
        side_effect=[
            httpx.Response(400, json=["ERROR_SESSION_TOKEN_MISSING", "нет токена"]),
            httpx.Response(200, json={"id": 5, "name": "t", "status": 1}),
        ]
    )
    client = make_client()
    t = await client.get_ticket(5)
    assert t is not None and t.id == 5
    assert init.call_count == 2
    assert ticket.calls[1].request.headers["Session-Token"] == "sess-2"
    await client.close()


async def test_plain_400_is_not_mistaken_for_session_error(mock):
    await _init_route(mock)
    route = mock.post(f"{BASE}/Ticket").mock(
        return_value=httpx.Response(400, json=["ERROR_BAD_INPUT", "nope"])
    )
    client = make_client()
    with pytest.raises(GlpiHTTPError):
        await client.create_ticket(name="t", content="c", urgency=3)
    assert route.call_count == 1  # no bogus renewal retry
    await client.close()


# --- ticket documents (attachments on the tech card) --------------------------
async def test_list_ticket_documents_resolves_metadata(mock):
    await _init_route(mock)
    mock.get(f"{BASE}/Ticket/7/Document_Item").mock(
        return_value=httpx.Response(
            200, json=[{"documents_id": 55}, {"documents_id": 56}, {"documents_id": 0}]
        )
    )
    mock.get(f"{BASE}/Document/55").mock(
        return_value=httpx.Response(
            200, json={"id": 55, "filename": "a.jpg", "mime": "image/jpeg", "filesize": 1234}
        )
    )
    mock.get(f"{BASE}/Document/56").mock(
        return_value=httpx.Response(404, json=["ERROR", "gone"])  # deleted meanwhile
    )
    client = make_client()
    docs = await client.list_ticket_documents(7)
    assert len(docs) == 1
    assert (docs[0].id, docs[0].filename, docs[0].is_image, docs[0].filesize) == (
        55,
        "a.jpg",
        True,
        1234,
    )
    await client.close()


async def test_download_document_uses_octet_stream_accept(mock):
    await _init_route(mock)
    route = mock.get(f"{BASE}/Document/55").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8binary")
    )
    client = make_client()
    data = await client.download_document(55)
    assert data == b"\xff\xd8binary"
    # the raw file comes back only with this Accept header (JSON meta otherwise)
    assert route.calls.last.request.headers["Accept"] == "application/octet-stream"
    await client.close()


async def test_search_tech_open_tickets_filters_by_assignee_field(mock):
    await _init_route(mock)
    route = mock.get(f"{BASE}/search/Ticket").mock(
        return_value=httpx.Response(
            200,
            json={
                "totalcount": 2,
                "data": [
                    {"2": 9, "1": "Моя", "12": 2, "5": "9"},
                    {"2": 8, "1": "Закрытая", "12": 6, "5": "9"},  # closed -> dropped
                ],
            },
        )
    )
    mock.get(f"{BASE}/User/9").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "tech", "realname": "Петров"})
    )
    client = make_client()
    result = await client.search_tech_open_tickets(9)
    await client.close()
    assert [t.id for t in result] == [9]
    params = dict(route.calls[0].request.url.params)
    assert params["criteria[0][field]"] == "5"  # assignee searchOption (Ticket_User type=2)
    assert params["criteria[0][value]"] == "9"
