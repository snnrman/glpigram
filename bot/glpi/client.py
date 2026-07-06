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
import logging
from typing import Any

import httpx

from .models import ITILCategory, User

log = logging.getLogger(__name__)

# --- Domain constants (documented so callers don't hardcode magic numbers) ---
# GLPI urgency is 1..5; the /new dialog exposes three levels mapped onto these.
URGENCY_LOW = 2
URGENCY_MEDIUM = 3
URGENCY_HIGH = 4

# GLPI ticket statuses.
TICKET_STATUS_NEW = 1
TICKET_STATUS_PROCESSING_ASSIGNED = 2
TICKET_STATUS_SOLVED = 5
TICKET_STATUS_CLOSED = 6

# Ticket_User link types.
TICKET_USER_REQUESTER = 1

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
    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # App-Token is optional (the localhost client may not need one); only
        # send the header when configured.
        if self._app_token:
            headers["App-Token"] = self._app_token
        if self._session_token:
            headers["Session-Token"] = self._session_token
        return headers

    # -- session -----------------------------------------------------------
    async def init_session(self) -> str:
        """Open (or renew) a GLPI session and cache the Session-Token.

        Concurrency-safe: if several coroutines race here only one HTTP call is
        made and the rest reuse the freshly acquired token.
        """
        async with self._session_lock:
            # A concurrent caller may have refreshed the token while we waited on
            # the lock (renewers null the token before calling), so reuse it.
            if self._session_token is not None:
                return self._session_token
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
                    method, url, headers=headers, json=json, params=params
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
        idempotent: bool,
    ) -> httpx.Response:
        """Authenticated request with transparent one-shot session renewal on 401."""
        if self._session_token is None:
            await self.init_session()

        resp = await self._send(
            method,
            path,
            headers=self._auth_headers(),
            json=json,
            params=params,
            idempotent=idempotent,
        )
        if resp.status_code == 401:
            # Session expired: re-init once and retry the original request.
            log.info("glpi_session_expired path=%s reinitialising", path)
            self._session_token = None
            await self.init_session()
            resp = await self._send(
                method,
                path,
                headers=self._auth_headers(),
                json=json,
                params=params,
                idempotent=idempotent,
            )
            if resp.status_code == 401:
                raise GlpiAuthError("still 401 after session renewal", response=resp)

        if resp.status_code >= 400:
            raise self._error_from_response(resp)
        return resp

    @staticmethod
    def _error_from_response(resp: httpx.Response) -> GlpiError:
        cls = GlpiAuthError if resp.status_code in (401, 403) else GlpiHTTPError
        return cls(f"GLPI returned HTTP {resp.status_code}", response=resp)

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
