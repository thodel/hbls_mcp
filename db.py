"""
db.py — SQLite query helpers for HBLS MCP server.
"""

# db.py already used 3.10-only annotations (dict | None), so the module
# could not be imported on 3.9 at all — including by its own tests.
from __future__ import annotations

import json
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


# ── Columns the deployed database may lack ───────────────────────────────────
#
# SCHEMA_SQL declares articles.category (fam | bio | geo | tem) and
# articles.lexical_class. The database on idhefix was built before either was
# added and has neither (checked 19.11.2026: id, headword, volume, page,
# snippet, article_text, pdf_url). #5 took them out of the semantic query; four
# more queries still named them and failed on every call with
# «no such column: category»: search_persons — which the Swiss History Bot
# uses —, search_bio, get_articles_by_category and get_category_stats. The data
# cannot be recovered from what is deployed, so the queries ask which columns
# are there: search_persons returns category as null, and the three category
# tools say plainly that this database carries no categories.

NO_CATEGORIES = (
    "This HBLS database carries no article categories (fam, bio, geo, tem): it "
    "was built before they were added. Use search_persons or search instead."
)


def _article_columns(c) -> set[str]:
    return {r[1] for r in c.execute("PRAGMA table_info(articles)")}


def _col(columns: set[str], name: str) -> str:
    """`a.<name>`, or NULL under the same name when the column is absent."""
    return f"a.{name}" if name in columns else f"NULL AS {name}"


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
            **_meta(c),
        }


def _tables(c) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _member_columns(c) -> set[str]:
    return {r[1] for r in c.execute("PRAGMA table_info(members)")}


def _meta(c) -> dict[str, Any]:
    """Data version and build facts of a rebuilt database (rebuild/build.py); the database built
    from the DigiBern text layer has no meta table and reports itself as such."""
    if "meta" not in _tables(c):
        return {"data_version": "2025.07-textlayer",
                "data_note": "Built from the DigiBern text layer: letter-spaced names, missing members."}
    out = {r[0]: r[1] for r in c.execute("SELECT key, value FROM meta")}
    if "counts" in out:
        try:
            out["counts"] = json.loads(out["counts"])
        except ValueError:
            pass
    return out


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

_MEMBER_EXTRA = ("stable_id", "branch_label", "epithet", "offices", "kin", "source", "review")


def _member_select(c, alias: str = "m") -> str:
    cols = _member_columns(c)
    return ", ".join(f"{alias}.{x}" if x in cols else f"NULL AS {x}" for x in _MEMBER_EXTRA)


def _decode(row: dict) -> dict:
    for k in ("offices", "kin", "review"):
        if isinstance(row.get(k), str):
            try:
                row[k] = json.loads(row[k])
            except ValueError:
                pass
    # the rebuilt columns only where they carry something; the old ones always, in the old shape
    return {k: v for k, v in row.items() if v is not None or k not in _MEMBER_EXTRA}


def get_family_members(headword: str, volume: int) -> list[dict]:
    with conn() as c:
        rows = c.execute(f"""
            SELECT m.given, m.birth_year, m.death_year, m.member_n, {_member_select(c)},
                   a.headword, a.volume, a.page, a.pdf_url
            FROM members m
            JOIN articles a ON a.id = m.article_id
            WHERE a.headword = ? COLLATE NOCASE AND a.volume = ?
            ORDER BY a.id, m.id
            LIMIT ?
        """, (headword, volume, MAX_LIMIT)).fetchall()
    return [_decode(dict(r)) for r in rows]


