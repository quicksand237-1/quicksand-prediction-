import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from news_event_crawler import (
    NewsCorpusStore,
    clean_text,
    content_hash,
    crawl_sources,
    normalize_url,
    parse_feed,
)


FEED = b'''<?xml version="1.0"?>
<rss version="2.0"><channel><item>
<title>Elon Musk discusses Tesla and SpaceX</title>
<link>https://news.example.test/story?utm_source=rss&amp;id=7#top</link>
<pubDate>Wed, 12 Aug 2026 10:00:00 GMT</pubDate>
<description><![CDATA[<p>Useful <b>summary</b>.</p><script>ignore()</script>]]></description>
</item></channel></rss>'''


class NewsParsingTests(unittest.TestCase):
    def source(self, **extra):
        value = {
            "source_id": "test_source",
            "name": "Test source",
            "domain": "news.example.test",
            "tier": 2,
            "reliability": 0.8,
            "url": "https://news.example.test/feed",
        }
        value.update(extra)
        return value

    def test_rss_is_normalized_and_html_cleaned(self):
        rows = parse_feed(FEED, self.source(), dt.datetime(2026, 8, 12, 11, tzinfo=dt.timezone.utc))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://news.example.test/story?id=7")
        self.assertEqual(rows[0]["excerpt"], "Useful summary.")
        self.assertEqual(rows[0]["published_at_utc"], "2026-08-12T10:00:00+00:00")

    def test_atom_is_supported(self):
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Grok update</title><link href="https://xai.example.test/a"/>
        <updated>2026-08-12T10:00:00Z</updated><summary>Text</summary></entry></feed>'''
        rows = parse_feed(atom, self.source(domain="xai.example.test"))
        self.assertEqual(rows[0]["title"], "Grok update")

    def test_official_title_only_source_is_explicitly_labeled(self):
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Tesla 8-K filing</title><link href="https://www.sec.gov/Archives/test"/>
        <updated>2026-08-12T10:00:00Z</updated></entry></feed>'''
        rows = parse_feed(atom, self.source(domain="sec.gov", tier=1, allow_title_only=True))
        self.assertIn("Official title-only entry", rows[0]["excerpt"])
        self.assertTrue(rows[0]["raw_metadata"]["title_only"])

    def test_non_official_empty_entry_is_not_upgraded_to_title_only(self):
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Unverified title</title><link href="https://news.example.test/story"/>
        <updated>2026-08-12T10:00:00Z</updated></entry></feed>'''
        rows = parse_feed(atom, self.source(allow_title_only=True))
        self.assertEqual(rows[0]["excerpt"], "")

    def test_url_and_content_hash_are_stable(self):
        self.assertEqual(normalize_url("HTTPS://Example.TEST/a/?b=2&utm_medium=x&a=1#fragment"), "https://example.test/a/?a=1&b=2")
        self.assertEqual(content_hash("A", " B  C "), content_hash("a", "b c"))


class NewsStoreTests(unittest.TestCase):
    def source(self, discovery_only=False):
        return {
            "source_id": "test_source", "name": "Test source", "domain": "news.example.test",
            "tier": 2, "reliability": 0.8, "url": "https://news.example.test/feed",
            "discovery_only": discovery_only, "content_policy": "metadata_excerpt",
        }

    def test_document_is_audited_candidate_and_point_in_time_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NewsCorpusStore(Path(directory) / "events.sqlite")
            try:
                rows = parse_feed(FEED, self.source(), dt.datetime(2026, 8, 12, 11, tzinfo=dt.timezone.utc))
                document_id, eligible = store.save_document(rows[0], self.source())
                store.save_candidate(document_id, rows[0], self.source())
                self.assertTrue(eligible)
                self.assertEqual(store.conn.execute("select count(*) from event_candidates").fetchone()[0], 1)
                before = Path(directory) / "before.jsonl"
                self.assertEqual(store.export_corpus(before, dt.datetime(2026, 8, 12, 10, tzinfo=dt.timezone.utc)), 0)
                after = Path(directory) / "after.jsonl"
                self.assertEqual(store.export_corpus(after, dt.datetime(2026, 8, 12, 12, tzinfo=dt.timezone.utc)), 1)
                payload = json.loads(after.read_text(encoding="utf-8"))
                self.assertEqual(payload["candidate_status"], "pending_review")
                self.assertEqual(store.conn.execute("select count(*) from source_registry where source_id='test_source'").fetchone()[0], 1)
                document_time = store.conn.execute(
                    "select available_at_utc, time_precision, time_quality from source_documents where document_id=?",
                    (document_id,),
                ).fetchone()
                version_time = store.conn.execute(
                    "select available_at_utc, time_precision, time_quality from document_versions where document_id=?",
                    (document_id,),
                ).fetchone()
                candidate_time = store.conn.execute(
                    "select available_at_utc, time_precision, time_quality from event_candidates where document_id=?",
                    (document_id,),
                ).fetchone()
                self.assertEqual(document_time[1:], ("second", "source_timestamp"))
                self.assertEqual(version_time[1:], ("second", "source_timestamp"))
                self.assertEqual(candidate_time[1:], ("second", "source_timestamp"))
                self.assertEqual(document_time[0], "2026-08-12T11:00:00+00:00")
            finally:
                store.close()

    def test_discovery_source_never_enters_ai_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NewsCorpusStore(Path(directory) / "events.sqlite")
            try:
                source = self.source(True)
                row = parse_feed(FEED, source)[0]
                document_id, eligible = store.save_document(row, source)
                self.assertFalse(eligible)
                output = Path(directory) / "corpus.jsonl"
                self.assertEqual(store.export_corpus(output), 0)
                self.assertEqual(store.conn.execute("select corpus_eligible, status from source_documents where document_id=?", (document_id,)).fetchone()[0], 0)
            finally:
                store.close()

    def test_duplicate_url_creates_one_document_and_versions_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NewsCorpusStore(Path(directory) / "events.sqlite")
            try:
                source = self.source()
                row = parse_feed(FEED, source)[0]
                first = store.save_document(row, source)
                second = store.save_document(row, source)
                self.assertEqual(first[0], second[0])
                self.assertEqual(store.conn.execute("select count(*) from source_documents").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("select count(*) from document_versions").fetchone()[0], 1)
            finally:
                store.close()

    def test_changed_feed_content_keeps_first_available_time_and_adds_version(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NewsCorpusStore(Path(directory) / "events.sqlite")
            try:
                source = self.source()
                first = parse_feed(FEED, source, dt.datetime(2026, 8, 12, 11, tzinfo=dt.timezone.utc))[0]
                document_id, _ = store.save_document(first, source)
                changed = dict(first)
                changed["retrieved_at_utc"] = "2026-08-12T13:00:00+00:00"
                changed["excerpt"] = "Revised summary."
                changed["content_hash"] = content_hash(changed["title"], changed["excerpt"])
                store.save_document(changed, source)
                row = store.conn.execute(
                    "select available_at_utc, retrieved_at_utc, excerpt from source_documents where document_id=?",
                    (document_id,),
                ).fetchone()
                self.assertEqual(row["available_at_utc"], "2026-08-12T11:00:00+00:00")
                self.assertEqual(row["retrieved_at_utc"], "2026-08-12T13:00:00+00:00")
                self.assertEqual(row["excerpt"], "Revised summary.")
                self.assertEqual(store.conn.execute("select count(*) from document_versions").fetchone()[0], 2)
            finally:
                store.close()

    def test_failed_feed_is_recorded_for_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NewsCorpusStore(Path(directory) / "events.sqlite")
            try:
                result = crawl_sources(store, [{
                    **self.source(), "url": "http://127.0.0.1:1/unavailable",
                }], timeout=1, sleep_seconds=0)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(store.conn.execute("select count(*) from crawl_errors").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("select status from crawl_runs where run_id=?", (result["run_id"],)).fetchone()[0], "partial")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
