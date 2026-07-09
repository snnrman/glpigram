"""GLPI legacy REST API (v1) client.

All GLPI HTTP access is isolated here so a future migration to API v2 touches
only this module. The client:

* opens a session with ``App-Token`` + ``user_token`` and caches the
  ``Session-Token``;
* transparently re-initialises the session on a ``401`` and retries the request
  once;
* retries idempotent requests (and pre-execution ``POST`` failures) on network
  errors / ``5xx`` with backoff;
* never leaks ``httpx`` exceptions — every failure surfaces as a ``GlpiError``
  subclass carrying the raw API response.

Usage IDs referenced below come from GLPI search options / constants; see the
``URGENCY_*`` and ``TICKET_STATUS_*`` constants.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
from typing import Any

import httpx

from .models import Document, Followup, ITILCategory, Ticket, TicketSummary, User

log = logging.getLogger(__name__)

# --- Domain constants (documented so callers don't hardcode magic numbers) ---
# GLPI urgency is 1..5; the /new dialog exposes three levels mapped onto these.
URGENCY_LOW = 2
URGENCY_MEDIUM = 3
URGENCY_HIGH = 4

# GLPI ticket statuses.
TICKET_STATUS_NEW = 1
TICKET_STATUS_PROCESSING_ASSIGNED = 2
TICKET_STATUS_PROCESSING_PLANNED = 3
TICKET_STATUS_WAITING = 4
TICKET_STATUS_SOLVED = 5
TICKET_STATUS_CLOSED = 6

# Not-yet-closed statuses: what /tickets lists and what a requester may close.
OPEN_TICKET_STATUSES = frozenset(
    {
        TICKET_STATUS_NEW,
        TICKET_STATUS_PROCESSING_ASSIGNED,
        TICKET_STATUS_PROCESSING_PLANNED,
        TICKET_STATUS_WAITING,
        TICKET_STATUS_SOLVED,
    }
)

# Ticket_User link types.
TICKET_USER_REQUESTER = 1
TICKET_USER_ASSIGN = 2  # technician / assignee

# GLPI searchOption id of the primary key ("id"). It is 2 for every itemtype
# (framework convention); verify with listSearchOptions/Ticket if a deployment
# ever disagrees. Used by /search/Ticket (feature 3), which sorts/criteria by
# searchOption id. NOTE: getAllItems (/Ticket) sorts by column NAME, not this.
SEARCHOPTION_ID = 2

# Ticket searchOption ids for /search/Ticket (feature 3). CLAUDE.md lists the
# common ones; verify via listSearchOptions/Ticket before trusting a new install.
SO_TICKET_NAME = 1
SO_TICKET_REQUESTER = 4
SO_TICKET_ASSIGN = 5
SO_TICKET_STATUS = 12

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5  # seconds; attempt n waits _BACKOFF_BASE * 2**(n-1)


class GlpiError(Exception):
    """Base class for all GLPI client errors.

    ``response`` holds the raw ``httpx.Response`` when the failure originated
    from an HTTP reply (``None`` for pure network failures).
    """

    def __init__(self, message: str, *, response: httpx.Response | None = None) -> None:
        super().__init__(message)
        self.response = response
        self.raw: Any = None
        if response is not None:
            try:
                self.raw = response.json()
            except ValueError:
                self.raw = response.text


class GlpiAuthError(GlpiError):
    """Authentication / session failure (bad tokens, session cannot be renewed)."""


class GlpiHTTPError(GlpiError):
    """Non-auth HTTP error returned by GLPI (4xx/5xx)."""


class GlpiNetworkError(GlpiError):
    """Transport-level failure (connection, timeout) after retries were exhausted."""


def _session_rejected(resp: httpx.Response) -> bool:
    """True when GLPI refused the request because of the session token.

    Observed live on GLPI 11.0.4: an expired/unknown token can come back as
    HTTP **400** ``ERROR_SESSION_TOKEN_MISSING``/``..._INVALID`` rather than the
    documented 401 — both must trigger the transparent renewal. Either way the
    request never executed, so retrying a POST is safe.
    """
    if resp.status_code == 401:
        return True
    return resp.status_code == 400 and "ERROR_SESSION_TOKEN" in resp.text


def _extract_id(resp: httpx.Response) -> int:
    """Pull the created object's id from a GLPI create response.

    GLPI returns a single ``{id, message}`` object or a one-element list of them.
    """
    data = resp.json()
    if isinstance(data, list):
        data = data[0] if data else None
    try:
        return int(data["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GlpiHTTPError("GLPI create returned no id", response=resp) from exc


def _parse_user_refs(raw: Any) -> tuple[list[int], str | None]:
    """Split a GLPI search user-column value into ids and any leftover names.

    Without ``expand_dropdowns`` a user column is a numeric id (or a list of
    them for multiple actors); some builds already return a name string. Return
    the numeric ids to resolve plus any non-numeric names to show verbatim.
    """
    if raw is None:
        return [], None
    items = raw if isinstance(raw, list) else [raw]
    ids: list[int] = []
    names: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text or text == "0":
            continue
        if text.isdigit():
            ids.append(int(text))
        else:
            names.append(text)
    return ids, (", ".join(names) if names else None)


class GlpiClient:
    def __init__(
        self,
        base_url: str,
        app_token: str,
        user_token: str,
        *,
        timeout: float = 20.0,
        proxy: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._app_token = app_token
        self._user_token = user_token
        self._session_token: str | None = None
        # trust_env=True lets httpx pick up HTTPS_PROXY automatically; an
        # explicit proxy argument (from config) takes precedence when given.
        self._http = httpx.AsyncClient(
            timeout=timeout,
            trust_env=True,
            proxy=proxy,
        )
        # Serialises session (re)initialisation so concurrent callers don't
        # open several sessions at once.
        self._session_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    # -- headers -----------------------------------------------------------
    def _auth_headers(
        self, *, json_content: bool = True, session_token: str | None = None
    ) -> dict[str, str]:
        """Auth headers. ``session_token`` pins a specific token to the request
        so a concurrent renewal can't yank it out from under a retry."""
        headers: dict[str, str] = {}
        # For multipart uploads httpx must set Content-Type (with the boundary),
        # so only force JSON for the normal case.
        if json_content:
            headers["Content-Type"] = "application/json"
        # App-Token is optional (the localhost client may not need one); only
        # send the header when configured.
        if self._app_token:
            headers["App-Token"] = self._app_token
        token = session_token or self._session_token
        if token:
            headers["Session-Token"] = token
        return headers

    # -- session -----------------------------------------------------------
    async def init_session(self) -> str:
        """Open (or renew) a GLPI session and cache the Session-Token.

        Concurrency-safe: if several coroutines race here only one HTTP call is
        made and the rest reuse the freshly acquired token.
        """
        async with self._session_lock:
            # A concurrent caller may have refreshed the token while we waited
            # on the lock, so reuse it.
            if self._session_token is not None:
                return self._session_token
            return await self._open_session_locked()

    async def _reauth(self, stale: str | None) -> str:
        """Renew the session after a 401 that was seen with token ``stale``.

        If a concurrent caller already replaced the token, reuse theirs instead
        of opening yet another GLPI session (and never null a fresh token).
        """
        async with self._session_lock:
            if self._session_token is not None and self._session_token != stale:
                return self._session_token
            self._session_token = None
            return await self._open_session_locked()

    async def _open_session_locked(self) -> str:
        """initSession HTTP call; caller must hold ``_session_lock``."""
        headers = {
            "Authorization": f"user_token {self._user_token}",
            "Content-Type": "application/json",
        }
        # App-Token is optional; include it only when configured.
        if self._app_token:
            headers["App-Token"] = self._app_token
        # initSession is safe to retry (idempotent, no side effects).
        resp = await self._send(
            "GET",
            "/initSession",
            headers=headers,
            idempotent=True,
        )
        if resp.status_code == 401:
            raise GlpiAuthError("GLPI rejected the service account tokens", response=resp)
        if resp.status_code >= 400:
            raise self._error_from_response(resp)
        try:
            token = resp.json()["session_token"]
        except (ValueError, KeyError) as exc:
            raise GlpiAuthError("initSession returned no session_token", response=resp) from exc
        self._session_token = token
        log.info("glpi_session_initialised")
        return token

    async def kill_session(self) -> None:
        """Best-effort session teardown; failures are logged, never raised."""
        if self._session_token is None:
            return
        try:
            await self._send("GET", "/killSession", headers=self._auth_headers(), idempotent=True)
        except GlpiError as exc:
            log.warning("glpi_kill_session_failed error=%s", exc)
        finally:
            self._session_token = None

    # -- low level transport ----------------------------------------------
    async def _send(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        idempotent: bool,
    ) -> httpx.Response:
        """Single HTTP call with retry on transient failures.

        Retry policy (CLAUDE.md): up to 3 attempts with exponential backoff on
        network errors and 5xx. Non-idempotent requests (POST that may have side
        effects) are retried ONLY when the failure is clearly pre-execution
        (connect error / connect timeout), never after the request may have
        reached GLPI.
        """
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await self._http.request(
                    method, url, headers=headers, json=json, params=params, data=data, files=files
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Pre-execution: the request never reached the server -> safe to
                # retry even for POST.
                last_exc = exc
                log.warning(
                    "glpi_connect_error method=%s path=%s attempt=%d", method, path, attempt
                )
            except httpx.HTTPError as exc:
                # Could have reached the server (read timeout, etc.). Only retry
                # when the call is idempotent.
                last_exc = exc
                if not idempotent:
                    raise GlpiNetworkError(f"network error on {method} {path}: {exc}") from exc
                log.warning(
                    "glpi_network_error method=%s path=%s attempt=%d", method, path, attempt
                )
            else:
                if resp.status_code >= 500 and (idempotent and attempt < _MAX_ATTEMPTS):
                    log.warning(
                        "glpi_5xx method=%s path=%s status=%d attempt=%d",
                        method,
                        path,
                        resp.status_code,
                        attempt,
                    )
                    last_exc = None
                else:
                    return resp

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))

        raise GlpiNetworkError(f"{method} {path} failed after {_MAX_ATTEMPTS} attempts: {last_exc}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        headers_extra: dict[str, str] | None = None,
        idempotent: bool,
    ) -> httpx.Response:
        """Authenticated request with transparent one-shot session renewal on 401.

        ``data``/``files`` send a multipart body (document uploads); in that case
        httpx sets the Content-Type, so the JSON header is dropped. File contents
        must be passed as ``bytes`` so the request can be re-sent on a 401 retry.
        """
        multipart = files is not None
        # Pin the token for this request: a concurrent renewal must not be able
        # to null it between acquiring and building the retry headers.
        token = self._session_token or await self.init_session()

        def _hdrs(tok: str) -> dict[str, str]:
            headers = self._auth_headers(json_content=not multipart, session_token=tok)
            if headers_extra:
                headers.update(headers_extra)
            return headers

        resp = await self._send(
            method,
            path,
            headers=_hdrs(token),
            json=json,
            params=params,
            data=data,
            files=files,
            idempotent=idempotent,
        )
        if _session_rejected(resp):
            # Session expired: renew once (or reuse a concurrent renewal) and retry.
            log.info("glpi_session_expired path=%s reinitialising", path)
            token = await self._reauth(stale=token)
            resp = await self._send(
                method,
                path,
                headers=_hdrs(token),
                json=json,
                params=params,
                data=data,
                files=files,
                idempotent=idempotent,
            )
            if _session_rejected(resp):
                raise GlpiAuthError("session still rejected after renewal", response=resp)

        if resp.status_code >= 400:
            raise self._error_from_response(resp)
        return resp

    @staticmethod
    def _error_from_response(resp: httpx.Response) -> GlpiError:
        cls = GlpiAuthError if resp.status_code in (401, 403) else GlpiHTTPError
        # Include the (truncated) GLPI body in the message so `str(exc)` — which
        # is what handlers/log lines print — carries the reason, not just a code.
        body = resp.text.strip().replace("\n", " ")
        if len(body) > 500:
            body = body[:500] + "…"
        method = resp.request.method if resp.request is not None else "?"
        detail = f": {body}" if body else ""
        msg = f"GLPI {method} {resp.url.path} -> HTTP {resp.status_code}{detail}"
        return cls(msg, response=resp)

    # -- endpoints ---------------------------------------------------------
    async def list_categories(self, *, page_size: int = 200) -> list[ITILCategory]:
        """Return all ITIL categories, following Range/206 pagination."""
        categories: list[ITILCategory] = []
        start = 0
        while True:
            end = start + page_size - 1
            resp = await self._request(
                "GET",
                "/ITILCategory",
                params={"range": f"{start}-{end}", "expand_dropdowns": "true"},
                idempotent=True,
            )
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            categories.extend(ITILCategory.from_api(item) for item in batch)
            # 206 => more rows remain; 200 => this was the last page.
            if resp.status_code != 206 or len(batch) < page_size:
                break
            start = end + 1
        return categories

    async def create_ticket(
        self,
        *,
        name: str,
        content: str,
        urgency: int,
        itilcategories_id: int | None = None,
        requester_users_id: int | None = None,
    ) -> int:
        """Create a ticket and return its GLPI id.

        ``requester_users_id`` sets the real employee as requester (feature 2);
        when omitted the ticket is owned by the service account.
        """
        payload: dict[str, Any] = {
            "name": name,
            "content": content,
            "urgency": urgency,
        }
        if itilcategories_id:
            payload["itilcategories_id"] = itilcategories_id
        if requester_users_id:
            payload["_users_id_requester"] = requester_users_id

        # POST has side effects: idempotent=False -> only pre-execution retries.
        resp = await self._request("POST", "/Ticket", json={"input": payload}, idempotent=False)
        data = resp.json()
        # GLPI may return a single object or a list of {id, message}.
        if isinstance(data, list):
            data = data[0]
        try:
            return int(data["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GlpiHTTPError("ticket created but no id returned", response=resp) from exc

    # -- users / groups (feature 2: account linking) -----------------------
    async def find_user_by_login(self, login: str, *, active_only: bool = True) -> User | None:
        """Find a GLPI user by AD login (``User.name``).

        Uses getAllItems with ``searchText`` (a substring match) and then filters
        to an exact, case-insensitive login match. With ``active_only`` (default)
        deactivated/deleted accounts are skipped; admin ``/unlink`` passes
        ``active_only=False`` so offboarded accounts can still be resolved.
        Returns ``None`` when no such user exists.
        """
        login = login.strip()
        if not login:
            return None
        resp = await self._request(
            "GET",
            "/User",
            params={"searchText[name]": login, "range": "0-99"},
            idempotent=True,
        )
        rows = resp.json()
        if not isinstance(rows, list):
            return None
        target = login.casefold()
        for raw in rows:
            if str(raw.get("name", "")).casefold() != target:
                continue  # substring match that isn't the exact login
            user = User.from_api(raw)
            if user.is_usable or not active_only:
                return user
        return None

    async def search_users_by_name(self, query: str, *, limit: int = 5) -> list[User]:
        """Find active users by partial, case-insensitive first/last-name match.

        Splits the query into tokens, narrows server-side with getAllItems
        ``searchText`` on the longest token (against both ``realname`` and
        ``firstname``), then keeps users whose combined "firstname realname"
        contains every token. Returns up to ``limit`` matches, name-sorted.
        """
        tokens = [t for t in query.split() if t]
        if not tokens:
            return []
        probe = max(tokens, key=len)  # most selective token for server-side narrowing
        pool: dict[int, User] = {}
        for field in ("realname", "firstname"):
            resp = await self._request(
                "GET",
                "/User",
                params={f"searchText[{field}]": probe, "range": "0-49"},
                idempotent=True,
            )
            rows = resp.json()
            if not isinstance(rows, list):
                continue
            for raw in rows:
                user = User.from_api(raw)
                if user.is_usable:
                    pool[user.id] = user
        needles = [t.casefold() for t in tokens]
        matches = [
            u
            for u in pool.values()
            if all(n in f"{u.firstname or ''} {u.realname or ''}".casefold() for n in needles)
        ]
        matches.sort(key=lambda u: u.display_name.casefold())
        return matches[:limit]

    async def get_user(self, user_id: int) -> User | None:
        """Fetch one user by id, or ``None`` if it no longer exists (404).

        Used by the auto-unlink re-check to see whether the account is still
        active (``User.is_usable``).
        """
        try:
            resp = await self._request("GET", f"/User/{user_id}", idempotent=True)
        except GlpiHTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise
        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        return User.from_api(data)

    async def user_in_group(self, user_id: int, group_id: int) -> bool:
        """True if the GLPI user belongs to the given group (Group_User link)."""
        resp = await self._request("GET", f"/User/{user_id}/Group_User", idempotent=True)
        links = resp.json()
        if not isinstance(links, list):
            return False
        return any(int(link.get("groups_id", 0) or 0) == group_id for link in links)

    # -- tickets / followups (feature 4: sync loop) ------------------------
    async def list_recent_tickets(self, *, limit: int = 100) -> list[Ticket]:
        """Return the most recent tickets, newest id first.

        getAllItems sorted by the ``id`` column descending; the sync loop filters
        to ``id > last_seen`` itself. A single page of ``limit`` covers far more
        than one 45 s interval ever produces.

        NOTE: getAllItems ``sort`` takes a **table column name** ("id"), not a
        searchOption id — GLPI 11 returns HTTP 400 ("sort param is not a field of
        glpi_tickets") for a numeric sort here. (Only /search/Ticket uses the
        numeric searchOption id for ``sort``.)
        """
        resp = await self._request(
            "GET",
            "/Ticket",
            params={
                "sort": "id",
                "order": "DESC",
                "range": f"0-{max(0, limit - 1)}",
            },
            idempotent=True,
        )
        rows = resp.json()
        if not isinstance(rows, list):
            return []
        return [Ticket.from_api(r) for r in rows]

    async def get_ticket(self, ticket_id: int) -> Ticket | None:
        """Fetch one ticket, or ``None`` if it no longer exists (404)."""
        try:
            resp = await self._request("GET", f"/Ticket/{ticket_id}", idempotent=True)
        except GlpiHTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise
        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        return Ticket.from_api(data)

    async def list_followups(self, ticket_id: int) -> list[Followup]:
        """Return all followups of a ticket (sub-item endpoint)."""
        resp = await self._request("GET", f"/Ticket/{ticket_id}/ITILFollowup", idempotent=True)
        rows = resp.json()
        if not isinstance(rows, list):
            return []
        return [Followup.from_api(r) for r in rows]

    # -- ticket mutations (feature 5: tech actions) ------------------------
    async def set_ticket_status(self, ticket_id: int, status: int) -> None:
        """Update a ticket's status (PUT /Ticket/{id})."""
        await self._request(
            "PUT",
            f"/Ticket/{ticket_id}",
            json={"input": {"id": ticket_id, "status": status}},
            idempotent=False,
        )

    async def assign_ticket(self, ticket_id: int, technician_users_id: int) -> None:
        """Assign a technician and move the ticket to *processing (assigned)*.

        Adds a ``Ticket_User`` assignee link, then flips the status. The two
        calls are separate GLPI mutations; if the status update fails the
        assignee is already recorded and the tech can retry.
        """
        await self._request(
            "POST",
            "/Ticket_User",
            json={
                "input": {
                    "tickets_id": ticket_id,
                    "users_id": technician_users_id,
                    "type": TICKET_USER_ASSIGN,
                }
            },
            idempotent=False,
        )
        await self.set_ticket_status(ticket_id, TICKET_STATUS_PROCESSING_ASSIGNED)

    async def add_followup(self, ticket_id: int, content: str, *, is_private: bool = False) -> int:
        """Add an ITILFollowup to a ticket; returns the new followup id."""
        resp = await self._request(
            "POST",
            "/ITILFollowup",
            json={
                "input": {
                    "itemtype": "Ticket",
                    "items_id": ticket_id,
                    "content": content,
                    "is_private": int(is_private),
                }
            },
            idempotent=False,
        )
        return _extract_id(resp)

    async def get_ticket_solution(self, ticket_id: int) -> tuple[int, str | None, str] | None:
        """The latest ITILSolution as ``(author_users_id, author_name, content)``.

        Used to include the actual solution text in the requester notification
        when a ticket is solved/closed from the GLPI web UI.
        """
        resp = await self._request("GET", f"/Ticket/{ticket_id}/ITILSolution", idempotent=True)
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            return None
        last = max(rows, key=lambda r: int(r.get("id", 0) or 0))
        content = str(last.get("content") or "").strip()
        if not content:
            return None
        name = None
        uid = int(last.get("users_id", 0) or 0)
        if uid:
            try:
                user = await self.get_user(uid)
                name = user.display_name if user else None
            except GlpiError:
                pass
        return uid, name, content

    async def add_solution(self, ticket_id: int, content: str) -> int:
        """Add an ITILSolution to a ticket (moves it to *solved*); returns its id."""
        resp = await self._request(
            "POST",
            "/ITILSolution",
            json={"input": {"itemtype": "Ticket", "items_id": ticket_id, "content": content}},
            idempotent=False,
        )
        return _extract_id(resp)

    # -- my tickets (feature 3) -------------------------------------------
    async def _user_name(self, user_id: int, cache: dict[int, str]) -> str:
        """Resolve a user id to a display name, memoised for this call."""
        if user_id in cache:
            return cache[user_id]
        try:
            user = await self.get_user(user_id)
        except GlpiError:
            user = None
        name = user.display_name if user else str(user_id)
        cache[user_id] = name
        return name

    async def search_user_open_tickets(
        self, requester_users_id: int, *, limit: int = 50
    ) -> list[TicketSummary]:
        """Not-yet-closed tickets where the user is requester, newest first.

        Uses /search/Ticket filtered by requester; only closed tickets are
        dropped client-side (status is numeric without expand_dropdowns), so the
        requester can still see and close *solved* tickets. Assignee ids are
        resolved to names.
        """
        params = {
            "criteria[0][field]": SO_TICKET_REQUESTER,
            "criteria[0][searchtype]": "equals",
            "criteria[0][value]": requester_users_id,
            "forcedisplay[0]": SEARCHOPTION_ID,
            "forcedisplay[1]": SO_TICKET_NAME,
            "forcedisplay[2]": SO_TICKET_STATUS,
            "forcedisplay[3]": SO_TICKET_ASSIGN,
            "sort": SEARCHOPTION_ID,
            "order": "DESC",
            "range": f"0-{max(0, limit - 1)}",
        }
        resp = await self._request("GET", "/search/Ticket", params=params, idempotent=True)
        payload = resp.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not rows:
            return []
        name_cache: dict[int, str] = {}
        summaries: list[TicketSummary] = []
        for row in rows:
            try:
                tid = int(row.get(str(SEARCHOPTION_ID)))
            except (TypeError, ValueError):
                continue
            status = int(row.get(str(SO_TICKET_STATUS)) or 0)
            if status == TICKET_STATUS_CLOSED:
                continue  # everything not-yet-closed (incl. solved) is listed
            ids, fallback = _parse_user_refs(row.get(str(SO_TICKET_ASSIGN)))
            parts = [await self._user_name(uid, name_cache) for uid in ids]
            if fallback:
                parts.append(fallback)
            summaries.append(
                TicketSummary(
                    id=tid,
                    title=str(row.get(str(SO_TICKET_NAME)) or ""),
                    status=status,
                    assignee=", ".join(parts) if parts else None,
                )
            )
        return summaries

    async def count_open_tickets_by_status(self) -> dict[int, int]:
        """Counts of not-yet-closed tickets grouped by status (for /stats).

        One paginated /search/Ticket query with GLPI's virtual status value
        ``notclosed`` (accepted by the status searchOption alongside plain
        ids); grouping happens client-side from the status column.
        """
        counts: dict[int, int] = {}
        start, page = 0, 200
        while True:
            params = {
                "criteria[0][field]": SO_TICKET_STATUS,
                "criteria[0][searchtype]": "equals",
                "criteria[0][value]": "notclosed",
                "forcedisplay[0]": SEARCHOPTION_ID,
                "forcedisplay[1]": SO_TICKET_STATUS,
                "range": f"{start}-{start + page - 1}",
            }
            resp = await self._request("GET", "/search/Ticket", params=params, idempotent=True)
            payload = resp.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not rows:
                return counts
            for row in rows:
                try:
                    status = int(row.get(str(SO_TICKET_STATUS)) or 0)
                except (TypeError, ValueError):
                    continue
                if status in OPEN_TICKET_STATUSES:
                    counts[status] = counts.get(status, 0) + 1
            start += len(rows)
            total = int(payload.get("totalcount") or 0)
            if start >= total:
                return counts

    # -- attachments (feature 6) ------------------------------------------
    async def upload_document(
        self, filename: str, content: bytes, *, mime: str = "application/octet-stream"
    ) -> int:
        """Upload a file as a GLPI Document (multipart); returns the document id.

        Follows the documented ``uploadManifest`` + file part convention. The
        content is passed as ``bytes`` so a 401 re-auth can re-send it.
        """
        manifest = jsonlib.dumps({"input": {"name": filename, "_filename": [filename]}})
        resp = await self._request(
            "POST",
            "/Document",
            data={"uploadManifest": manifest},
            files={"filename[0]": (filename, content, mime)},
            idempotent=False,
        )
        return _extract_id(resp)

    async def link_document(self, document_id: int, itemtype: str, items_id: int) -> None:
        """Link an uploaded Document to an item (Document_Item)."""
        await self._request(
            "POST",
            "/Document_Item",
            json={
                "input": {
                    "documents_id": document_id,
                    "itemtype": itemtype,
                    "items_id": items_id,
                }
            },
            idempotent=False,
        )

    async def attach_document_to_ticket(
        self, ticket_id: int, filename: str, content: bytes, *, mime: str | None = None
    ) -> int:
        """Upload a file and link it to the ticket; returns the document id."""
        doc_id = await self.upload_document(
            filename, content, mime=mime or "application/octet-stream"
        )
        await self.link_document(doc_id, "Ticket", ticket_id)
        return doc_id

    async def list_ticket_documents(self, ticket_id: int) -> list[Document]:
        """Documents attached to a ticket (Document_Item links + metadata)."""
        resp = await self._request("GET", f"/Ticket/{ticket_id}/Document_Item", idempotent=True)
        links = resp.json()
        if not isinstance(links, list):
            return []
        docs: list[Document] = []
        for link in links:
            doc_id = int(link.get("documents_id", 0) or 0)
            if not doc_id:
                continue
            try:
                meta = await self._request("GET", f"/Document/{doc_id}", idempotent=True)
                raw = meta.json()
            except GlpiHTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    continue  # document deleted meanwhile
                raise
            if isinstance(raw, dict):
                docs.append(Document.from_api(raw))
        return docs

    async def download_document(self, document_id: int) -> bytes:
        """Download a document's file content.

        The legacy API returns the raw file when the request carries
        ``Accept: application/octet-stream`` (JSON metadata otherwise).
        """
        resp = await self._request(
            "GET",
            f"/Document/{document_id}",
            headers_extra={"Accept": "application/octet-stream"},
            idempotent=True,
        )
        return resp.content

    async def get_ticket_assignees(self, ticket_id: int) -> list[str]:
        """Names of the technicians assigned to a ticket (Ticket_User type 2)."""
        resp = await self._request("GET", f"/Ticket/{ticket_id}/Ticket_User", idempotent=True)
        links = resp.json()
        if not isinstance(links, list):
            return []
        cache: dict[int, str] = {}
        names: list[str] = []
        for link in links:
            if int(link.get("type", 0) or 0) != TICKET_USER_ASSIGN:
                continue
            uid = int(link.get("users_id", 0) or 0)
            if uid:
                names.append(await self._user_name(uid, cache))
        return names

    async def get_ticket_requester(self, ticket_id: int) -> tuple[int, str] | None:
        """The ticket's requester as ``(glpi_users_id, display_name)``, or None.

        The requester is a ``Ticket_User`` link of type 1 (not a ticket column),
        so it needs its own lookup. Returns the first requester if several.
        """
        resp = await self._request("GET", f"/Ticket/{ticket_id}/Ticket_User", idempotent=True)
        links = resp.json()
        if not isinstance(links, list):
            return None
        cache: dict[int, str] = {}
        for link in links:
            if int(link.get("type", 0) or 0) != TICKET_USER_REQUESTER:
                continue
            uid = int(link.get("users_id", 0) or 0)
            if uid:
                return uid, await self._user_name(uid, cache)
        return None
