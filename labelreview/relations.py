"""Minimal local relations library (stdlib sqlite3).

Links document <-> clause <-> rule <-> coverage <-> version for ONE published
bundle. Built only from the published bundle (rules.json, knowledge/*.json and
its pinned clause pack), never from drafts or the root rules.json. The build is
deterministic: no timestamps, rows inserted in sorted order, and
dump_digest() hashes a canonical SQL dump, so rebuilding the same bundle gives
the same digest. The database is a derived artefact and can be deleted and
rebuilt at any time; it is not a source of truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

import labelreview.registry as registry_mod
SCHEMA_VERSION = "relations-1"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE versions (bundle_id TEXT NOT NULL, component TEXT NOT NULL, value TEXT NOT NULL,
                       PRIMARY KEY (bundle_id, component));
CREATE TABLE documents (document_id TEXT PRIMARY KEY, standard_id TEXT, filename TEXT, format TEXT,
                        sha256 TEXT, effective_date TEXT, clause_count INTEGER, parse_version TEXT);
CREATE TABLE file_links (standard_id TEXT NOT NULL, document_id TEXT NOT NULL, conflict INTEGER NOT NULL,
                         diff_count INTEGER, PRIMARY KEY (standard_id, document_id));
CREATE TABLE clauses (document_id TEXT NOT NULL, clause TEXT NOT NULL, kind TEXT, title TEXT, page INTEGER,
                      parent TEXT, text_hash TEXT, text_length INTEGER, verification_status TEXT,
                      PRIMARY KEY (document_id, clause));
CREATE TABLE rules (rule_id TEXT PRIMARY KEY, title TEXT, enabled INTEGER, implementation_kind TEXT,
                    implementation_version TEXT);
CREATE TABLE rule_citations (rule_id TEXT NOT NULL, edition TEXT NOT NULL, document_id TEXT NOT NULL,
                             clause TEXT NOT NULL, pages TEXT, clause_resolved INTEGER NOT NULL,
                             PRIMARY KEY (rule_id, edition, document_id, clause));
CREATE TABLE coverage (standard TEXT NOT NULL, clause TEXT NOT NULL, title TEXT, status TEXT, note TEXT,
                       PRIMARY KEY (standard, clause));
CREATE TABLE coverage_rules (standard TEXT NOT NULL, clause TEXT NOT NULL, rule_id TEXT NOT NULL,
                             rule_registered INTEGER NOT NULL, PRIMARY KEY (standard, clause, rule_id));
CREATE TABLE coverage_documents (standard TEXT NOT NULL, clause TEXT NOT NULL, document_id TEXT NOT NULL,
                                 clause_resolved INTEGER NOT NULL, PRIMARY KEY (standard, clause, document_id));
CREATE INDEX rule_citations_by_clause ON rule_citations (document_id, clause);
CREATE INDEX coverage_rules_by_rule ON coverage_rules (rule_id);
"""


def split_clauses(field):
    """'4.5、附录 A.1' -> ['4.5', 'A.1']. Splits only on list separators."""
    text = str(field or "").replace("附录", " ")
    out = []
    for part in text.replace("，", "、").replace(",", "、").replace("；", "、").replace(";", "、").split("、"):
        part = part.strip()
        if part:
            out.append(part)
    return out


def default_path(registry=None, bundle_id=None):
    registry = registry or registry_mod.default_registry()
    bundle_id = bundle_id or (registry.current() or {}).get("bundleId")
    return registry.store / "relations" / f"{bundle_id}.sqlite"


