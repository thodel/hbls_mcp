"""
db.py — SQLite query helpers for HBLS MCP server.
"""

# db.py already used 3.10-only annotations (dict | None), so the module
# could not be imported on 3.9 at all — including by its own tests.
from __future__ import annotations

import re
import os
import sqlite3
from contextlib import contextmanager
from typing import Any

_DB_PATH = "/data/hbls.db"

MAX_LIMIT = 500           # ceiling for any caller-supplied limit
VOLUME_INDEX_LIMIT = 1000 # rows in the hbls://volume/{volume} resource

# The schema this server expects. The database is built by the HBLS pipeline; this
# constant is the contract between it and the server, and the tests build their
# fixtures from it.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY,
    headword      TEXT,
    volume        INTEGER,
    page          INTEGER,
    snippet       TEXT,
    article_text  TEXT,
    pdf_url       TEXT,
    category      TEXT,
    lexical_class TEXT
);
CREATE TABLE IF NOT EXISTS members (
    id          INTEGER PRIMARY KEY,
    article_id  INTEGER REFERENCES articles(id),
    given       TEXT,
    birth_year  INTEGER,
    death_year  INTEGER,
    member_n    INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_articles USING fts5(
    headword, article_text, content=articles, content_rowid=id
);
CREATE INDEX IF NOT EXISTS ix_articles_volume   ON articles(volume);
CREATE INDEX IF NOT EXISTS ix_articles_category ON articles(category);
CREATE INDEX IF NOT EXISTS ix_members_article   ON members(article_id);
"""



# ── Semantic search ─────────────────────────────────────────────────────────
#
# Mirrors kf_mcp. Columns are this corpus's: an HBLS article is cited by
# headword, volume and page, and has no year of its own — the year filter
# therefore ranges over volumes.
EMBEDDING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT    PRIMARY KEY,   -- "<doc_id>#<chunk_index>"
    doc_id    TEXT    NOT NULL,
    chunk_index INTEGER NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    text        TEXT    NOT NULL,      -- the expanded reading, not the raw transcription
    UNIQUE (doc_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT    PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    model    TEXT    NOT NULL,
    dims     INTEGER NOT NULL,
    vector   BLOB    NOT NULL          -- float32, little-endian, L2-normalised
);
CREATE TABLE IF NOT EXISTS embedding_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    model       TEXT NOT NULL,
    dims        INTEGER,
    base_url    TEXT,
    chunk_chars INTEGER,
    chunk_overlap INTEGER,
    n_articles  INTEGER,
    n_chunks    INTEGER,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS ix_chunks_entry ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS ix_embeddings_model ON embeddings(model);
"""

_VECTOR_CACHE: dict = {}

_SEMANTIC_SQL = (
    "SELECT c.chunk_id,c.doc_id,c.chunk_index,c.char_start,c.char_end,c.text,"
    "e.headword,e.volume,e.page "
    "FROM chunks c JOIN articles e ON e.id=c.doc_id "
    "WHERE c.chunk_id IN ({placeholders})"
)

def _load_matrix(model):
    """(chunk_ids, matrix) for a model, loaded once and cached."""
    key = (_DB_PATH, model)
    cached = _VECTOR_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "numpy is required for semantic search — pip install numpy") from exc
    with conn() as c:
        rows = c.execute(
            "SELECT chunk_id, dims, vector FROM embeddings WHERE model = ? "
            "ORDER BY chunk_id", (model,)).fetchall()
    if not rows:
        raise RuntimeError(
            f"no embeddings for model {model!r} in {_DB_PATH}. "
            "Run embed_db.py to build the semantic index.")
    dims = rows[0]["dims"]
    chunk_ids = [row["chunk_id"] for row in rows]
    # One contiguous buffer: the difference between a matrix multiply and
    # thousands of small ones.
    buffer = b"".join(row["vector"] for row in rows)
    matrix = np.frombuffer(buffer, dtype="<f4").reshape(len(rows), dims)
    _VECTOR_CACHE[key] = (chunk_ids, matrix)
    return chunk_ids, matrix


