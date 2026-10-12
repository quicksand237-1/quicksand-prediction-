#!/usr/bin/env python3
"""Collect local, auditable news documents for the Musk forecast project.

This module deliberately stops before AI event approval.  It stores source
documents and heuristic event candidates; ``source_events`` remains an
explicit, validated import boundary owned by ``polymarket_pipeline.py``.
Only standard-library modules are used so the collector can run offline from
fixtures and independently of Codex.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

from polymarket_pipeline import MarketStore, json_dumps, utc_now


DEFAULT_SOURCES = "news_sources.example.json"
DEFAULT_DB = "data/extended.sqlite"
USER_AGENT = "AIPRP-local-news-corpus/1.0 (research; contact unavailable)"
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
TRACKING_QUERY_PREFIXES = ("utm_",)
MAX_EXCERPT_CHARS = 4000
MUSK_TERMS = ("elon musk", "musk", "tesla", "spacex", "xai", "x.com", "twitter")
TOPIC_TERMS = {
    "tesla": ("tesla", "tsla", "robotaxi", "cybercab", "autopilot", "fsd"),
    "spacex": ("spacex", "starship", "falcon", "launch", "rocket"),
    "xai": ("xai", "grok"),
    "policy": ("regulator", "sec", "government", "white house", "policy", "congress"),
    "travel": ("travel", "flight", "airport", "visit", "summit"),
    "social": ("x.com", "twitter", "tweet", "post", "account"),
}


def iso_utc(value: Any, fallback: dt.datetime | None = None) -> dt.datetime | None:
    if value is None or not str(value).strip():
        return fallback
    text = str(value).strip()
    parsed: dt.datetime | None = None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            parsed = None
    if parsed is None:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def clean_text(value: Any, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Extract readable text from feed HTML without retaining page markup."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text[:limit]


def normalize_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    query = []
    for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.lower()
        if lower in TRACKING_QUERY_KEYS or any(lower.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query.append((key, item))
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", urllib.parse.urlencode(sorted(query)), "")
    )


def content_hash(title: str, excerpt: str) -> str:
    normalized = re.sub(r"\s+", " ", f"{title}\n{excerpt}").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def host_matches(url: str, domain: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    domain = domain.lower().lstrip(".")
    return host.lower() == domain or host.lower().endswith("." + domain)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(node: ET.Element, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    for child in node.iter():
        if child is node or local_name(child.tag) not in wanted:
            continue
        if child.text:
            return child.text.strip()
    return ""


def entry_link(node: ET.Element) -> str:
    for child in node.iter():
        if local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href.strip()
        if child.text:
            return child.text.strip()
    return ""


def parse_feed(payload: bytes | str, source: dict[str, Any], retrieved_at: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Parse RSS 2.0 and Atom into one stable document shape."""
    retrieved = (retrieved_at or utc_now()).astimezone(dt.timezone.utc)
    root = ET.fromstring(payload)
    nodes = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    documents = []
    for node in nodes:
        title = clean_text(child_text(node, ("title",)))
        url = normalize_url(entry_link(node))
        published = iso_utc(child_text(node, ("pubdate", "published", "updated", "date")))
        summary = clean_text(child_text(node, ("description", "summary", "content", "encoded")))
        if not url or not title:
            continue
        title_only = False
        if not summary and bool(source.get("allow_title_only", False)) and int(source.get("tier", 3)) == 1:
            # Some official Atom feeds expose a filing title but no reusable body.
            # Keep that limitation visible instead of treating the title as full text.
            summary = f"Official title-only entry; full text was not collected: {title}"
            title_only = True
        documents.append({
            "source_id": str(source.get("source_id") or source.get("id") or "").strip(),
            "source_name": str(source.get("name") or source.get("source_id") or "").strip(),
            "domain": str(source.get("domain") or "").strip().lower(),
            "tier": int(source.get("tier", 3)),
            "reliability": float(source.get("reliability", 0.5)),
            "url": url,
            "title": title,
            "published_at_utc": published.isoformat() if published else None,
            "retrieved_at_utc": retrieved.isoformat(),
            "excerpt": summary,
            "content_hash": content_hash(title, summary),
            "raw_metadata": {
                "feed_entry": True,
                "source_url": source.get("url"),
                "title_only": title_only,
            },
        })
    return documents


