"""Optional X (Twitter) posting through the official API v2 (``POST /2/tweets``).

Safety model: nothing is ever posted automatically.  A draft is generated and
sent to Telegram; only an administrator pressing [📤 POST TO X] publishes it,
and only when ``X_ENABLED=true`` and all four OAuth 1.0a user-context
credentials are configured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

import httpx

from app.services.report_service import tweet_length
from app.utils.logging import get_logger

log = get_logger(__name__)

FORBIDDEN_PHRASES = ("confirmed scam", "100%", "scammer", "thief", "stole", "criminal")


class XError(Exception):
    pass


def _pct(s: str) -> str:
    return quote(s, safe="~-._")


def oauth1_header(method: str, url: str, *, consumer_key: str, consumer_secret: str, token: str, token_secret: str,
                  nonce: str | None = None, timestamp: str | None = None) -> str:  # fmt: skip
    """OAuth 1.0a HMAC-SHA1 Authorization header (JSON body is not part of the signature)."""
    params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    param_str = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
    base = "&".join([method.upper(), _pct(url), _pct(param_str)])
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"
    sig = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    params["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(params.items()))


def validate_post(text: str) -> None:
    low = text.lower()
    for p in FORBIDDEN_PHRASES:
        if p in low:
            raise XError(f"draft contains a prohibited over-claiming phrase: {p!r}")
    if tweet_length(text) > 280:
        raise XError("draft exceeds 280 characters")


class XService:
    def __init__(self, settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = settings
        self._transport = transport

    @property
    def enabled(self) -> bool:
        s = self.s
        return bool(s.x_enabled and s.x_api_key and s.x_api_secret and s.x_access_token and s.x_access_token_secret)

    async def post(self, text: str) -> str:
        if not self.enabled:
            raise XError("X integration is disabled (X_ENABLED=false or credentials missing)")
        validate_post(text)
        url = self.s.x_api_url.rstrip("/") + "/2/tweets"
        auth = oauth1_header(
            "POST", url,
            consumer_key=self.s.x_api_key, consumer_secret=self.s.x_api_secret,
            token=self.s.x_access_token, token_secret=self.s.x_access_token_secret,
        )  # fmt: skip
        async with httpx.AsyncClient(timeout=20, transport=self._transport) as client:
            resp = await client.post(url, json={"text": text}, headers={"Authorization": auth})
        if resp.status_code not in (200, 201):
            raise XError(f"X API returned HTTP {resp.status_code}: {resp.text[:200]}")
        tweet_id = str((resp.json().get("data") or {}).get("id") or "")
        log.info("X_POST_PUBLISHED", tweet_id=tweet_id)
        return tweet_id
