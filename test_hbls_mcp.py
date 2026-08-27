#!/usr/bin/env python3
"""
test_hbls_mcp.py — test suite for the HBLS MCP server.

Runs two ways. Under pytest, failures fail the run; the CLI keeps the grouped
output and sets the exit code.

    pytest test_hbls_mcp.py                                     # unit tests only
    HBLS_DB=/data/hbls.db pytest test_hbls_mcp.py               # + DB tests
    HBLS_SERVER=http://localhost:8003 pytest test_hbls_mcp.py   # + server tests

    python test_hbls_mcp.py --unit
    python test_hbls_mcp.py --db /data/hbls.db --server http://localhost:8003

Unit tests build their own throwaway database, so they need no setup. Tests
needing the real DB or a live server skip when it isn't configured.

Requires Python 3.10+ (db.py and server.py use `X | None` annotations); the
container image is python:3.12-slim.
"""
import argparse, json, os, sqlite3, sys, tempfile
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Helpers ───────────────────────────────────────────────────────────────────

RED   = "\033[91m"
GREEN = "\033[92m"
YELLOW= "\033[93m"
RESET = "\033[0m"

def ok(msg):   print(f"{GREEN}✅ {msg}{RESET}")
def fail(msg): print(f"{RED}❌ {msg}{RESET}")
def warn(msg): print(f"{YELLOW}⚠️  {msg}{RESET}")
def info(msg): print(f"   {msg}")

class Checks:
    """Collects several checks so one run reports them all, then fails as a unit.

    Not named Test* — pytest would try to collect it as a test class.
    """
    def __init__(self): self.passed = self.failed = 0; self.failures = []
    def check(self, cond, msg):
        if cond:
            self.passed += 1
            ok(msg)
        else:
            self.failed += 1
            self.failures.append(msg)
            fail(msg)
    def assert_ok(self):
        total = self.passed + self.failed
        print(f"\n{'─'*50}")
        print(f"Ran {total} checks: {GREEN}{self.passed} passed{RESET}", end="")
        if self.failed: print(f", {RED}{self.failed} failed{RESET}", end="")
        print()
        if self.failed:
            raise AssertionError(
                f"{self.failed} of {total} checks failed:\n  - " + "\n  - ".join(self.failures))


@pytest.fixture
def db_path():
    return os.environ.get("HBLS_DB", "")

@pytest.fixture
def base_url():
    return os.environ.get("HBLS_SERVER", "")


# ── Fixture database ──────────────────────────────────────────────────────────

# id, headword, volume, page, snippet, article_text, pdf_url, category, lexical_class
ARTICLES = [
    (1, "ZWINGLI", 7, 712, "Zürcher Reformatorenfamilie", "Zwingli, Ulrich, Reformator …",
     "https://example.org/v7p712.pdf", "fam", "Familie"),
    (2, "BRUGG", 2, 380, "Stadt im Aargau", "Brugg, Stadt und Bezirk im Kanton Aargau …",
     "https://example.org/v2p380.pdf", "geo", "Stadt"),
    (3, "SICHER 100%", 6, 120, "Testartikel", "Ein Artikel mit Prozentzeichen im Stichwort.",
     "https://example.org/v6p120.pdf", "bio", "Test"),
    (4, "HANS_MEIER", 6, 121, "Testartikel", "Ein Artikel mit Unterstrich im Stichwort.",
     "https://example.org/v6p121.pdf", "bio", "Test"),
]

# id, article_id, given, birth_year, death_year, member_n
MEMBERS = [
    (1, 1, "Ulrich", 1484, 1531, 1),
    (2, 1, "Huldrych", 1502, 1556, 2),
    (3, 3, "Anna 100%", 1600, 1650, 1),
    (4, 4, "Hans_", 1700, 1750, 1),
]


def make_fixture_db(path):
    import db as db_module
    con = sqlite3.connect(path)
    con.executescript(db_module.SCHEMA_SQL)
    con.executemany(f"INSERT INTO articles VALUES ({','.join('?' * 9)})", ARTICLES)
    con.executemany(f"INSERT INTO members VALUES ({','.join('?' * 6)})", MEMBERS)
    con.execute("INSERT INTO fts_articles(rowid, headword, article_text) "
                "SELECT id, headword, article_text FROM articles")
    con.commit(); con.close()
    db_module.set_db_path(path)
    return db_module