def load_sources(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("sources", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("source configuration must be a JSON list or an object with sources[]")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("url") or not row.get("source_id"):
            continue
        result.append(row)
    return result


class NewsCorpusStore:
    def __init__(self, db_path: Path) -> None:
        self.market_store = MarketStore(db_path)
        self.conn = self.market_store.conn
        self.conn.executescript(
            """
            create table if not exists source_documents (
                document_id text primary key,
                source_id text not null,
                source_name text not null,
                source_domain text not null,
                source_tier integer not null,
                source_reliability real not null,
                url text not null,
                normalized_url text not null unique,
                title text not null,
                published_at_utc text,
                retrieved_at_utc text not null,
                available_at_utc text not null,
                time_precision text not null default 'unknown',
                time_quality text not null default 'unverified',
                excerpt text not null,
                content_hash text not null,
                duplicate_cluster text not null,
                content_policy text not null,
                corpus_eligible integer not null default 0,
                status text not null,
                rejection_reason text not null,
                raw_metadata_json text not null
            );
            create table if not exists document_versions (
                version_id text primary key,
                document_id text not null,
                captured_at_utc text not null,
                available_at_utc text,
                time_precision text not null default 'unknown',
                time_quality text not null default 'unverified',
                content_hash text not null,
                excerpt text not null,
                raw_metadata_json text not null,
                unique(document_id, content_hash),
                foreign key(document_id) references source_documents(document_id)
            );
            create table if not exists crawl_runs (
                run_id text primary key,
                started_at_utc text not null,
                finished_at_utc text,
                status text not null,
                source_count integer not null default 0,
                document_count integer not null default 0,
                error_count integer not null default 0,
                config_json text not null
            );
            create table if not exists crawl_errors (
                error_id text primary key,
                run_id text not null,
                source_id text,
                url text,
                occurred_at_utc text not null,
                error_type text not null,
                message text not null,
                raw_json text not null,
                foreign key(run_id) references crawl_runs(run_id)
            );
            create table if not exists event_candidates (
                candidate_id text primary key,
                document_id text not null,
                candidate_key text not null,
                kind text not null,
                direction text not null,
                relevance_score real not null,
                available_at_utc text,
                time_precision text not null default 'unknown',
                time_quality text not null default 'unverified',
                extraction_method text not null,
                status text not null,
                decision_reason text not null,
                summary text not null,
                raw_json text not null,
                unique(document_id, candidate_key),
                foreign key(document_id) references source_documents(document_id)
            );
            create index if not exists idx_source_documents_published on source_documents(published_at_utc);
            create index if not exists idx_source_documents_corpus on source_documents(corpus_eligible, published_at_utc);
            create index if not exists idx_event_candidates_document on event_candidates(document_id);
            create index if not exists idx_source_documents_available on source_documents(available_at_utc);
            create index if not exists idx_event_candidates_available on event_candidates(available_at_utc);
            """
        )
        for table, columns in {
            "source_documents": {
                "time_precision": "text not null default 'unknown'",
                "time_quality": "text not null default 'unverified'",
            },
            "document_versions": {
                "available_at_utc": "text",
                "time_precision": "text not null default 'unknown'",
                "time_quality": "text not null default 'unverified'",
            },
            "event_candidates": {
                "available_at_utc": "text",
                "time_precision": "text not null default 'unknown'",
                "time_quality": "text not null default 'unverified'",
            },
        }.items():
            existing = {row[1] for row in self.conn.execute(f"pragma table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    self.conn.execute(f"alter table {table} add column {column} {definition}")
        self.conn.commit()

    def close(self) -> None:
        self.market_store.close()

    def start_run(self, sources: list[dict[str, Any]]) -> str:
        run_id = hashlib.sha256(f"{utc_now().isoformat()}|{time.time_ns()}".encode()).hexdigest()[:32]
        with self.conn:
            self.conn.execute(
                "insert into crawl_runs(run_id, started_at_utc, status, source_count, config_json) values (?, ?, ?, ?, ?)",
                (run_id, utc_now().isoformat(), "running", len(sources), json_dumps(sources)),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, documents: int, errors: int) -> None:
        with self.conn:
            self.conn.execute(
                "update crawl_runs set finished_at_utc=?, status=?, document_count=?, error_count=? where run_id=?",
                (utc_now().isoformat(), status, documents, errors, run_id),
            )

    def add_error(self, run_id: str, source_id: str, url: str, error: Exception) -> None:
        error_id = hashlib.sha256(f"{run_id}|{source_id}|{url}|{time.time_ns()}".encode()).hexdigest()[:40]
        with self.conn:
            self.conn.execute(
                "insert into crawl_errors values (?, ?, ?, ?, ?, ?, ?, ?)",
                (error_id, run_id, source_id, url, utc_now().isoformat(), type(error).__name__, str(error)[:1000], json_dumps({})),
            )

    def save_document(self, document: dict[str, Any], source: dict[str, Any]) -> tuple[str, bool]:
        url = document["url"]
        if not host_matches(url, str(source.get("domain") or "")):
            raise ValueError(f"document host is outside configured source domain: {url}")
        document_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]
        policy = str(source.get("content_policy", "metadata_excerpt"))
        discovery_only = bool(source.get("discovery_only", False)) or policy == "discovery_only"
        published = iso_utc(document.get("published_at_utc"))
        retrieved = iso_utc(document["retrieved_at_utc"]) or utc_now()
        eligible = int(not discovery_only and int(document["tier"]) <= 2 and bool(document.get("excerpt")))
        status = "accepted" if published else "rejected"
        reason = "" if published else "missing_published_at"
        cluster = document["content_hash"][:24]
        metadata = dict(document.get("raw_metadata") or {})
        metadata.update({"discovery_only": discovery_only, "content_policy": policy})
        with self.conn:
            self.conn.execute(
                """insert into source_registry(
                    source_id, name, domain, tier, reliability, enabled,
                    allowed_topics, notes, updated_at_utc
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id) do update set
                    name=excluded.name, domain=excluded.domain, tier=excluded.tier,
                    reliability=excluded.reliability, allowed_topics=excluded.allowed_topics,
                    notes=excluded.notes, updated_at_utc=excluded.updated_at_utc""",
                (document["source_id"], document["source_name"], document["domain"], int(document["tier"]),
                 float(document["reliability"]), 1 if source.get("enabled", True) else 0,
                 json_dumps(source.get("allowed_topics", [])), str(source.get("notes") or ""), retrieved.isoformat()),
            )
            existing = self.conn.execute("select document_id from source_documents where normalized_url=?", (url,)).fetchone()
            if existing:
                document_id = str(existing["document_id"])
            self.conn.execute(
                """insert into source_documents(
                    document_id, source_id, source_name, source_domain, source_tier,
                    source_reliability, url, normalized_url, title, published_at_utc,
                    retrieved_at_utc, available_at_utc, time_precision, time_quality, excerpt, content_hash,
                    duplicate_cluster, content_policy, corpus_eligible, status,
                    rejection_reason, raw_metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(document_id) do update set
                    source_id=excluded.source_id, source_name=excluded.source_name,
                    source_domain=excluded.source_domain, source_tier=excluded.source_tier,
                    source_reliability=excluded.source_reliability, title=excluded.title,
                    published_at_utc=coalesce(excluded.published_at_utc, source_documents.published_at_utc),
                    retrieved_at_utc=excluded.retrieved_at_utc,
                    available_at_utc=case when excluded.available_at_utc < source_documents.available_at_utc
                        then excluded.available_at_utc else source_documents.available_at_utc end,
                    time_precision=excluded.time_precision, time_quality=excluded.time_quality,
                    excerpt=excluded.excerpt, content_hash=excluded.content_hash,
                    duplicate_cluster=excluded.duplicate_cluster, content_policy=excluded.content_policy,
                    corpus_eligible=excluded.corpus_eligible, status=excluded.status,
                    rejection_reason=excluded.rejection_reason,
                    raw_metadata_json=excluded.raw_metadata_json
                """,
                (document_id, document["source_id"], document["source_name"], document["domain"], int(document["tier"]),
                 float(document["reliability"]), url, url, document["title"], published.isoformat() if published else None,
                 retrieved.isoformat(), retrieved.isoformat(), "second" if published else "unknown",
                 "source_timestamp" if published else "retrieval_only", document.get("excerpt", ""), document["content_hash"],
                 cluster, policy, eligible, status, reason, json_dumps(metadata)),
            )
            version_id = hashlib.sha256(f"{document_id}|{document['content_hash']}".encode()).hexdigest()[:40]
            self.conn.execute(
                """insert or ignore into document_versions(
                    version_id, document_id, captured_at_utc, available_at_utc,
                    time_precision, time_quality, content_hash, excerpt, raw_metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (version_id, document_id, retrieved.isoformat(), retrieved.isoformat(),
                 "second" if published else "unknown", "source_timestamp" if published else "retrieval_only",
                 document["content_hash"], document.get("excerpt", ""), json_dumps(metadata)),
            )
        return document_id, bool(eligible and status == "accepted")

    def save_candidate(self, document_id: str, document: dict[str, Any], source: dict[str, Any]) -> None:
        text = f"{document.get('title', '')} {document.get('excerpt', '')}".lower()
        topics = [kind for kind, terms in TOPIC_TERMS.items() if any(term in text for term in terms)]
        if not any(term in text for term in MUSK_TERMS):
            return
        kind = topics[0] if topics else "news"
        candidate_key = hashlib.sha256(f"{document['content_hash']}|{kind}".encode()).hexdigest()[:32]
        direction = "unknown"
        summary = document.get("excerpt") or document.get("title", "")
        relevance = min(1.0, 0.45 + 0.1 * min(len(topics), 4))
        candidate_id = hashlib.sha256(f"{document_id}|{candidate_key}".encode()).hexdigest()[:40]
        with self.conn:
            self.conn.execute(
                """insert or ignore into event_candidates(
                    candidate_id, document_id, candidate_key, kind, direction,
                    relevance_score, available_at_utc, time_precision, time_quality,
                    extraction_method, status, decision_reason,
                    summary, raw_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (candidate_id, document_id, candidate_key, kind, direction, relevance,
                 document.get("available_at_utc") or document.get("retrieved_at_utc"),
                 "second" if document.get("published_at_utc") else "unknown",
                 "source_timestamp" if document.get("published_at_utc") else "retrieval_only",
                 "keyword_prefilter", "pending_review", "AI_or_human_review_required", summary, json_dumps({"topics": topics, "source": source.get("source_id")})),
            )

    def export_corpus(self, output: Path, as_of: dt.datetime | None = None, limit: int | None = None) -> int:
        as_of = as_of or utc_now()
        sql = """select d.*, c.kind, c.direction, c.relevance_score as candidate_relevance,
                 c.status as candidate_status, c.summary as candidate_summary
                 from source_documents d left join event_candidates c on c.document_id=d.document_id
                 where d.corpus_eligible=1 and d.status='accepted'
                   and d.published_at_utc <= ? and d.available_at_utc <= ?
                 order by d.published_at_utc, d.document_id"""
        params: list[Any] = [as_of.isoformat(), as_of.isoformat()]
        if limit is not None:
            sql += " limit ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="\n") as file:
            for row in rows:
                file.write(json.dumps({
                    "document_id": row["document_id"], "source_id": row["source_id"],
                    "source_tier": row["source_tier"], "source_reliability": row["source_reliability"],
                    "url": row["url"], "title": row["title"],
                    "published_at_utc": row["published_at_utc"], "available_at_utc": row["available_at_utc"],
                    "excerpt": row["excerpt"], "content_hash": row["content_hash"],
                    "kind": row["kind"], "candidate_direction": row["direction"],
                    "candidate_relevance": row["candidate_relevance"],
                    "candidate_status": row["candidate_status"],
                }, ensure_ascii=False) + "\n")
        return len(rows)


def fetch_url(
    url: str,
    proxy: str | None,
    timeout: int,
    max_bytes: int,
    user_agent: str = USER_AGENT,
) -> bytes:
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    with opener.open(request, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"response exceeds max_bytes={max_bytes}")
        return data


def crawl_sources(
    store: NewsCorpusStore,
    sources: list[dict[str, Any]],
    proxy: str | None = None,
    timeout: int = 30,
    max_bytes: int = 2_000_000,
    max_items: int = 200,
    sleep_seconds: float = 0.2,
    user_agent: str = USER_AGENT,
) -> dict[str, Any]:
    run_id = store.start_run(sources)
    documents = errors = 0
    try:
        for source in sources:
            if not source.get("enabled", True):
                continue
            url = str(source["url"])
            try:
                parsed = parse_feed(fetch_url(url, proxy, timeout, max_bytes, user_agent), source)
                for document in parsed[:max_items]:
                    document_id, _ = store.save_document(document, source)
                    store.save_candidate(document_id, document, source)
                    documents += 1
            except (OSError, urllib.error.URLError, ET.ParseError, ValueError) as error:
                errors += 1
                store.add_error(run_id, str(source.get("source_id", "")), url, error)
            if sleep_seconds:
                time.sleep(sleep_seconds)
        status = "partial" if errors else "success"
    except Exception as error:
        errors += 1
        store.add_error(run_id, "", "", error)
        status = "failed"
    store.finish_run(run_id, status, documents, errors)
    return {"run_id": run_id, "status": status, "documents_seen": documents, "errors": errors}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Build a local auditable news corpus for Musk market research.")
    sub = root.add_subparsers(dest="command", required=True)
    crawl = sub.add_parser("crawl-news", help="Fetch configured RSS/Atom sources into SQLite.")
    crawl.add_argument("--db", default=DEFAULT_DB)
    crawl.add_argument("--sources", default=DEFAULT_SOURCES)
    crawl.add_argument("--proxy")
    crawl.add_argument("--timeout", type=int, default=30)
    crawl.add_argument("--max-bytes", type=int, default=2_000_000)
    crawl.add_argument("--max-items", type=int, default=200)
    crawl.add_argument("--sleep-seconds", type=float, default=0.2)
    crawl.add_argument(
        "--user-agent",
        default=USER_AGENT,
        help="Public contact identifier required by some official sources; never include an API key.",
    )
    crawl.set_defaults(func=run_crawl)
    export = sub.add_parser("export-corpus", help="Export eligible, point-in-time-safe JSONL corpus.")
    export.add_argument("--db", default=DEFAULT_DB)
    export.add_argument("--output", default="data/news_corpus.jsonl")
    export.add_argument("--as-of", help="UTC ISO timestamp; defaults to now.")
    export.add_argument("--limit", type=int)
    export.set_defaults(func=run_export)
    return root


def run_crawl(args: argparse.Namespace) -> None:
    store = NewsCorpusStore(Path(args.db))
    try:
        print(json.dumps(crawl_sources(
            store, load_sources(Path(args.sources)), args.proxy, args.timeout,
            args.max_bytes, args.max_items, args.sleep_seconds, args.user_agent,
        ), ensure_ascii=False))
    finally:
        store.close()


def run_export(args: argparse.Namespace) -> None:
    store = NewsCorpusStore(Path(args.db))
    try:
        as_of = iso_utc(args.as_of) if args.as_of else None
        if args.as_of and as_of is None:
            raise ValueError("--as-of must be an ISO-8601 timestamp")
        count = store.export_corpus(Path(args.output), as_of, args.limit)
        print(json.dumps({"output": args.output, "documents_exported": count}, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit((lambda args: (args.func(args), 0)[1])(parser().parse_args()))