def search_semantic(query_vector, limit=20, model=None, year_from=None,
                    year_to=None, per_document=2):
    """Passages closest in meaning to an already-embedded query.

    ``per_document`` caps how many passages one article may contribute, so a long
    document cannot fill the result set and crowd out the other articles that
    answer the question. ``year_from``/``year_to`` restrict to a period, which
    for this corpus is often the point of the question.
    """
    import numpy as np

    limit = clamp(limit, 20)
    model = model or os.environ.get("HBLS_EMBED_MODEL", "qwen3-embedding-0.6b")
    chunk_ids, matrix = _load_matrix(model)

    query = np.asarray(query_vector, dtype="float32")
    if query.shape[0] != matrix.shape[1]:
        raise ValueError(
            f"query has {query.shape[0]} dimensions, index has {matrix.shape[1]}")
    norm = float(np.linalg.norm(query)) or 1.0
    scores = matrix @ (query / norm)

    # Take a generous slice before filtering: the year filter and the per-entry
    # cap both discard candidates.
    fetch = min(len(chunk_ids), max(limit * 8, limit + 50))
    candidates = np.argpartition(-scores, fetch - 1)[:fetch]
    candidates = candidates[np.argsort(-scores[candidates])]
    picked = [(chunk_ids[i], float(scores[i])) for i in candidates]

    by_id = {}
    with conn() as c:
        for start in range(0, len(picked), 400):
            window = picked[start:start + 400]
            sql = _SEMANTIC_SQL.format(placeholders=",".join("?" * len(window)))
            for row in c.execute(sql, [cid for cid, _ in window]).fetchall():
                by_id[row["chunk_id"]] = row

    out, seen = [], {}
    for chunk_id, score in picked:
        row = by_id.get(chunk_id)
        if row is None:
            continue                      # vector outlived its chunk
        year = row["volume"]
        if year_from is not None and (year is None or year < year_from):
            continue
        if year_to is not None and (year is None or year > year_to):
            continue
        if seen.get(row["doc_id"], 0) >= per_document:
            continue
        seen[row["doc_id"]] = seen.get(row["doc_id"], 0) + 1
        out.append({
            "id": row["doc_id"],
            "chunk_id": chunk_id,
            "headword": row["headword"],
            "page": row["page"],
            "year": year,
            "snippet": row["text"],
            "score": round(score, 4),
            "chunk_index": row["chunk_index"],
            "char_start": row["char_start"],
            "char_end": row["char_end"],
        })
        if len(out) >= limit:
            break
    return out

def semantic_stats(model=None):
    """Coverage of the semantic index, and the runs that produced it."""
    with conn() as c:
        if not c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                         "AND name='embeddings'").fetchone():
            return {"indexed": False,
                    "reason": "no embeddings table; run embed_db.py"}
        by_model = c.execute(
            "SELECT model, COUNT(*) n, MAX(dims) d FROM embeddings GROUP BY model"
        ).fetchall()
        n_chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_total = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        n_indexed = c.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM chunks").fetchone()[0]
        runs = c.execute(
            "SELECT run_id, started_at, finished_at, model, dims, n_chunks, notes "
            "FROM embedding_runs ORDER BY started_at DESC LIMIT 5").fetchall()
    return {
        "indexed": bool(by_model),
        "n_chunks": n_chunks,
        "n_entries_indexed": n_indexed,
        "n_entries_total": n_total,
        "coverage": round(n_indexed / n_total, 4) if n_total else 0.0,
        "models": [{"model": m["model"], "n_vectors": m["n"], "dims": m["d"]}
                   for m in by_model],
        "recent_runs": _rows(runs),
    }




def warm_semantic_index(model):
    """Load the vectors now and report what was loaded, or why it could not be."""
    try:
        chunk_ids, matrix = _load_matrix(model)
    except RuntimeError as exc:
        return {"ready": False, "model": model, "reason": str(exc)}
    return {"ready": True, "model": model, "n_chunks": len(chunk_ids),
            "dims": int(matrix.shape[1]),
            "megabytes": round(matrix.nbytes / 1_048_576, 1)}


def set_db_path(path: str):
    global _DB_PATH
    _DB_PATH = path


@contextmanager
def conn():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    try:
        yield con
    finally:
        con.close()


def _row(r) -> dict | None:
    return dict(r) if r else None


def _rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


