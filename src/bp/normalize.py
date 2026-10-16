from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tld import get_tld

# Tracking / session parameters that never change which article a URL points at.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "spm", "scm", "from", "fromuid", "fromitemid", "scene", "ref", "referer", "referrer",
    "share_token", "sharefrom", "sharer_sharetime", "sharer_shareid", "srcid",
    "mpshare", "chksm", "keyv", "traceid", "pass_ticket", "wxtoken", "devicetype",
    "exportkey", "action", "appmsg_credit", "fxlogout", "wx_header",
    "cid", "mid", "sn", "idx", "ch", "bc", "fmt", "ge", "feature", "h5_prid",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "igshid", "si",
}
TRACKING_PREFIXES = ("utm_", "hms_", "pk_", "at_", "_hs", "ys_", "share_")

DEFAULT_PORTS = {"http": 80, "https": 443}

# A WeChat article is reachable through many query-string variants of the same /s/ slug.
_WECHAT_SHORT = re.compile(r"^/s/(?P<slug>[A-Za-z0-9_-]+)$")

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad"), None)


def normalize_text(text: str | None) -> str:
    """Whitespace- and decoration-insensitive form used for hashing and for
    checking whether a model-quoted piece of evidence really came from the text."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_ZERO_WIDTH)
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def normalize_url(url: str | None) -> str | None:
    """Stable identity for an article location: scheme/host case, default ports,
    fragment, and tracking parameters all removed."""
    if not url:
        return None
    url = url.strip().strip("<>").replace(" ", "")
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = (parts.scheme or "https").lower()
    host = (parts.netloc or "").lower()
    if not host:
        return None
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if ":" in host:
        name, _, port = host.rpartition(":")
        if port.isdigit() and int(port) == DEFAULT_PORTS.get(scheme, 0):
            host = name
        elif not port.isdigit():
            return None
    if host.startswith("www."):
        host = host[4:]

    path = parts.path or "/"
    match = _WECHAT_SHORT.match(path)
    if match:
        # /s/<slug> is canonical; ?__biz=&mid=&idx= variants are the same article.
        return f"{scheme}://{host}/s/{match.group('slug')}"

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    ]
    kept.sort()
    query = urlencode(kept)

    if len(path) > 1:
        path = path.rstrip("/") or "/"

    return urlunsplit((scheme, host, path, query, ""))


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        netloc = (parts := urlsplit(url)).netloc or ""
    except ValueError:
        return None
    netloc = netloc.lower().rsplit("@", 1)[-1].split(":")[0]
    return netloc or None


def registrable_domain(url: str | None) -> str | None:
    """Registered domain suitable for official-source comparison:
    docs.cursor.com -> cursor.com, mp.weixin.qq.com -> qq.com."""
    if not url:
        return None
    try:
        found = get_tld(url, as_object=True, fail_silently=True)
    except Exception:
        return None
    return found.fld.lower() if found else None


def content_hash(title: str | None, body: str | None) -> str:
    """Content identity, deliberately title-insensitive: a repost under a different
    headline is still the same article, and the title is kept separately."""
    return hashlib.sha256(normalize_text(body).encode("utf-8")).hexdigest()


def title_hash(title: str | None, url: str | None) -> str:
    return hashlib.sha256(
        f"{normalize_text(title)}|{normalize_url(url) or ''}".encode()
    ).hexdigest()
