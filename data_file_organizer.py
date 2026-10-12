"""Conservative local data inventory and consolidation helpers.

This module only deletes byte-identical, low-risk duplicate text artifacts
after an explicit local confirmation. SQLite and other database files,
backups, model inputs, tests and live capture sources are always protected.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

SUPPORTED_TEXT_SUFFIXES = {".csv", ".jsonl", ".ndjson", ".json"}
PROTECTED_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".bak", ".zip"}
ARCHIVE_PARTS = {"report_backups", "hourly_research_before", "consolidated"}
RISK_WORDS = {"before", "backup", "smoke", "verify", "test", "model", "retry", "initial", "baseline"}
KEY_COLUMNS = (
    "event_slug", "slug", "token_id", "checkpoint_utc",
    "data_time_utc", "timestamp", "ts", "id",
)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _deletion_safety(relative_path: str, suffix: str) -> tuple[bool, str]:
    path = Path(relative_path)
    if suffix not in SUPPORTED_TEXT_SUFFIXES:
        return False, "仅允许文本数据文件"
    if any(part.lower() in ARCHIVE_PARTS for part in path.parts):
        return False, "备份/归档目录受保护"
    lowered = path.name.lower()
    if any(word in lowered for word in RISK_WORDS):
        return False, "文件名显示为测试、模型、验证或回退产物"
    return True, "逐字节重复且不在受保护目录/命名范围"


def _csv_metadata(path: Path) -> dict[str, Any]:
    header: list[str] = []
    rows = 0
    keys: list[str] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = [str(value).strip() for value in (next(reader, []) or [])]
            keys = [column for column in KEY_COLUMNS if column in header]
            for _ in reader:
                rows += 1
    except (OSError, UnicodeError, csv.Error) as exc:
        return {"parse_status": "error", "parse_error": f"{type(exc).__name__}: {exc}"}
    return {
        "parse_status": "ok",
        "header": header,
        "header_hash": hashlib.sha256("\x1f".join(header).encode()).hexdigest(),
        "row_count": rows,
        "key_columns": keys,
    }


def _jsonl_metadata(path: Path) -> dict[str, Any]:
    rows = 0
    invalid = 0
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                rows += 1
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
    except (OSError, UnicodeError) as exc:
        return {"parse_status": "error", "parse_error": f"{type(exc).__name__}: {exc}"}
    return {"parse_status": "ok", "row_count": rows, "invalid_rows": invalid}


def _candidate_family(name: str) -> str:
    # Backup timestamps and common version suffixes do not change the semantic
    # family, but the original relative path remains in every plan entry.
    stem = Path(name).stem.lower()
    stem = re.sub(r"(?:[._-](?:20\d{6,14}|v?\d+|fullprob|public|private|summary|backup|smoke|verify))+", "", stem)
    return _SAFE_NAME.sub("_", stem).strip("._-") or "unnamed"


def scan_data_files(root: str | Path, max_files: int = 5000) -> dict[str, Any]:
    """Return a read-only inventory and conservative consolidation plan."""
    base = Path(root).resolve()
    if not base.is_dir():
        raise ValueError(f"数据目录不存在：{base}")
    records: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if len(records) >= max_files:
            break
        if not path.is_file() or any(part in {".git", "__pycache__", "consolidated"} for part in path.relative_to(base).parts):
            continue
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_TEXT_SUFFIXES and suffix not in PROTECTED_SUFFIXES:
            continue
        stat = path.stat()
        record: dict[str, Any] = {
            "path": _relative(path, base),
            "name": path.name,
            "suffix": suffix,
            "size": stat.st_size,
            "modified_at": stat.st_mtime,
            "protected": suffix in PROTECTED_SUFFIXES,
        }
        # Hashing a multi-gigabyte database is unnecessary and would make a
        # button click compete with the live collector. Text artifacts are
        # bounded by the configured scan and are safe to hash.
        if not record["protected"]:
            record["sha256"] = _sha256(path)
            if suffix == ".csv":
                record.update(_csv_metadata(path))
            elif suffix in {".jsonl", ".ndjson"}:
                record.update(_jsonl_metadata(path))
            else:
                record["parse_status"] = "not_row_scanned"
        else:
            record["status"] = "protected_source"
        records.append(record)

    by_hash: dict[str, list[str]] = {}
    by_schema: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
    for row in records:
        if row.get("sha256"):
            by_hash.setdefault(str(row["sha256"]), []).append(str(row["path"]))
        if row.get("suffix") == ".csv" and row.get("parse_status") == "ok":
            schema = (str(row["suffix"]), str(row.get("header_hash")), tuple(row.get("header") or []))
            by_schema.setdefault(schema, []).append(str(row["path"]))
    exact_duplicates = [
        {
            "sha256": digest,
            "paths": paths,
            "safe_redundant": True,
            "deletion_candidates": [
                {"path": path, "allowed": _deletion_safety(path, Path(path).suffix.lower())[0],
                 "reason": _deletion_safety(path, Path(path).suffix.lower())[1]}
                for path in paths
            ],
        }
        for digest, paths in by_hash.items() if len(paths) > 1
    ]
    for group in exact_duplicates:
        eligible = [item["path"] for item in group["deletion_candidates"] if item["allowed"]]
        if len(eligible) > 1:
            # Prefer a direct data child and a neutral name as the canonical
            # copy. Ties are resolved by the stable relative path.
            def score(value: str) -> tuple[int, int, str]:
                path = Path(value)
                return (len(path.parts), sum(word in path.name.lower() for word in RISK_WORDS), value)
            canonical = min(eligible, key=score)
            group["canonical_path"] = canonical
            group["deletable_paths"] = [value for value in eligible if value != canonical]
            group["deletion_allowed"] = bool(group["deletable_paths"])
        else:
            group["canonical_path"] = eligible[0] if eligible else None
            group["deletable_paths"] = []
            group["deletion_allowed"] = False
    merge_candidates = []
    for schema, paths in by_schema.items():
        if len(paths) < 2:
            continue
        merge_candidates.append({
            "suffix": schema[0],
            "header": list(schema[2]),
            "paths": paths,
            "family": _candidate_family(paths[0]),
            "safe_to_merge_rows": True,
            "deletion_allowed": False,
            "note": "仅按完整行去重；同一业务键但字段不同的记录全部保留",
        })
    protected = [row for row in records if row.get("protected")]
    return {
        "schema_version": "data_inventory_v1",
        "root": str(base),
        "files": records,
        "file_count": len(records),
        "exact_duplicate_groups": exact_duplicates,
        "merge_candidates": merge_candidates,
        "protected_files": [row["path"] for row in protected],
        "deletion_policy": "delete_only_confirmed_byte_identical_low_risk_duplicates",
        "do_not_auto_trade": True,
    }


def _row_fingerprint(row: Iterable[str]) -> str:
    return hashlib.sha256(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def delete_safe_duplicates(inventory: dict[str, Any], root: str | Path, selected_hashes: Iterable[str] | None = None) -> dict[str, Any]:
    """Delete only revalidated low-risk exact duplicates, keeping one copy."""
    base = Path(root).resolve()
    selected = {str(value) for value in selected_hashes} if selected_hashes is not None else None
    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for group in inventory.get("exact_duplicate_groups") or []:
        digest = str(group.get("sha256") or "")
        if selected is not None and digest not in selected:
            continue
        if not group.get("deletion_allowed"):
            skipped.append({"sha256": digest, "reason": "没有两个以上低风险重复副本"})
            continue
        canonical = str(group.get("canonical_path") or "")
        for relative in group.get("deletable_paths") or []:
            path = (base / str(relative)).resolve()
            try:
                path.relative_to(base)
                if path == base or not path.is_file() or _sha256(path) != digest:
                    raise ValueError("路径不存在或哈希已变化")
                allowed, reason = _deletion_safety(_relative(path, base), path.suffix.lower())
                if not allowed:
                    raise ValueError(reason)
                size = path.stat().st_size
                path.unlink()
                deleted.append({"path": _relative(path, base), "size": size, "sha256": digest, "canonical_path": canonical})
            except (OSError, ValueError) as exc:
                skipped.append({"path": str(relative), "sha256": digest, "reason": str(exc)})
    return {
        "schema_version": "data_deletion_v1",
        "deleted": deleted,
        "skipped": skipped,
        "bytes_freed": sum(int(item["size"]) for item in deleted),
        "sources_deleted": [item["path"] for item in deleted],
        "do_not_auto_trade": True,
    }


def consolidate_csv_candidates(inventory: dict[str, Any], root: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Write deduplicated CSV copies; source files remain byte-for-byte intact."""
    base = Path(root).resolve()
    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    outputs: list[dict[str, Any]] = []
    for index, candidate in enumerate(inventory.get("merge_candidates") or [], 1):
        paths = [base / str(value) for value in candidate.get("paths") or []]
        if not paths or any(not path.is_file() for path in paths):
            continue
        family = _SAFE_NAME.sub("_", str(candidate.get("family") or f"csv_{index}"))
        output = target / f"{index:03d}_{family}_consolidated.csv"
        seen: set[str] = set()
        row_count = 0
        duplicate_rows = 0
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(candidate.get("header") or [])
            for path in paths:
                with path.open("r", encoding="utf-8-sig", newline="") as source:
                    reader = csv.reader(source)
                    next(reader, None)
                    for row in reader:
                        fingerprint = _row_fingerprint(row)
                        if fingerprint in seen:
                            duplicate_rows += 1
                            continue
                        seen.add(fingerprint)
                        writer.writerow(row)
                        row_count += 1
        outputs.append({
            "output": _relative(output, base),
            "sources": [ _relative(path, base) for path in paths ],
            "rows_written": row_count,
            "duplicate_rows_removed_from_copy": duplicate_rows,
            "sources_deleted": [],
        })
    manifest = {
        "schema_version": "data_consolidation_v1",
        "inventory_schema": inventory.get("schema_version"),
        "outputs": outputs,
        "sources_preserved": True,
        "deletion_policy": "delete_only_confirmed_byte_identical_low_risk_duplicates",
        "do_not_auto_trade": True,
    }
    manifest_path = target / "consolidation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["manifest"] = _relative(manifest_path, base)
    return manifest
