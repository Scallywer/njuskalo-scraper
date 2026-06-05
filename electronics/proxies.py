"""
Proxy pool for the electronics crawler.

Two modes, chosen by config/env:

1. **Paid endpoint** (preferred for reliability): set ``NJ_PROXY_ENDPOINT`` to a
   single rotating proxy URL, e.g. ``http://user:pass@gate.provider.com:7000``.
   The pool just returns that endpoint (the provider rotates the exit IP).

2. **Free self-validating pool** (zero cost, best-effort): fetch thousands of
   public proxies, validate them concurrently against njuskalo, and keep only
   the live ~5%. Re-validate periodically. Live pool is cached to
   ``data/live_proxies.txt`` (gitignored) so restarts are fast.

SECURITY: free proxies can intercept traffic. They are ONLY ever used for
public, secret-free GETs (the index crawl). Requests that carry a bearer token
(the phone API) must use ``allow_free=False`` -> direct or paid endpoint only.
"""

import os
import random
import asyncio
from curl_cffi.requests import AsyncSession, Session

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE = os.path.join(REPO_ROOT, "data", "live_proxies.txt")

PAID_ENDPOINT = os.environ.get("NJ_PROXY_ENDPOINT")  # set when you buy proxies

# Public free-proxy sources (host:port lines).
FREE_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]

VALIDATE_URL = "https://www.njuskalo.hr/mobiteli"
VALIDATE_MARKER = "entity-list-categories"
_HEADERS = {
    "accept": "text/html",
    "accept-language": "hr-HR,hr;q=0.9",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
}


def _as_proxy_dict(hostport_or_url: str) -> dict:
    url = hostport_or_url if "://" in hostport_or_url else f"http://{hostport_or_url}"
    return {"http": url, "https": url}


def fetch_free_candidates() -> list[str]:
    """Download and dedupe candidate host:port proxies from the public sources."""
    out, seen = [], set()
    s = Session()
    for src in FREE_SOURCES:
        try:
            r = s.get(src, timeout=20)
            if r.status_code != 200:
                continue
            for line in r.text.splitlines():
                line = line.strip()
                if line and ":" in line and not line.startswith("#") and line not in seen:
                    seen.add(line)
                    out.append(line)
        except Exception:
            continue
    return out


async def validate(candidates: list[str], limit: int = 40, concurrency: int = 80,
                   timeout: int = 8, max_tests: int = 800) -> list[str]:
    """Validate candidates against njuskalo; return host:port strings that work.

    Bounded by ``max_tests`` and a short per-proxy timeout so a run finishes in
    a couple of minutes even though most free proxies are dead/slow.
    """
    random.shuffle(candidates)
    candidates = candidates[:max_tests]
    sem = asyncio.Semaphore(concurrency)
    live: list[str] = []

    async def check(hp: str):
        if len(live) >= limit:
            return
        async with sem:
            if len(live) >= limit:
                return
            try:
                async with AsyncSession() as cs:
                    r = await asyncio.wait_for(
                        cs.get(VALIDATE_URL, headers=_HEADERS, impersonate="chrome110",
                               proxies=_as_proxy_dict(hp)),
                        timeout=timeout,
                    )
                    if r.status_code == 200 and VALIDATE_MARKER in r.text.lower():
                        live.append(hp)
            except Exception:
                pass

    await asyncio.gather(*(check(c) for c in candidates))
    return live


def _load_cache() -> list[str]:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return [l.strip() for l in f if l.strip()]
    return []


def _save_cache(live: list[str]) -> None:
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        f.write("\n".join(live) + ("\n" if live else ""))


class ProxyPool:
    """Rotating proxy source for the fetch layer.

    Usage:
        pool = await ProxyPool.create()          # paid endpoint or free pool
        proxy = pool.get(allow_free=True)        # dict for curl_cffi, or None=direct
        pool.report_bad(proxy)                   # drop a proxy that got blocked
    """

    def __init__(self, paid_endpoint=None, free_pool=None):
        self.paid_endpoint = paid_endpoint
        self.free_pool = list(free_pool or [])
        self._i = 0

    @classmethod
    async def create(cls, refresh_free: bool = False, limit: int = 60) -> "ProxyPool":
        if PAID_ENDPOINT:
            return cls(paid_endpoint=PAID_ENDPOINT)
        live = [] if refresh_free else _load_cache()
        if not live:
            live = await validate(fetch_free_candidates(), limit=limit)
            _save_cache(live)
        return cls(free_pool=live)

    def get(self, allow_free: bool = True):
        """Return a proxy dict for curl_cffi, or None to use the direct connection.

        allow_free=False forces direct/paid only (use for token-bearing requests).
        """
        if self.paid_endpoint:
            return _as_proxy_dict(self.paid_endpoint)
        if allow_free and self.free_pool:
            hp = self.free_pool[self._i % len(self.free_pool)]
            self._i += 1
            return _as_proxy_dict(hp)
        return None  # direct connection

    def report_bad(self, proxy_dict) -> None:
        if not proxy_dict or self.paid_endpoint:
            return
        url = proxy_dict.get("http", "")
        hp = url.split("://", 1)[-1]
        if hp in self.free_pool:
            self.free_pool.remove(hp)

    def __len__(self):
        return 1 if self.paid_endpoint else len(self.free_pool)


if __name__ == "__main__":
    # `python -m electronics.proxies` refreshes and reports the live free pool.
    async def _main():
        if PAID_ENDPOINT:
            print(f"Using paid endpoint: {PAID_ENDPOINT}")
            return
        print("Fetching free candidates...")
        cands = fetch_free_candidates()
        print(f"  {len(cands)} candidates. Validating against njuskalo (this takes a bit)...")
        live = await validate(cands, limit=60)
        _save_cache(live)
        print(f"  {len(live)} live proxies -> cached to {CACHE_FILE}")
        for hp in live[:15]:
            print("   ", hp)
    asyncio.run(_main())
