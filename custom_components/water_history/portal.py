"""Client for the water provider's customer portal (plain HTTP, no browser).

Sequence validated against the real portal with a test script:
1. GET a public endpoint to receive the session cookies (Sso-Id, App-Id, Api-Id,
   OGAWEB2227 = server affinity cookie, which must be sent back on every call);
2. POST /signin (JSON:API) -> 201, the JWT comes back in a header or a cookie;
3. GET /teleindex2/<contract>/<from>/<to> with the JWT and all the cookies.
Cookies are managed by hand (the shared HA session keeps none).
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
import json
import logging
from typing import Any
import urllib.parse

import aiohttp
from yarl import URL

from .const import CHUNK_DAYS

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://moncompte.regie-eau-mm.fr"
API_URL = f"{BASE_URL}/api/v1/iclients"
API_ID = "0e1c7a9be5c1f7746c5c0fc7783f46fa@iclients-19430-eurometropolemetz"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
LOGIN_ATTEMPTS = 3
RETRY_STATUSES = {412, 429, 500, 502, 503, 504}
AUTH_STATUSES = {400, 401, 403, 404, 422}


class PortalError(Exception):
    """Base error."""


class PortalConnectionError(PortalError):
    """Network problem or temporary refusal (worth retrying later)."""


class PortalAuthError(PortalError):
    """Credentials or token refused."""


class PortalDataError(PortalError):
    """Unexpected content (typically a wrong internal contract id)."""


def extract_jwt(raw: str | None) -> str:
    """Accept a raw token, 'Jwt id="..."', or that form URL-encoded (cookie)."""
    raw = urllib.parse.unquote(raw or "").strip().strip('"')
    if raw.startswith("Jwt id="):
        raw = raw[len("Jwt id="):].strip().strip('"')
    return raw


class PortalClient:
    RETRY_DELAY = 2.0  # seconds, multiplied by the attempt number

    def __init__(
        self, session: aiohttp.ClientSession, login: str, password: str, contract: str
    ) -> None:
        self._session = session
        self._login = login
        self._password = password
        self._contract = contract
        self._cookies: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _cookie_header(self, extra: dict[str, str] | None = None) -> str:
        return "; ".join(f"{k}={v}" for k, v in {**self._cookies, **(extra or {})}.items())

    async def _request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> tuple[int, Any, str]:
        try:
            async with self._session.request(
                method,
                URL(url, encoded=True),
                headers=headers,
                data=body,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                text = await response.text()
                for name, morsel in response.cookies.items():
                    self._cookies[name] = morsel.value
                return response.status, response.headers, text
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise PortalConnectionError(str(err) or type(err).__name__) from err

    async def _open_session(self) -> None:
        self._cookies = {}
        await self._request(
            "GET",
            f"{API_URL}/settings/translations/fr/client",
            {
                "Accept": "application/json",
                "Api-Id": API_ID,
                "User-Agent": USER_AGENT,
                "Referer": f"{BASE_URL}/",
            },
        )

    async def async_login(self) -> str:
        """Open a session and sign in; returns the JWT. Retries temporary refusals."""
        body = json.dumps(
            {
                "data": {
                    "type": "POICL_Signin",
                    "id": "",
                    "attributes": {
                        "login": self._login,
                        "password": self._password,
                        "remember": False,
                    },
                }
            }
        ).encode()
        last_status = 0
        for attempt in range(1, LOGIN_ATTEMPTS + 1):
            await self._open_session()
            status, headers, text = await self._request(
                "POST",
                f"{API_URL}/signin",
                {
                    "Content-Type": "application/vnd.api+json",
                    "Accept": "application/vnd.api+json",
                    "Api-Id": API_ID,
                    "User-Agent": USER_AGENT,
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/connexion",
                    "Cookie": self._cookie_header(),
                },
                body,
            )
            if 200 <= status < 300:
                token = extract_jwt(headers.get("Authorization"))
                if not token:
                    token = extract_jwt(self._cookies.get("Authorization"))
                if not token:
                    try:
                        token = extract_jwt(json.loads(text)["data"]["attributes"]["jwt"])
                    except (KeyError, ValueError, TypeError):
                        pass
                if not token:
                    raise PortalDataError("jeton absent de la réponse de connexion")
                return token
            if status in AUTH_STATUSES:
                raise PortalAuthError(f"connexion refusée (HTTP {status})")
            last_status = status
            if status not in RETRY_STATUSES:
                break
            _LOGGER.debug("Connexion : HTTP %s, nouvel essai (%s/%s)", status, attempt, LOGIN_ATTEMPTS)
            if attempt < LOGIN_ATTEMPTS:
                await asyncio.sleep(self.RETRY_DELAY * attempt)
        raise PortalConnectionError(f"connexion impossible (HTTP {last_status})")

    async def _fetch_chunk(self, token: str, start: date, end: date) -> list[Any]:
        url = (
            f"{API_URL}/teleindex2/{self._contract}/{start:%Y%m%d}/{end:%Y%m%d}"
            "?option[sort]=asc&option[algorithm]=chronological"
        )
        extra = {
            "I-Clients-Cookie": "true",
            "Authorization": urllib.parse.quote(f'Jwt id="{token}"', safe="="),
        }
        status, _, text = await self._request(
            "GET",
            url,
            {
                "Accept": "application/vnd.api+json",
                "Api-Id": API_ID,
                "Authorization": f'Jwt id="{token}"',
                "Referer": f"{BASE_URL}/telereleves",
                "User-Agent": USER_AGENT,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Cookie": self._cookie_header(extra),
            },
        )
        if status in (401, 403):
            raise PortalAuthError(f"lecture refusée (HTTP {status})")
        if status != 200:
            raise PortalConnectionError(f"lecture impossible (HTTP {status})")
        try:
            payload = json.loads(text)
        except ValueError as err:
            raise PortalDataError("réponse non JSON") from err
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise PortalDataError(
                "pas de liste de relevés (identifiant interne du contrat incorrect ?)"
            )
        return items

    async def async_fetch(self, start: date, end: date) -> list[Any]:
        """Readings between two dates (inclusive), requested in chunks."""
        async with self._lock:  # one session at a time (button + scheduled poll)
            token = await self.async_login()
            items: list[Any] = []
            chunk_start = start
            while chunk_start <= end:
                chunk_end = min(end, chunk_start + timedelta(days=CHUNK_DAYS - 1))
                items.extend(await self._fetch_chunk(token, chunk_start, chunk_end))
                chunk_start = chunk_end + timedelta(days=1)
            return items