# ── 1. Unit tests ─────────────────────────────────────────────────────────────

def test_limits_are_clamped():
    """min(limit, 200) — what this module used before — passes every negative
    straight through, and SQLite reads LIMIT -1 as unbounded."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hbls.db")
        tr.check(db.clamp(-1, 50) == 50, "negative limit falls back to the default")
        tr.check(min(-1, 200) == -1, "the old min(limit, 200) guard let -1 through")
        tr.check(db.clamp(0, 50) == 50, "zero limit falls back to the default")
        tr.check(db.clamp("many", 50) == 50, "non-numeric limit falls back to the default")
        tr.check(db.clamp(10**9, 50) == db.MAX_LIMIT, f"huge limit capped at {db.MAX_LIMIT}")
        tr.check(db.clamp_offset(-3) == 0, "negative offset clamps to 0")

        tr.check(len(db.list_volume_articles(6, -1)) == 2, "list_volume_articles(-1) is bounded")
        tr.check(len(db.search_persons("a", limit=-1)) <= db.MAX_LIMIT,
                 "search_persons(-1) is bounded")
        tr.check(len(db.get_articles_by_category("bio", None, -1)) <= db.MAX_LIMIT,
                 "get_articles_by_category(-1) is bounded")
        first, second = db.list_volume_articles(6, 1, 0), db.list_volume_articles(6, 1, 1)
        tr.check(first[0]["id"] != second[0]["id"], "paging does not repeat a row")
    tr.assert_ok()


def test_like_wildcards_are_escaped():
    """A '%' or '_' in a search query must match itself, not act as a wildcard."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hbls.db")
        fams = lambda rows: sorted({r["family_name"] for r in rows})

        tr.check(fams(db.search_persons("%")) == ["SICHER 100%"],
                 "'%' matches a literal percent, not every row")
        tr.check(fams(db.search_persons("_")) == ["HANS_MEIER"],
                 "'_' matches a literal underscore, not any character")
        tr.check(db.search_persons("%ZWINGLI%") == [],
                 "caller-supplied wildcards do not expand")
        tr.check(fams(db.search_persons("Ulrich")) == ["ZWINGLI"],
                 "ordinary substring search still works")
        tr.check(db.search_bio("%")[0]["family_name"] == "SICHER 100%",
                 "search_bio escapes wildcards too")
        tr.check(db.like_pattern("a%b_c") == "%a\\%b\\_c%", "like_pattern escapes both wildcards")
    tr.assert_ok()


def test_fulltext_survives_hostile_queries():
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hbls.db")
        hits = db.search_articles("Reformator")
        tr.check(hits and hits[0]["headword"] == "ZWINGLI",
                 f"plain query finds the article ({hits[:1]})")
        for q in ['Zwingli"', "Brugg AND", "(unbalanced", "Refor*", "%"]:
            try:
                tr.check(isinstance(db.search_articles(q, 5), list),
                         f"search_articles({q!r}) returned a list")
            except Exception as e:
                tr.check(False, f"search_articles({q!r}) raised {type(e).__name__}: {e}")
        tr.check("error" in db.search_articles("")[0], "an empty query is reported as an error")
    tr.assert_ok()


