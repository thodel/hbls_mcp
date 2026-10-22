"""
db.py — SQLite query helpers for HBLS MCP server.
"""

# db.py already used 3.10-only annotations (dict | None), so the module
# could not be imported on 3.9 at all — including by its own tests.
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from typing import Any

import corpus          # sets MCP_ENV_PREFIX=HBLS before embeddings is imported
import vectorstore

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
# The vector schema, the matrix and the search are vectorstore.py, shared across
# the fleet (thodel/hbls_mcp#9). What is this corpus's is here: how a chunk is
# joined to its article, and what a hit looks like. An HBLS article is cited by
# headword, volume and page, and has no year of its own — the year filter
# therefore ranges over volumes.

JOIN_SQL = (
    "SELECT c.chunk_id,c.parent_id,c.chunk_index,c.char_start,c.char_end,c.text,"
    "e.headword,e.volume,e.page,e.pdf_url "
    "FROM chunks c JOIN articles e ON e.id=c.parent_id "
    "WHERE c.chunk_id IN ({placeholders})"
)


def passage_body(text: str, headword, volume) -> str:
    """The passage without its head line (fleet rule, thodel/ch-h-bot#555).

    Every passage carries `headword volume` on its first line — for the
    vector, which otherwise has no idea which article a window from the
    middle of a long entry belongs to. The reader does not need it there: the
    hit carries headword, volume and page as fields, and a snippet that
    starts with them repeats the citation line above it, puts the head
    instead of the text into a `#:~:text=` link and hides a half-word the
    window may start with (thodel/eperiodica_mcp#10). Only a text that
    starts with exactly this article's head is cut, built by the same
    corpus.head_line that wrote it; a passage without one is untouched.
    """
    head = corpus.head_line(headword, volume)
    if head and text.startswith(head + "\n"):
        return text[len(head) + 1:]
    return text


def row_to_hit(row, chunk_id: str, score: float, year_from=None, year_to=None):
    """One joined row (JOIN_SQL's columns) as a result dict, or None to drop it."""
    volume = row["volume"]
    # NOTE: year_from/year_to have always filtered on the volume number,
    # which for this corpus is 1-8 and not a year at all. Left as it was —
    # changing what a parameter means is not this change's business — but
    # it is why the field below is not called "year".
    if year_from is not None and (volume is None or volume < year_from):
        return None
    if year_to is not None and (volume is None or volume > year_to):
        return None
    return {
        "id": row["parent_id"],
        "chunk_id": chunk_id,
        "headword": row["headword"],
        # An HBLS article is cited by headword, volume and page, and the
        # PDF is the only place a reader can check it. A semantic hit that
        # carries none of that is a passage nobody can follow — so it ships
        # the same citation fields the keyword search has always returned.
        "volume": volume,
        "page": row["page"],
        "pdf_url": row["pdf_url"],
        "snippet": passage_body(row["text"], row["headword"], volume),
        "score": score,
        "chunk_index": row["chunk_index"],
        # Offsets count in the cleaned article text and know nothing of the
        # head line, so cutting it leaves them untouched.
        "char_start": row["char_start"],
        "char_end": row["char_end"],
    }


def search_semantic(query_vector, limit=20, model=None, year_from=None,
                    year_to=None, per_parent=2):
    """Passages closest in meaning to an already-embedded query.

    ``per_parent`` caps how many passages one article may contribute, so a long
    article cannot fill the result set and crowd out the others that answer
    the question. ``year_from``/``year_to`` restrict the volume (see the NOTE
    in row_to_hit).
    """
    return vectorstore.search(
        conn, _DB_PATH, query_vector,
        join_sql=JOIN_SQL,
        row_to_hit=lambda r, cid, sc: row_to_hit(r, cid, sc, year_from, year_to),
        limit=clamp(limit, 20), model=model, per_parent=per_parent)


def semantic_stats():
    """Coverage of the semantic index, and the runs that produced it."""
    return vectorstore.stats(conn, corpus.PARENT_TABLE)


def warm_semantic_index(model):
    """Load the vectors now and report what was loaded, or why it could not be."""
    return vectorstore.warm(conn, _DB_PATH, model)


def set_db_path(path: str):
    global _DB_PATH
    _DB_PATH = path
    vectorstore.clear_cache()


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
