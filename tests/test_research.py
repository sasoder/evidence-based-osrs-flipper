from __future__ import annotations

import unittest
import urllib.error
from unittest.mock import patch

from flipper import research

class ResearchTests(unittest.TestCase):
    _NEWS_RSS = (
        '<rss version="2.0"><channel><title>OSRS</title>'
        "<item><title>Frost Dragons &amp; More</title>"
        "<pubDate>Wed, 17 Jun 2026 00:00:00 GMT</pubDate>"
        "<link>https://x/news</link>"
        "<description>&lt;p&gt;New Slayer unlocks&lt;/p&gt;</description></item>"
        "</channel></rss>"
    )

    def test_news_parses_rss_and_strips_html(self) -> None:
        with patch.object(research, "_fetch", return_value=self._NEWS_RSS):
            out = research.news()
        self.assertTrue(out["ok"])
        self.assertEqual(out["items"][0]["title"], "Frost Dragons & More")
        self.assertEqual(out["items"][0]["summary"], "New Slayer unlocks")

    def test_failed_fetch_surfaces_citable_error(self) -> None:
        err = urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
        with patch.object(research, "_fetch", side_effect=err):
            out = research.news()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "HTTP 403")

    _REDDIT_RSS = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><category term="2007scape" label="r/2007scape"/>'
        "<title>Frost dragon drops are nuts</title>"
        '<link href="https://www.reddit.com/r/2007scape/comments/1"/>'
        "<published>2026-06-18T00:00:00+00:00</published></entry>"
        "</feed>"
    )

    def test_reddit_reads_rss_feed(self) -> None:
        with (patch.dict(research.RESEARCH, {"subreddit": "2007scape"}),
              patch.object(research, "_fetch", return_value=self._REDDIT_RSS)):
            out = research.reddit(limit=5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["items"][0]["title"], "Frost dragon drops are nuts")

    def test_reddit_accepts_no_configured_subreddits(self) -> None:
        with (patch.dict(research.RESEARCH, {"subreddit": []}),
              patch.object(research, "_fetch") as fetch):
            out = research.reddit()
        self.assertEqual(out, {
            "source": "reddit", "ok": True, "items": [], "subreddits": []
        })
        fetch.assert_not_called()

    def test_reddit_uses_plain_rss_path_not_hot(self) -> None:
        seen = {}
        def fake_fetch(url, ua=research.UA):
            seen["url"], seen["ua"] = url, ua
            return self._REDDIT_RSS
        with (patch.dict(research.RESEARCH, {"subreddit": "2007scape"}),
              patch.object(research, "_fetch", side_effect=fake_fetch)):
            research.reddit(limit=3)
        self.assertIn("/r/2007scape/.rss?", seen["url"])  # not /hot.rss
        self.assertIn("Mozilla", seen["ua"])  # browser UA, not the wiki UA

    def test_reddit_fetches_multiple_subreddits_once(self) -> None:
        feed = self._REDDIT_RSS.replace(
            "</feed>",
            '<entry><category term="OSRS" label="r/OSRS"/>'
            '<title>OSRS post</title><link href="https://x/2"/>'
            '<published>2026-06-18T00:00:00+00:00</published></entry></feed>',
        )
        with (
            patch.dict(research.RESEARCH, {"subreddit": ["2007scape", "OSRS"]}),
            patch.object(research, "REDDIT_LIMIT", 5),
            patch.object(research, "_fetch", return_value=feed) as fetch,
        ):
            out = research.reddit()
        self.assertTrue(out["ok"])
        self.assertEqual(out["subreddits"], ["2007scape", "OSRS"])
        self.assertEqual([(i["subreddit"], i["title"]) for i in out["items"]],
                         [("2007scape", "Frost dragon drops are nuts"), ("OSRS", "OSRS post")])
        fetch.assert_called_once()

    def test_brief_marks_partial_source_failure(self) -> None:
        with (
            patch.object(research, "news", return_value={"source": "osrs_news", "ok": True}),
            patch.object(research, "reddit", return_value={
                "source": "reddit", "ok": True, "partial_errors": ["r/x: HTTP 429"]
            }),
        ):
            out = research.brief()
        self.assertFalse(out["all_ok"])
        self.assertEqual(out["failed_sources"], ["reddit"])



if __name__ == "__main__":
    unittest.main()
