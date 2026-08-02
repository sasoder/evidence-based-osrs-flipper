"""Catalyst-research gather: OSRS news RSS plus configured subreddits, as one digest.

Each source degrades gracefully — on failure it returns a concrete `error` string the brief
must cite, never a vague "unavailable". Plain urllib with a hard timeout, no caching: this
runs ~once per session and freshness is the whole point.

Reddit is read from the public /.rss feed with a browser User-Agent (the descriptive wiki UA
and the /hot.rss path both 403; the plain /.rss path serves fine at our once-per-session
rate). `research.subreddit` may be one name or a list; results are merged. RSS omits
score/comments — titles are the catalyst signal, which is all this gather needs.

CLI:
    python -m flipper.research brief      # compact digest across all sources
    python -m flipper.research news
    python -m flipper.research reddit
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from .config import load_config

CONFIG = load_config()
UA = CONFIG["user_agent"]
TIMEOUT = 20
RESEARCH = CONFIG.get("research", {})
REDDIT_LIMIT = 12
# Reddit blocks the descriptive wiki UA and the /hot.rss path (403); the plain /.rss feed with a
# browser UA serves fine at our once-per-session rate. The Wiki (prices) still needs the
# descriptive UA, so this browser UA is reddit-only.
REDDIT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _fetch(url: str, ua: str = UA) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _clean(text: str, limit: int = 200) -> str:
    text = _WS_RE.sub(" ", _TAG_RE.sub("", text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _err(source: str, exc: Exception, **extra) -> dict:
    """Concrete, citable failure — never a silent drop. HTTPError carries the status code."""
    if isinstance(exc, urllib.error.HTTPError):
        detail = f"HTTP {exc.code}"
    elif isinstance(exc, urllib.error.URLError):
        detail = f"URLError: {exc.reason}"
    else:
        detail = f"{type(exc).__name__}: {exc}"
    return {"source": source, "ok": False, "error": detail, **extra}


NEWS_RSS = "https://secure.runescape.com/m=news/latest_news.rss?oldschool=1"


def news(limit: int = 10) -> dict:
    """Official OSRS news/patch-notes RSS — the highest-signal catalyst feed."""
    url = NEWS_RSS
    try:
        root = ET.fromstring(_fetch(url))
        items = []
        for it in root.iter("item"):
            items.append({
                "title": _clean(it.findtext("title") or "", 140),
                "date": (it.findtext("pubDate") or "").strip(),
                "url": (it.findtext("link") or "").strip(),
                "summary": _clean(it.findtext("description") or ""),
            })
            if len(items) >= limit:
                break
        return {"source": "osrs_news", "ok": True, "items": items}
    except Exception as exc:  # noqa: BLE001 - any failure must surface as a citable error
        return _err("osrs_news", exc, url=url)


def _reddit_via_rss(subs: list[str], limit: int) -> list[dict]:
    """Hot posts from one combined Reddit RSS feed, capped per configured subreddit."""
    url = f"https://www.reddit.com/r/{'+'.join(subs)}/.rss?limit=100"
    root = ET.fromstring(_fetch(url, REDDIT_UA))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    configured = {sub.casefold(): sub for sub in subs}
    counts = {sub: 0 for sub in subs}
    items = []
    for entry in root.findall("a:entry", ns):
        category = entry.find("a:category", ns)
        sub = configured.get((category.get("term") if category is not None else "").casefold())
        if not sub or counts[sub] >= limit:
            continue
        link = entry.find("a:link", ns)
        items.append({
            "title": _clean(entry.findtext("a:title", "", ns), 160),
            "published": (entry.findtext("a:published", "", ns) or "").strip(),
            "url": link.get("href") if link is not None else None,
            "subreddit": sub,
        })
        counts[sub] += 1
    return items


def reddit(limit: int | None = None) -> dict:
    """Hot posts across the configured subreddit(s), via the public RSS feeds.
    `research.subreddit` may be a single name or a list — items are merged and tagged with
    their `subreddit`. A failure surfaces as a citable error rather than a silent drop."""
    subs = RESEARCH.get("subreddit", "2007scape")
    subs = [subs] if isinstance(subs, str) else list(subs)
    limit = limit or REDDIT_LIMIT
    if not subs:
        return {"source": "reddit", "ok": True, "items": [], "subreddits": []}

    try:
        items = _reddit_via_rss(subs, limit)
    except Exception as exc:  # noqa: BLE001 - surface a concrete source failure
        return _err("reddit", exc, subreddits=subs)
    out = {"source": "reddit", "ok": True, "items": items}
    out["subreddit" if len(subs) == 1 else "subreddits"] = subs[0] if len(subs) == 1 else subs
    return out


def brief() -> dict:
    """One compact digest across every source, with a meta block listing which failed so the
    report can cite concrete blockers instead of claiming research was 'unavailable'."""
    sources = [news(), reddit()]
    failed = [
        source["source"] for source in sources
        if not source.get("ok") or source.get("partial_errors")
    ]
    return {
        "gathered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "failed_sources": failed,
        "all_ok": not failed,
    }


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="flipper.research")
    ap.add_argument("cmd", choices=["brief", "news", "reddit"], nargs="?", default="brief")
    args = ap.parse_args(argv)
    out = {"brief": brief, "news": news, "reddit": reddit}[args.cmd]()
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