def clamp(limit, default, cap=MAX_LIMIT) -> int:
    """Constrain a caller-supplied limit. SQLite reads LIMIT -1 as unbounded, so an
    unchecked negative value would return the whole table — and min(limit, 200),
    which this module used before, passes every negative straight through."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return min(n, cap) if n >= 1 else default


def clamp_offset(offset) -> int:
    try:
        return max(int(offset), 0)
    except (TypeError, ValueError):
        return 0


def like_pattern(query) -> str:
    """Substring pattern for LIKE, with the wildcards escaped so a query of '%' or
    '_' matches those characters literally instead of the whole table. Pairs with
    ESCAPE '\\' in the SQL."""
    escaped = (query or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def quote_fts(query: str) -> str:
    """Rewrite a query as quoted FTS5 phrases, one per word (implicit AND).
    Strips the characters FTS5 treats as syntax so no input can be a syntax error."""
    tokens = [t for t in re.split(r'\s+', re.sub(r'["\*\(\):^-]', ' ', query or "")) if t]
    return ' '.join(f'"{t}"' for t in tokens)


# ── Stats ─────────────────────────────────────────────────────────────────────

def db_stats() -> dict[str, Any]:
    with conn() as c:
        n_art     = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        n_mem     = c.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        n_typed   = c.execute(
            "SELECT COUNT(*) FROM members WHERE given IS NOT NULL").fetchone()[0]
        vol_cnts  = c.execute(
            "SELECT volume, COUNT(*) FROM articles WHERE volume IS NOT NULL "
            "GROUP BY volume ORDER BY volume").fetchall()
        txt_chars = c.execute(
            "SELECT SUM(LENGTH(article_text)) FROM articles").fetchone()[0] or 0
        return {
            "n_articles":      n_art,
            "n_members":       n_mem,
            "n_members_named": n_typed,
            "text_chars":      txt_chars,
            "text_mb":         round(txt_chars / 1e6, 1),
            "volumes":         dict(vol_cnts),
        }


# ── Article queries ───────────────────────────────────────────────────────────

# Note the absence of a.article_text: a result set of 20 full articles ran to
# hundreds of kilobytes, past the ~150k-character limit Claude.ai and Claude Desktop
# apply to a tool result. The snippet locates the hit; get_article returns the text.
_FTS_SQL = """
    SELECT a.id, a.headword, a.volume, a.page,
           snippet(fts_articles, 1, '<b>', '</b>', '…', 30) AS snippet,
           a.pdf_url,
           (SELECT COUNT(*) FROM members m WHERE m.article_id = a.id) AS n_members
    FROM fts_articles f
    JOIN articles a ON a.id = f.rowid
    WHERE fts_articles MATCH ?
    ORDER BY rank
    LIMIT ?
"""

_LIKE_SQL = """
    SELECT a.id, a.headword, a.volume, a.page, a.snippet, a.pdf_url,
           (SELECT COUNT(*) FROM members m WHERE m.article_id = a.id) AS n_members
    FROM articles a
    WHERE a.headword LIKE ? ESCAPE '\\'
    LIMIT ?
"""


def search_articles(query: str, limit: int = 20) -> list[dict]:
    """Full-text search. Honours FTS5 operators (OR, NEAR, prefix*) when the query is
    well formed, falls back to quoted phrases, and finally to a literal headword
    search, rather than raising at the caller."""
    limit = clamp(limit, 20)
    if not query or not query.strip():
        return [{"error": "Empty query."}]
    with conn() as c:
        for q in (query, quote_fts(query)):
            if not q:
                continue
            try:
                return _rows(c.execute(_FTS_SQL, (q, limit)).fetchall())
            except sqlite3.OperationalError:
                continue
        return _rows(c.execute(_LIKE_SQL, (like_pattern(query), limit)).fetchall())


def get_article(headword: str, volume: int) -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM articles WHERE headword=? AND volume=?",
            (headword, volume)).fetchone()
        if not row:
            return None
        result = _row(row)
        result["members"] = _rows(c.execute(
            "SELECT given, birth_year, death_year, member_n "
            "FROM members WHERE article_id=? ORDER BY member_n",
            (result["id"],)).fetchall())
        return result


def get_article_by_id(article_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        if not row:
            return None
        result = _row(row)
        result["members"] = _rows(c.execute(
            "SELECT given, birth_year, death_year, member_n "
            "FROM members WHERE article_id=? ORDER BY member_n",
            (result["id"],)).fetchall())
        return result


def get_article_by_page(volume: int, page: int) -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM articles WHERE volume=? AND page=?",
            (volume, page)).fetchone()
        if not row:
            return None
        result = _row(row)
        result["members"] = _rows(c.execute(
            "SELECT given, birth_year, death_year, member_n "
            "FROM members WHERE article_id=? ORDER BY member_n",
            (result["id"],)).fetchall())
        return result


def list_volume_articles(volume: int, limit: int = 100, offset: int = 0) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT a.id, a.headword, a.volume, a.page, a.snippet, a.pdf_url,
                   (SELECT COUNT(*) FROM members m WHERE m.article_id = a.id) AS n_members
            FROM articles a
            WHERE a.volume = ?
            ORDER BY a.page
            LIMIT ? OFFSET ?
        """, (volume, clamp(limit, 100), clamp_offset(offset))).fetchall()
    return _rows(rows)


def volume_index(volume: int, limit: int = VOLUME_INDEX_LIMIT) -> dict:
    """Article index for one volume, for the hbls://volume/{volume} resource. Says
    so when it is truncated rather than silently returning a prefix."""
    limit = clamp(limit, VOLUME_INDEX_LIMIT, cap=VOLUME_INDEX_LIMIT)
    with conn() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM articles WHERE volume = ?", (volume,)).fetchone()[0]
        rows = _rows(c.execute("""
            SELECT id, headword, volume, page, pdf_url
            FROM articles WHERE volume = ? ORDER BY page LIMIT ?
        """, (volume, limit)).fetchall())
    out = {"volume": volume, "total": total, "returned": len(rows),
           "truncated": len(rows) < total, "articles": rows}
    if out["truncated"]:
        out["note"] = (f"Showing the first {len(rows)} of {total} articles in volume "
                       f"{volume}. Use list_volume(volume, limit, offset) for the rest.")
    return out