def get_member(member_id: str) -> dict | None:
    """One member of a rebuilt database by its stable id ("hbls-4-510-KISTLER#u4"), with
    branch, offices, kinship and the passage of the article that is about this person."""
    with conn() as c:
        if "stable_id" not in _member_columns(c):
            return {"error": "This HBLS database has no member ids (built before the rebuild).",
                    "not_found": False}
        r = c.execute(f"""
            SELECT m.given, m.birth_year, m.death_year, m.member_n, {_member_select(c)},
                   m.text_start, m.text_end, a.article_text, a.stable_id AS article_id,
                   a.headword, a.volume, a.page, a.pdf_url,
                   b.name AS branch_name, b.place AS branch_place, b.canton AS branch_canton
            FROM members m
            JOIN articles a ON a.id = m.article_id
            LEFT JOIN branches b ON b.id = m.branch_id
            WHERE m.stable_id = ?
        """, (member_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        text, s, e = d.pop("article_text") or "", d.pop("text_start"), d.pop("text_end")
        if s is not None:
            d["passage"] = text[s:e if e is not None and e - s <= 4000 else s + 4000].strip()
        return _decode(d)


def _name_splits(query: str) -> list[tuple[str, str]]:
    """(forename, family) readings of a query of several words: "Peter Kistler",
    "Kistler Peter", "Kistler, Peter", "Hans Jakob von Erlach"."""
    q = (query or "").strip()
    if "," in q:
        fam, given = (x.strip() for x in q.split(",", 1))
        return [(given, fam)] if given and fam else []
    words = q.split()
    out = []
    for i in range(1, len(words)):
        out.append((" ".join(words[:i]), " ".join(words[i:])))
        out.append((" ".join(words[i:]), " ".join(words[:i])))
    return out


def search_persons(query: str, limit: int = 50) -> list[dict]:
    """Search persons by forename OR family name (headword) — or both: a query of several
    words ("Peter Kistler", "Kistler, Peter") finds the members of that family with that forename."""
    limit = clamp(limit, 50)
    with conn() as c:
        columns = _article_columns(c)
        category = _col(columns, "category")
        select = f"""
            SELECT m.given          AS forename,
                   m.birth_year,
                   m.death_year,
                   m.member_n,
                   a.headword       AS family_name,
                   a.volume,
                   a.page,
                   a.pdf_url,
                   {category}, {_member_select(c)}
            FROM members m
            JOIN articles a ON a.id = m.article_id
        """
        rows = []
        for given, fam in _name_splits(query):
            rows += c.execute(select + """
                WHERE m.given LIKE ? ESCAPE '\\' AND a.headword LIKE ? ESCAPE '\\'
                ORDER BY a.headword, m.id LIMIT ?
            """, (like_pattern(given)[1:], like_pattern(fam.upper())[1:], limit)).fetchall()
        if not rows:
            rows = c.execute(select + """
                WHERE m.given LIKE ? ESCAPE '\\' OR a.headword LIKE ? ESCAPE '\\'
                ORDER BY a.headword, m.given
                LIMIT ?
            """, (like_pattern(query), like_pattern(query), limit)).fetchall()
    seen, out = set(), []
    for r in rows:
        d = _decode(dict(r))
        k = (d["family_name"], d["volume"], d.get("stable_id") or (d["forename"], d["member_n"], d["page"]))
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out[:limit]


def search_bio(query: str, limit: int = 50) -> list[dict]:
    """Search persons in bio articles only — family name + forename."""
    with conn() as c:
        columns = _article_columns(c)
        if "category" not in columns:
            return [{"error": NO_CATEGORIES, "not_found": False}]
        lexical_class = _col(columns, "lexical_class")
        rows = c.execute(f"""
            SELECT m.given          AS forename,
                   m.birth_year,
                   m.death_year,
                   a.headword       AS family_name,
                   a.volume,
                   a.page,
                   a.pdf_url,
                   {lexical_class}
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
        columns = _article_columns(c)
        if "category" not in columns:
            return [{"error": NO_CATEGORIES, "not_found": False}]
        lexical_class = _col(columns, "lexical_class")
        if volume is not None:
            rows = c.execute(f"""
                SELECT a.id, a.headword, a.volume, a.page, a.snippet, a.pdf_url,
                       a.category, {lexical_class},
                       (SELECT COUNT(*) FROM members m WHERE m.article_id = a.id) AS n_members
                FROM articles a
                WHERE a.category = ? AND a.volume = ?
                ORDER BY a.page
                LIMIT ? OFFSET ?
            """, (category, volume, clamp(limit, 100), clamp_offset(offset))).fetchall()
        else:
            rows = c.execute(f"""
                SELECT a.id, a.headword, a.volume, a.page, a.snippet, a.pdf_url,
                       a.category, {lexical_class},
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
        if "category" not in _article_columns(c):
            if volume is not None:
                total = c.execute("SELECT COUNT(*) FROM articles WHERE volume = ?",
                                  (volume,)).fetchone()[0]
            else:
                total = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            return {"error": NO_CATEGORIES, "not_found": False, "_total": total}
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
