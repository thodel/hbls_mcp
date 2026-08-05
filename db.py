"""
db.py — SQLite query helpers for HBLS MCP server.
"""

import sqlite3
from contextlib import contextmanager
from typing import Any

_DB_PATH = "/home/dh/hbls_mcp/hbls.db"


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

def search_articles(query: str, limit: int = 20) -> list[dict]:
    with conn() as c:
        try:
            rows = c.execute("""
                SELECT a.id, a.headword, a.volume, a.page,
                       snippet(fts_articles, 1, '<b>', '</b>', '…', 30) AS snippet,
                       a.article_text, a.pdf_url,
                       (SELECT COUNT(*) FROM members m WHERE m.article_id = a.id) AS n_members
                FROM fts_articles f
                JOIN articles a ON a.rowid = f.rowid
                WHERE fts_articles MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (query, limit)).fetchall()
        except Exception:
            rows = c.execute("""
                SELECT a.id, a.headword, a.volume, a.page, a.snippet,
                       a.article_text, a.pdf_url,
                       (SELECT COUNT(*) FROM members m WHERE m.article_id = a.id) AS n_members
                FROM articles a
                WHERE a.headword LIKE ?
                LIMIT ?
            """, (f"%{query}%", limit)).fetchall()
    return _rows(rows)


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
        """, (volume, limit, offset)).fetchall()
    return _rows(rows)


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
        """, (headword, volume)).fetchall()
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
            WHERE m.given LIKE ? OR a.headword LIKE ?
            ORDER BY a.headword, m.given
            LIMIT ?
        """, (f"%{query}%", f"%{query}%", min(limit, 200))).fetchall()
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
              AND (a.headword LIKE ? OR m.given LIKE ?)
            ORDER BY a.headword, m.birth_year
            LIMIT ?
        """, (f"%{query}%", f"%{query}%", min(limit, 200))).fetchall()
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
            """, (category, volume, limit, offset)).fetchall()
        else:
            rows = c.execute("""
                SELECT a.id, a.headword, a.volume, a.page, a.snippet, a.pdf_url,
                       a.category, a.lexical_class,
                       (SELECT COUNT(*) FROM members m WHERE m.article_id = a.id) AS n_members
                FROM articles a
                WHERE a.category = ?
                ORDER BY a.volume, a.page
                LIMIT ? OFFSET ?
            """, (category, limit, offset)).fetchall()
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
