"""Deterministic catalyst-research gather.

The edge in this account isn't the price bands — those are deterministic and self-grading.
It's catching *why* a price is about to move: an update, a meta shift, a community reaction.
That signal lives on external sources (official OSRS news/blogs, r/2007scape), and historically
the brief delegated it to subagents with web tools — which silently failed when the toolset was
too narrow, leaving the report to shrug "web checks unavailable".

This module makes the gather deterministic, like every other input: fetch compact summaries
over plain HTTP and return them. Each source degrades gracefully — on failure it returns a
concrete `error` string the brief must cite, never a vague "unavailable". Subagents should be
used to *interpret* this digest into positioning, not to fetch it.

Network use mirrors merch.prices: urllib with the configured descriptive User-Agent (generic
UAs like python-urllib get blocked/ratelimited by both the Wiki and Reddit), and a hard
timeout. No caching — this runs ~once per session and freshness is the whole point.

Reddit: read via the public **/.rss** feed with a browser User-Agent. The descriptive wiki UA
and the /hot.rss path both 403; the plain /.rss path + browser UA serves fine at our
once-per-session rate. `research.subreddit` may be one name or a list; results are merged.
RSS omits score/comments — titles are the catalyst signal, which is all this gather needs.

CLI:
    python -m merch.research brief      # compact digest across all sources
    python -m merch.research news
    python -m merch.research reddit
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
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
REDDIT_REQUEST_DELAY = 6.0   # seconds between subreddit fetches — polite spacing avoids 429
REDDIT_RETRY_BACKOFF = 12.0  # extra wait before a single retry when a sub still 429s

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


DEFAULT_NEWS_RSS = "https://secure.runescape.com/m=news/latest_news.rss?oldschool=1"


def news(limit: int = 10) -> dict:
    """Official OSRS news/patch-notes RSS — the highest-signal catalyst feed."""
    url = RESEARCH.get("news_rss", DEFAULT_NEWS_RSS)
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


def _reddit_via_rss(sub: str, limit: int) -> list[dict]:
    """r/<sub> hot posts from the public RSS/Atom feed. No score/comments (RSS omits them) —
    titles are the catalyst signal. Uses the plain /.rss path + a browser UA (the /hot.rss path
    and the descriptive wiki UA both 403)."""
    url = f"https://www.reddit.com/r/{sub}/.rss"
    root = ET.fromstring(_fetch(url, REDDIT_UA))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = []
    for entry in root.findall("a:entry", ns):
        link = entry.find("a:link", ns)
        items.append({
            "title": _clean(entry.findtext("a:title", "", ns), 160),
            "published": (entry.findtext("a:published", "", ns) or "").strip(),
            "url": link.get("href") if link is not None else None,
        })
        if len(items) >= limit:
            break
    return items


def reddit(limit: int | None = None) -> dict:
    """Hot posts across the configured subreddit(s), via the public RSS feeds.
    `research.subreddit` may be a single name or a list — items are merged and tagged with
    their `subreddit`. A failure surfaces as a citable error rather than a silent drop."""
    subs = RESEARCH.get("subreddit", "2007scape")
    subs = [subs] if isinstance(subs, str) else list(subs)
    limit = limit or REDDIT_LIMIT

    items, errors = [], []
    for i, sub in enumerate(subs):
        if i:
            time.sleep(REDDIT_REQUEST_DELAY)  # space requests so we don't trip Reddit's rate limit
        for attempt in range(2):
            try:
                sub_items = _reddit_via_rss(sub, limit)
                for it in sub_items:
                    it["subreddit"] = sub
                items.extend(sub_items)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt == 0:  # one backoff retry on rate-limit
                    time.sleep(REDDIT_RETRY_BACKOFF)
                    continue
                errors.append(f"r/{sub}: {_err('reddit', exc)['error']}")
                break
            except Exception as exc:  # noqa: BLE001 - record per-sub failure, keep going
                errors.append(f"r/{sub}: {_err('reddit', exc)['error']}")
                break

    if not items and errors:
        return {"source": "reddit", "ok": False,
                "subreddits": subs, "error": "; ".join(errors)}
    out = {"source": "reddit", "ok": True, "items": items}
    if errors:
        out["partial_errors"] = errors
    out["subreddit" if len(subs) == 1 else "subreddits"] = subs[0] if len(subs) == 1 else subs
    return out


def brief() -> dict:
    """One compact digest across every source, with a meta block listing which failed so the
    report can cite concrete blockers instead of claiming research was 'unavailable'."""
    sources = [news(), reddit()]
    failed = [s["source"] for s in sources if not s.get("ok")]
    return {
        "gathered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "failed_sources": failed,
        "all_ok": not failed,
    }


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="merch.research")
    ap.add_argument("cmd", choices=["brief", "news", "reddit"], nargs="?", default="brief")
    args = ap.parse_args(argv)
    out = {"brief": brief, "news": news, "reddit": reddit}[args.cmd]()
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
