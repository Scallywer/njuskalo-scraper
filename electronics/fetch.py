"""
Fetch layer for the electronics crawler.

Wraps curl_cffi with the anti-ban behaviour the r/croatia thread spelled out:
  * Real browser TLS/header fingerprint (impersonate="chrome110").
  * Random per-request jitter -- njuskalo flags FIXED intervals instantly.
  * Ban detection (ShieldSquare captcha / 403 / 429) -> exponential backoff,
    rotate proxy, retry.
  * Proxy-pluggable via ProxyPool. Token-bearing requests pass allow_free=False
    so secrets never traverse an untrusted free proxy.

A single AsyncFetcher instance owns one AsyncSession (keeps cookies) and one
ProxyPool. Use it as an async context manager.
"""

import asyncio
import random
import re
from curl_cffi.requests import AsyncSession

from .proxies import ProxyPool

DEFAULT_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "hr-HR,hr;q=0.9,en;q=0.8",
    "cache-control": "no-cache",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
}

_BLOCK_TITLE = re.compile(r"<title>\s*ShieldSquare Captcha\s*</title>", re.I)


def is_blocked(status_code: int, text: str) -> bool:
    if status_code in (403, 429):
        return True
    if not text or len(text) < 2000:
        return True
    return bool(_BLOCK_TITLE.search(text))


class AsyncFetcher:
    def __init__(self, pool: ProxyPool, jitter=(2.5, 7.0), max_retries=4,
                 backoff_base=5.0, timeout=25, headers=None):
        self.pool = pool
        self.jitter = jitter
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.headers = headers or dict(DEFAULT_HEADERS)
        self._session: AsyncSession | None = None

    async def __aenter__(self):
        self._session = AsyncSession()
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.__aexit__(*exc)

    async def _sleep_jitter(self):
        await asyncio.sleep(random.uniform(*self.jitter))

    async def get(self, url: str, allow_free: bool = True, cookies: dict | None = None):
        """
        GET a URL with jitter + ban-aware retries. Returns response text, or
        None if every attempt was blocked/failed.

        allow_free=False keeps the request on the direct/paid path only (use for
        any request that carries a bearer token or other secret).
        """
        last_err = None
        for attempt in range(self.max_retries):
            await self._sleep_jitter()
            proxy = self.pool.get(allow_free=allow_free)
            try:
                kwargs = dict(headers=self.headers, impersonate="chrome110",
                              timeout=self.timeout)
                if proxy:
                    kwargs["proxies"] = proxy
                if cookies:
                    kwargs["cookies"] = cookies
                r = await asyncio.wait_for(self._session.get(url, **kwargs),
                                           timeout=self.timeout + 5)
                if is_blocked(r.status_code, r.text):
                    self.pool.report_bad(proxy)
                    await self._backoff(attempt)
                    continue
                return r.text
            except Exception as e:  # network / proxy / timeout
                last_err = e
                self.pool.report_bad(proxy)
                await self._backoff(attempt)
        if last_err:
            print(f"[fetch] gave up on {url}: {type(last_err).__name__}")
        else:
            print(f"[fetch] gave up on {url}: blocked after {self.max_retries} tries")
        return None

    async def _backoff(self, attempt: int):
        delay = self.backoff_base * (2 ** attempt) + random.uniform(0, 3)
        await asyncio.sleep(delay)


async def make_fetcher(allow_free_pool: bool = True, **kwargs) -> AsyncFetcher:
    """Convenience: build a ProxyPool (paid endpoint, cached free pool, or direct)
    and wrap it in an AsyncFetcher. If no proxies are available it runs direct."""
    pool = await ProxyPool.create() if allow_free_pool else ProxyPool()
    return AsyncFetcher(pool, **kwargs)