def test_search_results_omit_full_article_text():
    """20 full articles blew past the ~150k-character result limit; the snippet
    locates the hit and get_article returns the text."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hbls.db")
        hits = db.search_articles("Reformator")
        tr.check(hits and "article_text" not in hits[0],
                 f"search results carry no article_text (keys: {sorted(hits[0]) if hits else '—'})")
        tr.check("snippet" in hits[0], "search results still carry a snippet")
        full = db.get_article("ZWINGLI", 7)
        tr.check(full and full.get("article_text"), "get_article still returns the full text")
        tr.check(len(full["members"]) == 2, "get_article attaches the family members")
    tr.assert_ok()


def test_volume_index_reports_truncation():
    """hbls://volume/{volume} must say when it is only showing a prefix."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hbls.db")
        full = db.volume_index(6)
        tr.check(full["total"] == 2 and full["returned"] == 2, "full index returns everything")
        tr.check(full["truncated"] is False, "full index is not flagged truncated")
        tr.check("note" not in full, "no truncation note when nothing is cut")

        cut = db.volume_index(6, limit=1)
        tr.check(cut["total"] == 2 and cut["returned"] == 1, "truncated index reports both counts")
        tr.check(cut["truncated"] is True, "truncation is flagged")
        tr.check("list_volume" in cut.get("note", ""), "note points at list_volume")
        tr.check("article_text" not in cut["articles"][0], "the index carries no article text")
    tr.assert_ok()


def test_connection_is_read_only():
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hbls.db")
        with db.conn() as c:
            try:
                c.execute("DELETE FROM articles")
                tr.check(False, "a write through db.conn() was accepted")
            except sqlite3.OperationalError as e:
                tr.check(True, f"writes are rejected ({e})")
        tr.check(db.db_stats()["n_articles"] == 4, "corpus is intact after the attempted write")
    tr.assert_ok()


def test_server_module_registers_tools():
    """server.py must import without reading sys.argv — it used to call
    parse_args() at import time, which hijacks the arguments of any process that
    imports it, pytest included."""
    pytest.importorskip("mcp", reason="mcp SDK not installed")
    import anyio
    import server as server_module

    tr = Checks()
    expected = {"corpus_stats", "search", "get_article", "get_article_by_page", "list_volume",
                "get_family_members", "search_persons", "search_bio", "get_pdf_url",
                "get_articles_by_category", "get_category_stats"}
    names = {t.name for t in anyio.run(server_module.mcp.list_tools)}
    tr.check(not expected - names, f"all tools registered (missing: {sorted(expected - names)})")

    n = server_module.normalise_path
    tr.check(n("/mcp/hbls/mcp") == "/mcp/hbls/mcp", "an already-correct path is unchanged")
    tr.check(n("mcp/hbls/mcp/") == "/mcp/hbls/mcp", "slashes are normalised")
    tr.check(n("") == "/mcp" and n(None) == "/mcp", "an empty path falls back to /mcp")
    tr.check(server_module.parse_args([]).http_path == "/mcp", "default endpoint path is /mcp")
    tr.check(server_module.parse_args(["--http-path", "mcp/hbls/mcp/"]).http_path
             == "/mcp/hbls/mcp", "--http-path is normalised on the way in")

    bad = server_module.get_articles_by_category("nonsense")
    tr.check(isinstance(bad, list) and "error" in bad[0],
             f"a bad category returns a list, matching the declared schema (got {bad!r})")
    tr.assert_ok()


# ── 2. DB tests — against the real corpus ─────────────────────────────────────

def test_db_layer_against_real_db(db_path):
    if not db_path or not os.path.exists(db_path):
        pytest.skip(f"hbls.db not found at {db_path!r} — set HBLS_DB or pass --db")

    import db as db_module
    db_module.set_db_path(db_path)
    tr = Checks()

    s = db_module.db_stats()
    info(f"articles={s['n_articles']} members={s['n_members']} text={s['text_mb']} MB")
    tr.check(s["n_articles"] > 10000, f"articles: >10000 (got {s['n_articles']})")
    tr.check(len(db_module.list_volume_articles(1, -1)) <= db_module.MAX_LIMIT,
             "list_volume_articles(-1) is bounded")

    for q in ["Zwingli", "%", "_", 'quote"mark', "Bern AND"]:
        try:
            tr.check(isinstance(db_module.search_articles(q, 5), list),
                     f"search_articles({q!r}) returned a list")
            tr.check(isinstance(db_module.search_persons(q, 5), list),
                     f"search_persons({q!r}) returned a list")
        except Exception as e:
            tr.check(False, f"{q!r} raised {type(e).__name__}: {e}")

    idx = db_module.volume_index(1)
    info(f"volume 1: {idx['returned']} of {idx['total']} articles, truncated={idx['truncated']}")
    tr.check(idx["returned"] <= db_module.VOLUME_INDEX_LIMIT,
             f"volume index capped at {db_module.VOLUME_INDEX_LIMIT}")
    payload = json.dumps(idx, ensure_ascii=False)
    tr.check(len(payload) < 150_000,
             f"volume index stays under the 150k result limit ({len(payload):,} chars)")
    tr.assert_ok()