# ── Member/person queries ──────────────────────────────────────────────────────

def get_family_members(headword: str, volume: int) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT m.given, m.birth_year, m.death_year, m.member_n,
                   a.headword, a.volume, a.page, a.pdf_url
            FROM members m
            JOIN articles a ON a.id = m.article_id
            WHERE a.headword=? AND a.volume=?
            ORDER BY m.member_n
            LIMIT ?
        """, (headword, volume, MAX_LIMIT)).fetchall()
    return _rows(rows)


def search_persons(query: str, limit: int = 50) -> list[dict]:
    """Search all persons by forename OR family name (headword)."""
    with conn() as c:
        rows = c.execute("""
            SELECT m.given          AS forename,
                   m.birth_year,
                   m.death_year,
                   m.member_n,
                   a.headword       AS family_name,
                   a.volume,
                   a.page,
                   a.pdf_url,
                   a.category
            FROM members m
            JOIN articles a ON a.id = m.article_id
            WHERE m.given LIKE ?1 ESCAPE '\\' OR a.headword LIKE ?1 ESCAPE '\\'
            ORDER BY a.headword, m.given
            LIMIT ?2
        """, (like_pattern(query), clamp(limit, 50))).fetchall()
    return _rows(rows)


def search_bio(query: str, limit: int = 50) -> list[dict]:
    """Search persons in bio articles only — family name + forename."""
    with conn() as c:
        rows = c.execute("""
            SELECT m.given          AS forename,
                   m.birth_year,
                   m.death_year,
                   a.headword       AS family_name,
                   a.volume,
                   a.page,
                   a.pdf_url,
                   a.lexical_class
            FROM members m
            JOIN articles a ON a.id = m.article_id
            WHERE a.category = 'bio'
              AND (a.headword LIKE ?1 ESCAPE '\\' OR m.given LIKE ?1 ESCAPE '\\')
            ORDER BY a.headword, m.birth_year
            LIMIT ?2
        """, (like_pattern(query), clamp(limit, 50))).fetchall()
    return _rows(rows)


# ── PDF helpers ────────────────────────────────────────────────────────────────

def get_pdf_url(headword: str, volume: int, page: int) -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT pdf_url, volume, page FROM articles "
            "WHERE headword=? AND volume=? AND page=?",
            (headword, volume, page)).fetchone()
        return dict(row) if row else {}


# ── Category queries ──────────────────────────────────────────────────────────

def get_articles_by_category(
    category: str, volume: int | None = None, limit: int = 100, offset: int = 0
) -> list[dict]:
    """
    List articles filtered by category.
    category: one of fam, bio, geo, tem
    volume: optional filter to a specific volume
    """
    with conn() as c:
        if volume is not None:
            rows = c.execute("""
                SELECT a.id, a.headword, a.volume, a.page, a.snippet, a.pdf_url,
                       a.category, a.lexical_class,
                       (SELECT COUNT(*) FROM members m WHERE m.article_id = a.id) AS n_members
                FROM articles a
                WHERE a.category = ? AND a.volume = ?
                ORDER BY a.page
                LIMIT ? OFFSET ?
            """, (category, volume, clamp(limit, 100), clamp_offset(offset))).fetchall()
        else:
            rows = c.execute("""
                SELECT a.id, a.headword, a.volume, a.page, a.snippet, a.pdf_url,
                       a.category, a.lexical_class,
                       (SELECT COUNT(*) FROM members m WHERE m.article_id = a.id) AS n_members
                FROM articles a
                WHERE a.category = ?
                ORDER BY a.volume, a.page
                LIMIT ? OFFSET ?
            """, (category, clamp(limit, 100), clamp_offset(offset))).fetchall()
    return _rows(rows)


def get_category_stats(volume: int | None = None) -> dict:
    """Return article counts by category, optionally filtered by volume."""
    with conn() as c:
        if volume is not None:
            rows = c.execute("""
                SELECT category, COUNT(*) as n
                FROM articles
                WHERE volume = ?
                GROUP BY category
                ORDER BY n DESC
            """, (volume,)).fetchall()
            total = c.execute("SELECT COUNT(*) FROM articles WHERE volume = ?", (volume,)).fetchone()[0]
        else:
            rows = c.execute("""
                SELECT category, COUNT(*) as n
                FROM articles
                GROUP BY category
                ORDER BY n DESC
            """).fetchall()
            total = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    cats = dict(rows)
    cats["_total"] = total
    return cats