def build(registry=None, bundle_id=None, path=None):
    """(Re)build the relations DB for a published bundle. Atomic: temp file + rename."""
    registry = registry or registry_mod.default_registry()
    bundle_id = bundle_id or (registry.current() or {}).get("bundleId")
    bundle = registry.load_bundle(bundle_id)
    if not bundle:
        raise ValueError(f"发布版本 {bundle_id} 不存在。")
    folder = registry._bundle_dir(bundle_id)
    path = Path(path) if path else default_path(registry, bundle_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")

    coverage_file = folder / "knowledge" / "coverage.json"
    if coverage_file.is_file():
        coverage = json.loads(coverage_file.read_text(encoding="utf-8"))
        coverage_source = "bundle"
    else:
        # Legacy bundles (bundle-bootstrap) predate coverage in the bundle.
        coverage = json.loads((registry.knowledge_dir / "coverage.json").read_text(encoding="utf-8"))
        coverage_source = "knowledge-dir(legacy-bundle)"
    ref = json.loads((folder / "knowledge" / "index-ref.json").read_text(encoding="utf-8"))
    clauses_by_doc = bundle["knowledge"]["clauses"] or {}
    manifest_docs = (bundle["knowledge"]["manifest"] or {}).get("documents") or []
    links = bundle["knowledge"]["fileLinks"] or []
    rules_book = bundle["rules"] or {}
    versions = (bundle["meta"] or {}).get("versions") or {}

    standard_of = {}
    for group in links:
        for item in group.get("files") or []:
            standard_of[item.get("documentId")] = group.get("standardId")

    con = sqlite3.connect(str(tmp))
    try:
        con.executescript(SCHEMA)
        meta_rows = {
            "schemaVersion": SCHEMA_VERSION,
            "bundleId": bundle_id,
            "packFingerprint": ref.get("packFingerprint") or ref.get("fingerprint") or "",
            "parseVersion": ref.get("parseVersion") or "",
            "coverageSource": coverage_source,
            "coverageStandard": coverage.get("standard") or "",
        }
        con.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta_rows.items()))
        con.executemany("INSERT INTO versions VALUES (?, ?, ?)",
                        sorted((bundle_id, k, str(v)) for k, v in versions.items() if v is not None))

        clause_keys = set()
        doc_rows, clause_rows = [], []
        for meta in sorted(manifest_docs, key=lambda m: m.get("id") or ""):
            key = meta.get("id")
            pack = clauses_by_doc.get(key) or {}
            doc_rows.append((key, standard_of.get(key), meta.get("filename"), meta.get("format"),
                             meta.get("sha256"), meta.get("effectiveDate"),
                             pack.get("clauseCount") or len(pack.get("slices") or []),
                             pack.get("parseVersion")))
            seen = set()
            for item in pack.get("slices") or []:
                number = item.get("clause")
                if not number or number in seen:
                    continue  # first slice wins, same as clause_index.read_clause
                seen.add(number)
                clause_keys.add((key, number))
                text = item.get("fullText") or item.get("text") or ""
                clause_rows.append((key, number, item.get("kind"), item.get("title"), item.get("page"),
                                    item.get("parent"), item.get("fullTextHash") or item.get("textHash"),
                                    len(text), item.get("verificationStatus")))
        con.executemany("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)", doc_rows)
        con.executemany("INSERT INTO clauses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", sorted(clause_rows))

        link_rows = []
        for group in links:
            diff = (group.get("pairDiff") or {}).get("diffCount")
            for item in group.get("files") or []:
                link_rows.append((group.get("standardId"), item.get("documentId"),
                                  1 if group.get("conflict") else 0, diff))
        con.executemany("INSERT OR IGNORE INTO file_links VALUES (?, ?, ?, ?)", sorted(link_rows))

        rules = rules_book.get("rules") or {}
        con.executemany("INSERT INTO rules VALUES (?, ?, ?, ?, ?)", sorted(
            (rid, r.get("title"), 1 if r.get("enabled", True) else 0, r.get("implementationKind"),
             r.get("implementationVersion")) for rid, r in rules.items()))
        cite_rows = set()
        for rid, r in rules.items():
            for c in r.get("citations") or []:
                doc = c.get("document")
                for number in split_clauses(c.get("clause")):
                    if not doc:
                        continue
                    cite_rows.add((rid, str(c.get("edition") or ""), doc, number,
                                   json.dumps(c.get("pages") or [], ensure_ascii=False),
                                   1 if (doc, number) in clause_keys else 0))
        con.executemany("INSERT INTO rule_citations VALUES (?, ?, ?, ?, ?, ?)", sorted(cite_rows))

        standard = coverage.get("standard") or ""
        std_docs = sorted(d for d, s in standard_of.items() if s == standard)
        cov_rows, cov_rule_rows, cov_doc_rows = [], [], []
        for number, body in sorted((coverage.get("clauses") or {}).items()):
            cov_rows.append((standard, number, body.get("title"), body.get("status"), body.get("note")))
            rule_ids = body.get("rule") or []
            if isinstance(rule_ids, str):
                rule_ids = [rule_ids]
            for rid in sorted(set(rule_ids)):
                cov_rule_rows.append((standard, number, rid, 1 if rid in rules else 0))
            for doc in std_docs:
                cov_doc_rows.append((standard, number, doc, 1 if (doc, number) in clause_keys else 0))
        con.executemany("INSERT INTO coverage VALUES (?, ?, ?, ?, ?)", cov_rows)
        con.executemany("INSERT INTO coverage_rules VALUES (?, ?, ?, ?)", cov_rule_rows)
        con.executemany("INSERT INTO coverage_documents VALUES (?, ?, ?, ?)", cov_doc_rows)
        con.commit()
    finally:
        con.close()
    with open(tmp, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def dump_digest(path):
    con = sqlite3.connect(str(path))
    try:
        blob = "\n".join(con.iterdump()).encode("utf-8")
    finally:
        con.close()
    return hashlib.sha256(blob).hexdigest()


def connect(path):
    con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def rule_clauses(con, rule_id):
    return [dict(row) for row in con.execute(
        "SELECT rule_id, edition, document_id, clause, clause_resolved FROM rule_citations "
        "WHERE rule_id = ? ORDER BY edition, document_id, clause", (rule_id,))]


def clause_rules(con, document_id, clause):
    return [row[0] for row in con.execute(
        "SELECT DISTINCT rule_id FROM rule_citations WHERE document_id = ? AND clause = ? ORDER BY rule_id",
        (document_id, clause))]


def coverage_for(con, clause, standard=None):
    sql = "SELECT * FROM coverage WHERE clause = ?" + (" AND standard = ?" if standard else "")
    row = con.execute(sql, (clause, standard) if standard else (clause,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["rules"] = [r[0] for r in con.execute(
        "SELECT rule_id FROM coverage_rules WHERE standard = ? AND clause = ? ORDER BY rule_id",
        (out["standard"], clause))]
    out["documents"] = [dict(r) for r in con.execute(
        "SELECT document_id, clause_resolved FROM coverage_documents WHERE standard = ? AND clause = ? "
        "ORDER BY document_id", (out["standard"], clause))]
    return out


if __name__ == "__main__":
    import sys
    target = build(bundle_id=sys.argv[1] if len(sys.argv) > 1 else None)
    print(target, dump_digest(target))
