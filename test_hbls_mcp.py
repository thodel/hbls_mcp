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
                "get_articles_by_category", "get_category_stats",
                "search_semantic", "semantic_index_stats"}
    tools = {t.name: t for t in anyio.run(server_module.mcp.list_tools)}
    names = set(tools)
    tr.check(not expected - names, f"all tools registered (missing: {sorted(expected - names)})")
    params = set(tools["search_semantic"].input_schema.get("properties", {}))
    tr.check("per_parent" in params and "per_document" not in params,
             f"search_semantic caps per_parent, as the shared vectorstore names it ({sorted(params)})")

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


# ── The vendored embeddings.py ───────────────────────────────────────────────

@pytest.mark.parametrize("entry", ["server", "embed_db"])
def test_the_vendored_embeddings_read_the_hbls_settings(entry):
    # embeddings.py is byte-identical across the fleet and finds HBLS_EMBED_MODEL
    # and the other HBLS_* settings only through MCP_ENV_PREFIX, which corpus.py
    # sets before importing it. Without that, an override would be read by
    # nothing and queries would be embedded with a model the index was not
    # built with.
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "MCP_ENV_PREFIX"}
    env["HBLS_EMBED_MODEL"] = "probe-model"
    out = subprocess.run(
        [sys.executable, "-c",
         # server.py imports corpus inside search_semantic, the first place
         # it needs a vector; this is that order.
         f"import {entry}, corpus, embeddings; "
         "print(embeddings.ENV_PREFIX, embeddings.DEFAULT_MODEL)"],
        cwd=os.path.dirname(os.path.abspath(__file__)), env=env,
        capture_output=True, text=True)
    assert out.stdout.split() == ["HBLS", "probe-model"], out.stderr


def test_queries_carry_this_corpus_instruction():
    # The shared embed_query only knows "Swiss history"; corpus.embed_query
    # keeps the instruction this index has always been queried with.
    import corpus as corpus_module
    seen = {}

    class FakeClient:
        class embeddings:
            @staticmethod
            def create(model, input):
                seen["input"] = input
                return type("R", (), {"data": [type("D", (), {"embedding": [1.0, 0.0]})()]})()

    corpus_module.embed_query("Wer?", client=FakeClient())
    assert seen["input"][0].startswith("Instruct:")
    assert "Historisch-Biographisches Lexikon" in seen["input"][0]
    assert seen["input"][0].endswith("Wer?")


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
    import vectorstore

    path = tmp_path / "semantic.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_SQL)
    conn.executescript(vectorstore.SCHEMA_SQL)
    conn.execute(
        "INSERT INTO articles (id, headword, volume, page, snippet, article_text, "
        "pdf_url, category, lexical_class) VALUES "
        "(1,'GONTENSCHWIL',3,86,'','Dorf im Kanton Aargau, Bezirk Kulm.',"
        "'http://x','geo','place')")
    conn.execute(
        "INSERT INTO chunks (chunk_id, parent_id, chunk_index, char_start, char_end, text) "
        "VALUES ('1#0','1',0,0,35,'GONTENSCHWIL 3\nDorf im Kanton Aargau, Bezirk Kulm.')")
    dims = 8
    vector = [1.0] + [0.0] * (dims - 1)
    conn.execute(
        "INSERT INTO embeddings (chunk_id, model, dims, vector) VALUES (?,?,?,?)",
        ("1#0", "test-model", dims, struct.pack(f"<{dims}f", *vector)))
    conn.commit(); conn.close()
    db.set_db_path(str(path))
    vectorstore.clear_cache()
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


def test_a_semantic_hit_carries_everything_needed_to_cite_it(tmp_path):
    """The comment above says how an HBLS article is cited; this checks it.

    The hit used to ship `headword` and `page` but no volume under that name
    and no PDF link at all — the volume travelled as "year", which for this
    corpus is a number between 1 and 8 and not a year. A consumer reading it
    as a publication date got "1923" wrong by nineteen centuries, and a reader
    following the citation got nowhere.

    Downstream (ch-h-bot#271) this is what makes a semantic hit usable as
    evidence rather than as an unfollowable claim.
    """
    db, vector = _semantic_db(tmp_path)

    hit = db.search_semantic(vector, limit=5, model="test-model")[0]

    assert hit["volume"] == 3
    assert hit["pdf_url"] == "http://x"
    assert "year" not in hit, (
        "the volume must not travel as a year; it is 1-8 for this corpus")
    # The same citation fields the keyword search has always returned.
    for key in ("headword", "volume", "page", "pdf_url"):
        assert key in hit, key


