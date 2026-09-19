import asyncio
import logging
from typing import Any, Optional

import aiohttp
from lxml.etree import XMLSyntaxError

from .parser import parse_xml
from .ratelimiter import RateLimiter

logger = logging.getLogger(__name__)

NS_API_URL = "https://www.nationstates.net/cgi-bin/api.cgi"


class NSApiClient:
    """Central gateway for all NationStates API calls: rate-limits, queues, and parses every request."""

    def __init__(self, user_agent: str):
        """
        :param user_agent: contact string sent as the User-Agent header, per NS API policy
        """
        self.user_agent = user_agent
        self._session: Optional[aiohttp.ClientSession] = None
        self._limiter = RateLimiter(max_requests=48, per_seconds=30.0)

    async def start(self) -> None:
        """Open the underlying HTTP session."""
        self._session = aiohttp.ClientSession(headers={"User-Agent": self.user_agent})

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None:
            await self._session.close()

    async def request(
        self,
        params: dict[str, Any],
        *,
        method: str = "GET",
        data: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Issue a rate-limited request against the NS API and return the parsed JSON body.
        :param params: query string parameters, e.g. {"nation": "testlandia", "q": "population"}
        :param method: HTTP method to use, "GET" or "POST" (POST is required for authenticated commands)
        :param data: form-encoded body fields for a POST request, e.g. issue/option command fields
        :param headers: extra per-request headers, e.g. X-Password/X-Autologin/X-Pin for private shards
        :return: parsed response body
        """
        if self._session is None:
            await self.start()

        while True:
            await self._limiter.acquire()
            async with self._session.request(
                method, NS_API_URL, params=params, data=data, headers=headers
            ) as resp:
                if resp.status == 429:
                    try:
                        retry_after = float(
                            resp.headers.get("Retry-After", self._limiter.per_seconds)
                        )
                    except ValueError:
                        retry_after = self._limiter.per_seconds
                    logger.warning(
                        "NS API rate limit hit, retrying in %.1fs", retry_after
                    )
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                body = await resp.read()
                try:
                    return parse_xml(body)
                except XMLSyntaxError:
                    return {"_raw": body.decode("utf-8", "replace").strip()}