# ── 3. Server integration test ────────────────────────────────────────────────

def _tool_payload(result):
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured.get("result", structured)
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            try: return json.loads(text)
            except json.JSONDecodeError: return text
    return None


def test_server(base_url):
    """Drive the running server over streamable HTTP using the official client."""
    if not base_url:
        pytest.skip("no server URL — set HBLS_SERVER or pass --server")
    try:
        import anyio
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as e:
        pytest.skip(f"mcp client library not available: {e}")

    tr = Checks()
    url = base_url.rstrip("/")
    if url.rsplit("/", 1)[-1] != "mcp":
        url += "/mcp"

    async def exercise():
        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                names = {t.name for t in (await session.list_tools()).tools}
                tr.check("corpus_stats" in names, f"tools/list returned {len(names)} tools")

                stats = _tool_payload(await session.call_tool("corpus_stats", {}))
                tr.check(isinstance(stats, dict) and stats.get("n_articles", 0) > 0,
                         f"corpus_stats returns articles (got {stats})")

                for tool, args in [
                    ("search",         {"query": "Zwingli", "limit": 3}),
                    ("search_persons", {"query": "Ulrich", "limit": 3}),
                    ("search_bio",     {"query": "Anna", "limit": 3}),
                    ("list_volume",    {"volume": 1, "limit": 3}),
                    ("get_category_stats", {}),
                ]:
                    res = await session.call_tool(tool, args)
                    tr.check(not res.is_error, f"{tool} call succeeded")

                res = await session.call_tool("search", {"query": 'Zwingli"', "limit": 3})
                tr.check(not res.is_error, "search survives an unbalanced quote")

                payload = _tool_payload(await session.call_tool(
                    "get_articles_by_category", {"category": "nonsense"}))
                tr.check(isinstance(payload, list) and "error" in payload[0],
                         f"a bad category comes back as data (got {payload})")

                payload = _tool_payload(await session.call_tool(
                    "get_article", {"headword": "definitely_not_a_headword", "volume": 1}))
                tr.check(isinstance(payload, dict) and "error" in payload,
                         f"unknown headword returns an error object (got {payload})")

                resources = {str(r.uri) for r in (await session.list_resources()).resources}
                tr.check("hbls://stats" in resources, f"hbls://stats listed (got {sorted(resources)})")

    anyio.run(exercise)
    tr.assert_ok()


# ── CLI ───────────────────────────────────────────────────────────────────────

def cli_run(label, fn, *fn_args):
    print(f"\n{label}")
    try:
        fn(*fn_args)
        return True
    except pytest.skip.Exception as e:
        warn(f"skipped: {e}")
        return True
    except AssertionError as e:
        fail(str(e))
        return False
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HBLS MCP test suite")
    ap.add_argument("--unit", action="store_true", help="Run unit tests")
    ap.add_argument("--db", default=os.environ.get("HBLS_DB", ""), help="Path to hbls.db")
    ap.add_argument("--server", default=os.environ.get("HBLS_SERVER", ""), help="Server URL")
    args = ap.parse_args()

    if not args.unit and not args.db and not args.server:
        ap.print_help(); sys.exit(0)

    print(f"{'═'*50}\nHBLS MCP test suite\n{'═'*50}")
    ok_all = True

    if args.unit:
        ok_all &= cli_run("[1] Unit: limits are clamped", test_limits_are_clamped)
        ok_all &= cli_run("[2] Unit: LIKE wildcards are escaped", test_like_wildcards_are_escaped)
        ok_all &= cli_run("[3] Unit: full-text survives hostile queries",
                          test_fulltext_survives_hostile_queries)
        ok_all &= cli_run("[4] Unit: search omits full article text",
                          test_search_results_omit_full_article_text)
        ok_all &= cli_run("[5] Unit: volume index reports truncation",
                          test_volume_index_reports_truncation)
        ok_all &= cli_run("[6] Unit: connection is read-only", test_connection_is_read_only)
        ok_all &= cli_run("[7] Unit: server registers its tools", test_server_module_registers_tools)

    if args.db:
        ok_all &= cli_run(f"[8] DB: query layer ({args.db})", test_db_layer_against_real_db, args.db)

    if args.server:
        ok_all &= cli_run(f"[9] Server: MCP integration ({args.server})", test_server, args.server)

    print(f"\n{'═'*50}")
    print(f"{GREEN}ALL PASSED{RESET}" if ok_all else f"{RED}FAILURES{RESET}")
    sys.exit(0 if ok_all else 1)


