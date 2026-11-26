"""IDX fetch helper: plain request first, then a browser TLS fingerprint ladder.

Cloudflare fingerprints the caller: plain urllib/requests gets a challenge page (HTML,
HTTP 403 or 200), and which browser fingerprint passes rotates every few days.
Verified 24 Nov 2026: firefox133 serves the JSON endpoints, the zip/xlsx/pdf downloads
and the HTML pages. `kind` picks the body check:
  json -> starts with "{"      zip -> starts with "PK"      raw -> just not a challenge page

One copy of this file per repo (they deploy independently); keep them identical.
"""
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

IDX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/119.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.idx.co.id/",
}
# Verified 20 Nov on 7 IDX endpoints + 8x soak: firefox133 and tor145 pass reliably;
# the 19 Nov winners (safari17_0, safari17_2_ios) are 403 today but kept at the bottom so a
# rotation back costs one extra try instead of a failed run.
IDX_FINGERPRINTS = ["firefox133", "tor145", "safari17_0", "safari17_2_ios"]
IDX_ROUNDS = 2
IDX_BACKOFF_SECONDS = 5
# Cloudflare's interstitial, which arrives with either 403 or 200
_CHALLENGE_MARKERS = (
    b"Just a moment",
    b"cf-browser-verification",
    b"cf_chl_opt",
    b"Attention Required",
    b"Enable JavaScript and cookies to continue",
)

class IdxBlocked(RuntimeError):
    """Every route returned a challenge page or failed; nothing usable came back."""

def _looks_right(response, kind: str) -> bool:
    """A blocked request comes back as a challenge page, so the body shape is the real status."""
    head = response.content[:4000]
    if any(marker in head for marker in _CHALLENGE_MARKERS):
        return False
    if kind == "zip":
        return response.content[:2] == b"PK"
    if kind == "json":
        return response.text.lstrip().startswith("{")
    return bool(response.content)   # "raw": html/xlsx/pdf - just not empty, not a challenge

def _plain(url: str, params, proxies, timeout, kind: str) -> bytes:
    session = requests.Session()
    try:
        session.mount("https://", HTTPAdapter(max_retries=Retry(
            total=2, connect=2, read=2, backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"],
        )))
        response = session.get(url, params=params, headers=IDX_HEADERS, proxies=proxies,
                               verify=False, timeout=timeout)
        response.raise_for_status()
        if not _looks_right(response, kind):
            raise ValueError(f"non-{kind} body (HTTP {response.status_code}) - challenge page")
        return response.content
    finally:
        session.close()

def idx_get(url: str, kind: str = "json", proxies=None, params: dict | None = None,
            timeout=(10, 60), rounds: int = IDX_ROUNDS,
            backoff: int = IDX_BACKOFF_SECONDS) -> bytes:
    """GET an IDX url, plain requests first, then each browser TLS fingerprint.

    Proxy route first (it hides the runner IP), then direct: the proxy stalls often
    enough that a healthy direct connection is worth trying before giving up.
    `proxies` is whatever the caller already used (None = direct only).
    Raises IdxBlocked (a RuntimeError) listing what every attempt returned.
    """
    if kind not in ("json", "zip", "raw"):
        raise ValueError(f"unknown kind {kind!r}, expected json|zip|raw")
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        cffi_requests = None

    tried = []
    try:
        return _plain(url, params, proxies, timeout, kind)
    except Exception as error:
        tried.append(f"plain: {type(error).__name__}: {str(error)[:80]}")
        if cffi_requests is None:
            raise IdxBlocked(f"IDX fetch failed ({'; '.join(tried)})") from error

    routes = [proxies, None] if proxies else [None]
    # The impersonated client sets its own User-Agent; keep our headers from overriding it
    fingerprint_headers = {k: v for k, v in IDX_HEADERS.items() if k.lower() != "user-agent"}

    for round_no in range(1, rounds + 1):
        for fingerprint in IDX_FINGERPRINTS:
            for route in routes:
                where = "proxy" if route else "direct"
                session = cffi_requests.Session(impersonate=fingerprint, proxies=route, verify=False)
                try:
                    response = session.get(url, params=params, headers=fingerprint_headers,
                                           timeout=timeout)
                    response.raise_for_status()
                    if not _looks_right(response, kind):
                        raise ValueError(f"non-{kind} body (HTTP {response.status_code})")
                    return response.content
                except ValueError:
                    tried.append(f"{fingerprint}/{where}: challenge")
                except Exception as error:
                    tried.append(f"{fingerprint}/{where}: {type(error).__name__}")
                finally:
                    session.close()
        if round_no < rounds:
            time.sleep(backoff * round_no)

    raise IdxBlocked(f"IDX blocked every route ({'; '.join(tried)})")
