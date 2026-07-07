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
        "<entry><title>Frost dragon drops are nuts</title>"
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

    def test_reddit_uses_plain_rss_path_not_hot(self) -> None:
        seen = {}
        def fake_fetch(url, ua=research.UA):
            seen["url"], seen["ua"] = url, ua
            return self._REDDIT_RSS
        with (patch.dict(research.RESEARCH, {"subreddit": "2007scape"}),
              patch.object(research, "_fetch", side_effect=fake_fetch)):
            research.reddit(limit=3)
        self.assertTrue(seen["url"].endswith("/r/2007scape/.rss"))  # not /hot.rss
        self.assertIn("Mozilla", seen["ua"])  # browser UA, not the wiki UA

    def test_reddit_merges_and_tags_multiple_subreddits(self) -> None:
        with (
            patch.dict(research.RESEARCH, {"subreddit": ["2007scape", "OSRS"]}),
            patch.object(research, "REDDIT_LIMIT", 5),
            patch.object(research, "_reddit_via_rss",
                         side_effect=lambda sub, limit: [{"title": f"{sub} post", "url": "u", "published": "p"}]),
            patch.object(research.time, "sleep") as sleeper,
        ):
            out = research.reddit()
        self.assertTrue(out["ok"])
        self.assertEqual(out["subreddits"], ["2007scape", "OSRS"])
        self.assertEqual([(i["subreddit"], i["title"]) for i in out["items"]],
                         [("2007scape", "2007scape post"), ("OSRS", "OSRS post")])
        sleeper.assert_called_once()  # one polite delay between the two fetches



if __name__ == "__main__":
    unittest.main()