def test_semantic_stats_reports_coverage(tmp_path):
    db, _ = _semantic_db(tmp_path)

    stats = db.semantic_stats()

    assert stats["n_chunks"] == 1 and stats["indexed"] is True
    # The shared vectorstore.stats names the parent counts n_indexed/n_total;
    # the old n_entries_* keys are gone (thodel/ch-h-bot#555: nobody read them).
    assert stats["n_indexed"] == 1 and stats["n_total"] == 1
    assert stats["coverage"] == 1.0
    assert not {k for k in stats if k.startswith("n_entries")}, sorted(stats)


def test_every_sql_statement_matches_the_schema(tmp_path):
    import sqlite3

    import db

    import vectorstore

    conn = sqlite3.connect(tmp_path / "schema.db")
    conn.executescript(db.SCHEMA_SQL)
    conn.executescript(vectorstore.SCHEMA_SQL)
    conn.execute(db.JOIN_SQL.format(placeholders="?"), ("x",)).fetchall()
    conn.close()


def test_every_module_is_copied_into_the_image():
    """hls_mcp shipped without embeddings.py and crash-looped on the import."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent
    copy_lines = [l for l in (root / "Dockerfile").read_text(encoding="utf-8").splitlines()
                  if l.strip().startswith("COPY")]
    copied = set(re.findall(r"([\w]+\.py)", " ".join(copy_lines)))
    # embed_db.py and migrate_parent_id.py are run by hand on the host, not
    # by the server.
    shipped = {p.name for p in root.glob("*.py")
               if not p.name.startswith("test_")
               and p.name not in ("embed_db.py", "migrate_parent_id.py")}
    assert not shipped - copied, f"not COPYed into the image: {sorted(shipped - copied)}"


def test_the_semantic_sql_uses_only_columns_the_deployed_database_has():
    """SCHEMA_SQL is not what is deployed.

    It declares category and lexical_class; the database on tei was built
    before those were added and has neither. Building a fixture from
    SCHEMA_SQL therefore proves nothing about production — the query passed
    every local test and failed on the first real call with
    'no such column: e.category'.

    So this asserts against the columns that are actually there.
    """
    import re

    import db

    deployed = {"id", "headword", "volume", "page", "snippet", "article_text",
                "pdf_url"}
    referenced = set(re.findall(r"\be\.(\w+)", db.JOIN_SQL))
    assert referenced <= deployed, (
        f"columns absent from the deployed database: {sorted(referenced - deployed)}"
    )


# ── The move to the shared embed_db.py + vectorstore.py (#9) ─────────────────
# The index on idhefix was written by this repo's old embed_db.py: 54,015
# passages with `headword volume` on the first line, chunks.doc_id as TEXT next
# to articles.id as INTEGER. ch-h-bot#555 measured that the shared build_chunks
# reproduces every row byte for byte, so the migration is a column rename and
# no re-embed. These tests pin the two halves of that on a fixture: the rows
# the old code wrote (frozen below, from the old code before it was deleted)
# and what the new code finds and writes after migrate_parent_id.py.

import hashlib
import struct

# The schema the old embed_db.py built, as deployed: doc_id, n_articles,
# ix_chunks_entry. Frozen here because the live code no longer has it.
OLD_EMBEDDING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT    PRIMARY KEY,
    doc_id    TEXT    NOT NULL,
    chunk_index INTEGER NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    UNIQUE (doc_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT    PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    model    TEXT    NOT NULL,
    dims     INTEGER NOT NULL,
    vector   BLOB    NOT NULL
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

# Transcription with the edition's abbreviation marks (see corpus.py). "Hein"
# is the lever: in article 1 alone it is a fragment four times and a word four
# times, so it stays "Hein rich"; with article 3 counted the ratio tips to 6:4
# and it becomes "Heinrich". Only a vocabulary over the whole table gets it.
_LONG = (
    "Zwingli, Ulrich, Reformator, geb. 1. Jan. 1484 in Wildhaus, gest. 11. Okt. 1531 bei Kappel. "
    "Sohn des Ammanns Ulrich Zwingli un̄ und der Margaretha Bruggmann ✳ Studierte in Basel, Bern "
    "un̄ und Wien, dann wieder in Basel, wo er 1506 Magister wurde. Hein r₎ rich war sein Bruder, "
    "Hein hiess auch ein Vetter ✳ 1506 Leutpriester in Glarus, 1516 in Einsiedeln, 1519 in Zürich. "
    "Diz ist dˀ der Anfang der Reformation in Zürich. "
) * 4
_HEINRICH = (
    "Heinrich, Bischof von Konstanz. Hein r₎ rich stif tˀin terin un̄ und Hein r₎ rich der Ältere "
    "✳ regierte lange. Das Kloster Oͤs wart un̄ und blieb."
)

# id, headword, volume, page, snippet, article_text, pdf_url, category, lexical_class.
# "ZWINGLI " and "BRUGG " keep their trailing blank on purpose: a .strip() on
# the head cost hls 11 passages in the measurement (8 titles with a blank at
# the edge), so the head has to be verbatim. With no volume, "BRUGG " ends
# the head and a strip would change it.
MIGRATION_ARTICLES = [
    (1, "ZWINGLI ", 7, 712, "Zürcher Reformatorenfamilie", _LONG,
     "https://example.org/v7p712.pdf", "fam", "Familie"),
    (2, "BRUGG ", None, 380, "Stadt im Aargau", "Brugg, Stadt un̄ und Bezirk im Kanton Aargau.",
     "https://example.org/v2p380.pdf", "geo", "Stadt"),
    (3, "HEINRICH", 3, 86, "Bischof", _HEINRICH,
     "https://example.org/v3p86.pdf", "bio", "Person"),
    (4, "LEER", 1, 1, "", "", "https://example.org/v1p1.pdf", "tem", "Begriff"),
    (5, "NULL", 2, 2, "", None, "https://example.org/v2p2.pdf", "tem", "Begriff"),
]

# What the old code produced on MIGRATION_ARTICLES, recorded by running it
# (embed_db.build_chunks of 897d9f1, chunk_chars=1000, chunk_overlap=150)
# before it was deleted.
OLD_HEADS = {1: "ZWINGLI  7", 2: "BRUGG ", 3: "HEINRICH 3", 4: "LEER 1", 5: "NULL 2"}
OLD_CLEAN_2 = "Brugg, Stadtund Bezirk im Kanton Aargau."
OLD_CLEAN_3 = ("Heinrich, Bischof von Konstanz. Heinrich stifterin und Heinrich der Ältere "
               "regierte lange. Das Kloster Oͤs wartund blieb.")
OLD_CLEAN_1_LEN, OLD_CLEAN_1_SHA = 1595, "46699d4bbc433952"
# chunk_id, doc_id (TEXT), chunk_index, char_start, char_end, len(text), sha256[:16], text[:22]
OLD_CHUNKS = [
    ("1#0", "1", 0, 0, 890, 900, "9d580398a3cb39b8", "ZWINGLI  7\nZwingli, Ul"),
    ("1#1", "1", 1, 740, 1595, 866, "47b853f32c519c24", "ZWINGLI  7\n9 in Zürich"),
    ("2#0", "2", 0, 0, 40, 47, "78e20bc2269fb18d", "BRUGG \nBrugg, Stadtund"),
    ("3#0", "3", 0, 0, 121, 132, "e076fa7382168a31", "HEINRICH 3\nHeinrich, B"),
]
FAKE_MODEL, FAKE_DIMS = "test-model", 8


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _chunk_rows(conn, parent_col):
    return [(cid, pid, idx, cs, ce, len(t), _sha(t), t[:22]) for cid, pid, idx, cs, ce, t
            in conn.execute(f"SELECT chunk_id, {parent_col}, chunk_index, char_start, "
                            f"char_end, text FROM chunks ORDER BY chunk_id")]


def _fake_vector(i):
    v = [0.0] * FAKE_DIMS
    v[i % FAKE_DIMS] = 1.0
    return v


def make_old_layout_db(path):
    """A database as the old embed_db.py left it: INTEGER article ids, TEXT
    doc_id, the old index and run columns, one fake vector per chunk."""
    import db as db_module

    con = sqlite3.connect(path)
    con.executescript(db_module.SCHEMA_SQL)
    con.executescript(OLD_EMBEDDING_SCHEMA_SQL)
    con.executemany("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?)", MIGRATION_ARTICLES)
    for i, (cid, pid, idx, cs, ce, _n, _sha_, _head) in enumerate(OLD_CHUNKS):
        # The old code wrote the chunk text itself; rebuild it from the same
        # article text and check it against the frozen signature, so the
        # fixture holds what the old code held and not a paraphrase.
        text = _old_chunk_text(cid)
        assert (len(text), _sha(text)) == (_n, _sha_), cid
        con.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)", (cid, pid, idx, cs, ce, text))
        con.execute("INSERT INTO embeddings VALUES (?,?,?,?)",
                    (cid, FAKE_MODEL, FAKE_DIMS, struct.pack(f"<{FAKE_DIMS}f", *_fake_vector(i))))
    con.execute("INSERT INTO embedding_runs (run_id, started_at, finished_at, model, dims, "
                "n_articles, n_chunks) VALUES ('r1','2026-01-01T00:00:00Z',"
                "'2026-01-01T00:01:00Z',?,?,3,4)", (FAKE_MODEL, FAKE_DIMS))
    con.commit(); con.close()


def _old_chunk_text(chunk_id):
    """The text of one frozen chunk, rebuilt the way the old code built it:
    head + newline + the window of the cleaned article text."""
    import corpus

    vocab = corpus.build_vocab(a[5] for a in MIGRATION_ARTICLES if a[5])
    article = {a[0]: a for a in MIGRATION_ARTICLES}[int(chunk_id.split("#")[0])]
    cleaned = corpus.clean_entry_text(article[5], vocab)
    cid, pid, idx, cs, ce, *_ = next(r for r in OLD_CHUNKS if r[0] == chunk_id)
    head = " ".join(str(x) for x in (article[1], article[2]) if x)
    return f"{head}\n{cleaned[cs:ce].strip()}"


def test_head_and_clean_are_what_the_old_embed_db_wrote():
    """Frozen from the old code on the fixture. The head is verbatim — the
    trailing blank of "ZWINGLI " survives as a double space, the one of
    "BRUGG " (no volume) ends the head — and a volume of None adds nothing."""
    import corpus

    for aid, headword, volume, *_ in MIGRATION_ARTICLES:
        assert corpus.head_line(headword, volume) == OLD_HEADS[aid], aid
    assert corpus.head_line("ZWINGLI ", 7) == "ZWINGLI  7"
    assert corpus.head_line("BRUGG ", None) == "BRUGG "

    con = sqlite3.connect(":memory:")
    con.executescript(corpus_schema_with_chunks())
    con.executemany("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?)", MIGRATION_ARTICLES)
    docs = {aid: (head, text) for aid, head, text in corpus.iter_documents(con)}
    assert sorted(docs) == [1, 2, 3], "empty and NULL texts are not documents"
    assert corpus.clean(docs[2][1]) == OLD_CLEAN_2
    assert corpus.clean(docs[3][1]) == OLD_CLEAN_3
    cleaned_1 = corpus.clean(docs[1][1])
    assert (len(cleaned_1), _sha(cleaned_1)) == (OLD_CLEAN_1_LEN, OLD_CLEAN_1_SHA)
    assert "Heinrich war sein Bruder" in cleaned_1


def corpus_schema_with_chunks():
    import db as db_module
    import vectorstore
    return db_module.SCHEMA_SQL + vectorstore.SCHEMA_SQL


def test_the_vocabulary_is_built_over_the_whole_table_regardless_of_limit():
    """iter_documents(limit=1) yields article 1 only, but "Hein" must still be
    judged with article 3 counted: the old embed_db.py built the vocabulary
    before applying --limit, and that is how the 54,015 passages were
    measured row-identical. A vocabulary over the yielded rows alone gives
    "Hein rich"."""
    import corpus

    con = sqlite3.connect(":memory:")
    con.executescript(corpus_schema_with_chunks())
    con.executemany("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?)", MIGRATION_ARTICLES)
    docs = list(corpus.iter_documents(con, limit=1))
    assert [d[0] for d in docs] == [1]
    cleaned = corpus.clean(docs[0][2])
    assert "Heinrich war sein Bruder" in cleaned and "Hein rich" not in cleaned
    # The counter-case, so the assertion above is known to discriminate.
    assert "Hein rich" in corpus.clean_entry_text(_LONG, corpus.build_vocab([_LONG]))


def test_the_shared_build_chunks_writes_the_rows_the_old_code_wrote(tmp_path):
    """The measurement of ch-h-bot#555 on the fixture: same chunk ids, same
    parent (TEXT, through SQLite affinity against INTEGER article ids), same
    offsets, same text to the byte."""
    import embed_db
    import vectorstore

    path = tmp_path / "rebuild.db"
    con = sqlite3.connect(path)
    con.executescript(corpus_schema_with_chunks())
    con.executemany("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?)", MIGRATION_ARTICLES)
    written = embed_db.build_chunks(con, None, 1000, 150, True)
    assert written == len(OLD_CHUNKS)
    assert _chunk_rows(con, "parent_id") == OLD_CHUNKS
    assert con.execute("SELECT DISTINCT typeof(parent_id) FROM chunks").fetchall() == [("text",)]
    # A second pass finds nothing left to chunk.
    assert embed_db.build_chunks(con, None, 1000, 150, True) == 0
    con.close()


def test_migration_keeps_every_chunk_and_vector(tmp_path):
    """migrate_parent_id.py on a database in the old layout: afterwards the
    shared embed_db.py finds every chunk with its vector and has nothing to
    embed, and search_semantic answers through vectorstore."""
    import db as db_module
    import embed_db
    import migrate_parent_id
    import vectorstore

    path = tmp_path / "old.db"
    make_old_layout_db(path)
    before = _chunk_rows(sqlite3.connect(path), "doc_id")
    assert before == OLD_CHUNKS

    assert migrate_parent_id.main(["--db", str(path)]) == 0
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(chunks)")]
    assert "parent_id" in cols and "doc_id" not in cols
    run_cols = [r[1] for r in con.execute("PRAGMA table_info(embedding_runs)")]
    assert "n_parents" in run_cols and "n_articles" not in run_cols
    index_names = sorted(r[1] for r in con.execute("PRAGMA index_list(chunks)")
                         if not r[1].startswith("sqlite_autoindex"))
    assert index_names == ["ix_chunks_parent"]
    assert con.execute("SELECT typeof(parent_id) FROM chunks LIMIT 1").fetchone() == ("text",)
    assert _chunk_rows(con, "parent_id") == OLD_CHUNKS
    assert con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == len(OLD_CHUNKS)
    # The shared SCHEMA_SQL on top adds nothing: no second index.
    con.executescript(vectorstore.SCHEMA_SQL)
    assert sorted(r[1] for r in con.execute("PRAGMA index_list(chunks)")
                  if not r[1].startswith("sqlite_autoindex")) == ["ix_chunks_parent"]
    con.close()

    # Idempotent: a second run changes nothing and still passes its checks.
    assert migrate_parent_id.main(["--db", str(path)]) == 0
    assert _chunk_rows(sqlite3.connect(path), "parent_id") == OLD_CHUNKS

    # The shared embed_db.py: nothing new to chunk, nothing pending to embed —
    # it returns before it would open a client, so no GPUStack is needed.
    assert embed_db.main(["--db", str(path), "--model", FAKE_MODEL, "--quiet"]) == 0
    con = sqlite3.connect(path)
    assert _chunk_rows(con, "parent_id") == OLD_CHUNKS
    assert con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == len(OLD_CHUNKS)
    pending = con.execute(
        "SELECT COUNT(*) FROM chunks c LEFT JOIN embeddings e ON e.chunk_id = c.chunk_id "
        "AND e.model = ? WHERE e.chunk_id IS NULL", (FAKE_MODEL,)).fetchone()[0]
    assert pending == 0
    last_run = con.execute("SELECT n_parents, n_chunks FROM embedding_runs "
                           "ORDER BY started_at DESC LIMIT 1").fetchone()
    assert last_run == (3, 4)
    con.close()
    assert embed_db.main(["--db", str(path), "--status"]) == 0

    db_module.set_db_path(str(path))
    stats = db_module.semantic_stats()
    assert (stats["n_chunks"], stats["n_indexed"], stats["n_total"]) == (4, 3, 5)
    hits = db_module.search_semantic(_fake_vector(3), limit=5, model=FAKE_MODEL)
    assert hits and hits[0]["chunk_id"] == "3#0" and hits[0]["id"] == "3"
    assert hits[0]["headword"] == "HEINRICH" and hits[0]["volume"] == 3
    assert hits[0]["snippet"].startswith("Heinrich, Bischof")
    assert db_module.warm_semantic_index(FAKE_MODEL)["n_chunks"] == 4


def test_the_snippet_starts_with_the_text_not_the_head(tmp_path):
    """Fleet rule (ch-h-bot#555, eperiodica_mcp#10): the head line is for the
    vector; the hit carries headword, volume and page as fields. Cut only the
    head of this article, built by the same function that wrote it; offsets
    are untouched because they never counted the head."""
    import db as db_module

    piece = "Dorf im Kanton Aargau,\nBezirk Kulm."
    assert db_module.passage_body(f"GONTENSCHWIL 3\n{piece}", "GONTENSCHWIL", 3) == piece
    assert db_module.passage_body(f"ZWINGLI  7\n{piece}", "ZWINGLI ", 7) == piece
    # Another article's head, a head without the newline, or no head: untouched.
    assert db_module.passage_body(f"GONTENSCHWIL 3\n{piece}", "GONTENSCHWIL", 4).startswith("GONT")
    assert db_module.passage_body("GONTENSCHWIL 3 ist ein Dorf", "GONTENSCHWIL", 3) \
        == "GONTENSCHWIL 3 ist ein Dorf"
    assert db_module.passage_body(piece, "GONTENSCHWIL", 3) == piece
    assert db_module.passage_body(piece, None, None) == piece

    db, vector = _semantic_db(tmp_path)
    hit = db.search_semantic(vector, limit=5, model="test-model")[0]
    assert hit["snippet"] == "Dorf im Kanton Aargau, Bezirk Kulm."
    assert (hit["char_start"], hit["char_end"]) == (0, 35)
    assert hit["chunk_index"] == 0


def test_year_filter_still_ranges_over_the_volume(tmp_path):
    """year_from/year_to filter the volume number, as the NOTE in db.py says —
    on purpose, in row_to_hit now (None drops the row)."""
    db, vector = _semantic_db(tmp_path)

    row = {"chunk_id": "1#0", "parent_id": "1", "chunk_index": 0, "char_start": 0,
           "char_end": 35, "text": "x", "headword": "G", "volume": 3, "page": 86,
           "pdf_url": "http://x"}
    assert db.row_to_hit(row, "1#0", 0.9, year_from=3, year_to=3)["volume"] == 3
    assert db.row_to_hit(row, "1#0", 0.9, year_from=4) is None
    assert db.row_to_hit(row, "1#0", 0.9, year_to=2) is None
    assert db.row_to_hit(dict(row, volume=None), "1#0", 0.9, year_from=1) is None
    assert "year" not in db.row_to_hit(row, "1#0", 0.9)

    assert db.search_semantic(vector, model="test-model", year_from=3, year_to=3)
    assert db.search_semantic(vector, model="test-model", year_from=4) == []
    assert db.search_semantic(vector, model="test-model", year_to=2) == []


def test_the_shared_files_are_byte_identical_with_the_fleet():
    """vectorstore.py and embed_db.py are vendored from eperiodica_mcp; a local
    edit would make ch-h-bot's check_vendored_drift.py red."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent
    digests = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()[:12]
               for name in ("embed_db.py", "vectorstore.py", "embeddings.py")}
    # 26.10.2026: default model qwen3-embedding-4b in both files (ch-h-bot#570).
    assert digests == {"embed_db.py": "abd29cd4d258", "vectorstore.py": "adde376b0f75",
                       "embeddings.py": "b8d38be69cb1"}