# ── Semantic search ───────────────────────────────────────────────────────────
# Written before this shipped. The same port into ssrq_mcp produced four
# failures in a row, each reaching the caller as an identical opaque
# "Error executing tool search_semantic": a module missing from the image, a
# missing module constant, another corpus's columns in the SQL, and a row key
# that did not match the alias. One test over a real database catches all four.

def _semantic_db(tmp_path):
    import sqlite3
    import struct

    import db

    path = tmp_path / "semantic.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_SQL)
    conn.executescript(db.EMBEDDING_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO articles (id, headword, volume, page, snippet, article_text, "
        "pdf_url, category, lexical_class) VALUES "
        "(1,'GONTENSCHWIL',3,86,'','Dorf im Kanton Aargau, Bezirk Kulm.',"
        "'http://x','geo','place')")
    conn.execute(
        "INSERT INTO chunks (chunk_id, doc_id, chunk_index, char_start, char_end, text) "
        "VALUES ('1#0','1',0,0,35,'Dorf im Kanton Aargau, Bezirk Kulm.')")
    dims = 8
    vector = [1.0] + [0.0] * (dims - 1)
    conn.execute(
        "INSERT INTO embeddings (chunk_id, model, dims, vector) VALUES (?,?,?,?)",
        ("1#0", "test-model", dims, struct.pack(f"<{dims}f", *vector)))
    conn.commit(); conn.close()
    db.set_db_path(str(path))
    db._VECTOR_CACHE.clear()
    return db, vector


def test_search_semantic_end_to_end(tmp_path):
    db, vector = _semantic_db(tmp_path)

    hits = db.search_semantic(vector, limit=5, model="test-model")

    assert len(hits) == 1
    hit = hits[0]
    # An HBLS article is cited by headword, volume and page.
    assert hit["headword"] == "GONTENSCHWIL"
    assert hit["page"] == 86
    assert hit["score"] > 0.99


def test_semantic_stats_reports_coverage(tmp_path):
    db, _ = _semantic_db(tmp_path)

    stats = db.semantic_stats()

    assert stats["n_chunks"] == 1 and stats["indexed"] is True


def test_every_sql_statement_matches_the_schema(tmp_path):
    import sqlite3

    import db

    conn = sqlite3.connect(tmp_path / "schema.db")
    conn.executescript(db.SCHEMA_SQL)
    conn.executescript(db.EMBEDDING_SCHEMA_SQL)
    conn.execute(db._SEMANTIC_SQL.format(placeholders="?"), ("x",)).fetchall()
    conn.close()


def test_every_module_is_copied_into_the_image():
    """hls_mcp shipped without embeddings.py and crash-looped on the import."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent
    copy_lines = [l for l in (root / "Dockerfile").read_text(encoding="utf-8").splitlines()
                  if l.strip().startswith("COPY")]
    copied = set(re.findall(r"([\w]+\.py)", " ".join(copy_lines)))
    shipped = {p.name for p in root.glob("*.py")
               if not p.name.startswith("test_") and p.name != "embed_db.py"}
    assert not shipped - copied, f"not COPYed into the image: {sorted(shipped - copied)}"
