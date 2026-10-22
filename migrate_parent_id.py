#!/usr/bin/env python3
"""
migrate_parent_id.py — rename this index's columns to the shared vector schema.

    python migrate_parent_id.py --db /home/tobias/mcp/data/hbls.db

The old embed_db.py called the parent column `chunks.doc_id` and the run
counter `embedding_runs.n_articles`; the shared vectorstore.py (thodel/hbls_mcp#9,
thodel/ch-h-bot#555) says `parent_id` and `n_parents`. Nothing else changes:
chunk ids, offsets, texts and vectors stay, so nothing is re-embedded. In one
transaction:

    ALTER TABLE chunks RENAME COLUMN doc_id TO parent_id;
    ALTER TABLE embedding_runs RENAME COLUMN n_articles TO n_parents;
    DROP INDEX ix_chunks_entry;
    CREATE INDEX ix_chunks_parent ON chunks(parent_id);

then PRAGMA integrity_check. DROP INDEX is needed: RENAME COLUMN re-points the
old index but keeps its name, and vectorstore.SCHEMA_SQL would create a second,
identical one. The UNIQUE (parent_id, chunk_index) constraint follows the rename
by itself.

Idempotent: a step already done is reported and skipped, so a second run is a
no-op that still prints the counts and runs the integrity check.

The backup is the operator's job; this script makes none. Stop the server
first — the old db.py asks for doc_id and would answer every search_semantic
with «no such column» on a renamed database, the new one the other way round.
Needs SQLite 3.25 or newer (RENAME COLUMN); idhefix has 3.45.1.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

MIN_SQLITE = (3, 25, 0)


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def indexes(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA index_list({table})")]


def counts(conn: sqlite3.Connection, parent_col: str) -> dict:
    """What the index holds, with the join to articles and the pending vectors
    — the numbers that must not change."""
    pending = conn.execute(
        "SELECT COUNT(*) FROM chunks c LEFT JOIN embeddings e "
        "ON e.chunk_id = c.chunk_id WHERE e.chunk_id IS NULL").fetchone()[0]
    joined = conn.execute(
        f"SELECT COUNT(*) FROM chunks c JOIN articles a ON a.id = c.{parent_col}"
    ).fetchone()[0]
    return {
        "articles": conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "chunks_joined_to_an_article": joined,
        "parents": conn.execute(
            f"SELECT COUNT(DISTINCT {parent_col}) FROM chunks").fetchone()[0],
        "embeddings": conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        "chunks_without_vector": pending,
    }


def migrate(conn: sqlite3.Connection, log=print) -> list[str]:
    """Run the steps still to do, in one transaction. Returns what was done."""
    done: list[str] = []
    chunk_cols = columns(conn, "chunks")
    run_cols = columns(conn, "embedding_runs")
    if "parent_id" not in chunk_cols and "doc_id" not in chunk_cols:
        raise SystemExit("chunks has neither doc_id nor parent_id — not this index")

    conn.isolation_level = None          # explicit transaction below
    conn.execute("BEGIN")
    try:
        if "doc_id" in chunk_cols:
            conn.execute("ALTER TABLE chunks RENAME COLUMN doc_id TO parent_id")
            done.append("chunks.doc_id -> parent_id")
        else:
            log("  chunks.parent_id already there")
        if "n_articles" in run_cols:
            conn.execute(
                "ALTER TABLE embedding_runs RENAME COLUMN n_articles TO n_parents")
            done.append("embedding_runs.n_articles -> n_parents")
        else:
            log("  embedding_runs.n_parents already there")
        if "ix_chunks_entry" in indexes(conn, "chunks"):
            conn.execute("DROP INDEX ix_chunks_entry")
            done.append("DROP INDEX ix_chunks_entry")
        else:
            log("  ix_chunks_entry already gone")
        if "ix_chunks_parent" not in indexes(conn, "chunks"):
            conn.execute("CREATE INDEX ix_chunks_parent ON chunks(parent_id)")
            done.append("CREATE INDEX ix_chunks_parent")
        else:
            log("  ix_chunks_parent already there")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return done


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("HBLS_DB", "/data/hbls.db"))
    args = parser.parse_args(argv)

    if sqlite3.sqlite_version_info < MIN_SQLITE:
        print(f"ERROR: SQLite {sqlite3.sqlite_version} cannot RENAME COLUMN "
              f"(needs {'.'.join(map(str, MIN_SQLITE))})", file=sys.stderr)
        return 1
    if not os.path.exists(args.db):
        print(f"ERROR: database not found at {args.db}", file=sys.stderr)
        return 1

    print(f"{args.db} (SQLite {sqlite3.sqlite_version}); no backup is made here")
    conn = sqlite3.connect(args.db)
    try:
        if "chunks" not in [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]:
            print("ERROR: no chunks table — nothing to migrate", file=sys.stderr)
            return 1
        before_col = "doc_id" if "doc_id" in columns(conn, "chunks") else "parent_id"
        before = counts(conn, before_col)
        print("before:", before)

        done = migrate(conn, log=print)
        print("done:  ", "; ".join(done) if done else "nothing, already migrated")

        after = counts(conn, "parent_id")
        print("after: ", after)
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print("integrity_check:", check)
        if check != "ok":
            return 2
        if after != before:
            print("ERROR: the counts changed", file=sys.stderr)
            return 2
        if after["chunks_without_vector"]:
            print(f"NOTE: {after['chunks_without_vector']} chunks have no vector; "
                  "embed_db.py would embed them", file=sys.stderr)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
