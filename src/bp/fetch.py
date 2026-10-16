from __future__ import annotations

import re
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .config import get_settings
from .normalize import registrable_domain

# Signals that the content is behind a gate. Per the project rules we stop and
# mark the perk 待核实 rather than attempting to get past any of them.
GATE_MARKERS = {
    "login_required": [
        "please log in", "sign in to continue", "请登录", "先登录", "登录后查看",
        "authentication required", "enable javascript and cookies to login",
    ],
    "captcha": [
        "verify you are human", "checking your browser", "captcha", "人机验证",
        "安全验证", "access denied - automated query",
    ],
    "paywall": [
        "subscription required", "paywall", "subscribe to read", "付费阅读",
        "开通会员后可读", "购买后查看",
    ],
}

BLOCKED_HINTS = [
    "unusual traffic", "too many requests", "temporarily blocked",
    "are you a robot",
]


@dataclass
class Fetched:
    url: str
    final_url: str
    status: int
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    gate: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400 and bool(self.body)

    @property
    def registrable_domain(self) -> str | None:
        return registrable_domain(self.final_url or self.url)


class GateError(RuntimeError):
    pass


class Fetcher:
    """GET-only HTTP client.

    Deliberate limits, matching the project's safety rules: no POST bodies are ever
    sent, no credentials or cookies are injected into third-party sites, and a page
    that turns out to require a login, captcha or payment is reported as gated
    instead of being worked around.
    """

    def __init__(self, user_agent: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self.user_agent = user_agent or settings.user_agent
        self.timeout = timeout or settings.http_timeout_seconds
        self._client: httpx.Client | None = None
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    def __enter__(self) -> "Fetcher":
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            timeout=self.timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=5),
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._client:
            self._client.close()
        self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise RuntimeError("Fetcher must be used as a context manager")
        return self._client

    def allowed_by_robots(self, url: str) -> bool:
        """Honour robots.txt. An unreachable or unparsable robots file is treated
        as permissive, which is the widely accepted behaviour and keeps one misbehaving
        host from blocking the whole run."""
        # RFC 9306 scopes a robots file to one host+port, so this must not be collapsed
        # to the registrable domain: gemini.google.com and google.com publish different
        # rules and sharing one parser would silently forbid pages that are allowed.
        key = urlsplit(url)
        domain = key.netloc.lower()
        if not domain:
            return True
        parser = self._robots.get(domain)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            base = f"https://{domain}/robots.txt"
            lines: list[str] = []
            try:
                reply = self.client.get(base)
                if reply.status_code < 400:
                    lines = reply.text.splitlines()
            except Exception:
                lines = []
            parser.parse(lines)
            self._robots[domain] = parser
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def get(self, url: str, *, check_robots: bool = True) -> Fetched:
        if not url:
            return Fetched(url=url, final_url=url, status=0, error="empty url")
        if not re.match(r"^https?://", url, re.I):
            # urljoin with a scheme-only base yields "https:///host", which every
            # transport rejects, so the bare host is prefixed directly.
            url = "https://" + url.lstrip("/")
        if check_robots and not self.allowed_by_robots(url):
            return Fetched(url=url, final_url=url, status=0, error="disallowed by robots.txt")
        try:
            reply = self.client.get(url, headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        except httpx.HTTPError as exc:
            return Fetched(url=url, final_url=url, status=0, error=f"{type(exc).__name__}: {exc}"[:300])

        body = reply.text or ""
        gate = detect_gate(body, reply.status_code)
        return Fetched(
            url=url,
            final_url=str(reply.url),
            status=reply.status_code,
            body=body[:2_000_000],
            headers=dict(reply.headers),
            gate=gate,
        )


def detect_gate(body: str, status: int) -> str | None:
    text = body[:60_000].lower()
    if status in (401, 403) or status >= 500:
        if status in (401, 403):
            return "login_required"
        return "error"
    for kind, markers in GATE_MARKERS.items():
        if any(marker in text for marker in markers):
            return kind
    if any(hint in text for hint in BLOCKED_HINTS):
        return "captcha"
    return None
