"""Tests for rebuild/hls_links.py (HBLS ↔ HLS links). No network: the HLS answers are put into the cache and
the client runs offline.

    pytest test_hls_links.py
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from rebuild import hls_links as hl  # noqa: E402


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_norm_umlaut_ocr_ligature_accents():
    assert hl.norm("STÆGER") == hl.norm("Stäger") == "staeger"
    assert hl.norm("Palézieux") == "palezieux"
    assert hl.norm("Müller") == hl.norm("MUELLER")


def test_hls_title_key():
    assert hl.hls_title_key("Erlach, von") == ("erlach", "")
    assert hl.hls_title_key("Eschenbach Kloster") == ("eschenbach", "Kloster")
    assert hl.hls_title_key("Lenzburg Herrschaft, Amt") == ("lenzburg", "Herrschaft")
    assert hl.hls_title_key("Meyer (LU)") == ("meyer", "")


@pytest.mark.parametrize("text, want", [
    ("(K t. Bern, Am tsbez. Erlach. S. GLS). Städtchen", {"BE"}),
    ("Familien der K te. Bern und Schwyz. A. Burgergeschlecht", {"BE", "SZ"}),
    ("Fam ilien der K te. G raubünden u. Tessin. A.", {"GR", "TI"}),
    ("von. Altangesehenes adeliges Berner Geschlecht", {"BE"}),
    ("f Geschlecht der S tadt Zürich, begründet durch", {"ZH"}),
    ("Eine der ältesten Fam ilien des K antons Uri, heute in Altdorf", {"UR"}),
    ("Fam ilie in Davos seit dem 15. Jahrh.", {"GR"}),
    ("Seit 1649 in Basel eingebürgertes Geschlecht", {"BS"}),
    ("Verbreitetes Geschlecht des K ts. Bern", {"BE"}),
    ("Fam ilie von Sauvigny im B ourbonnais", set()),
])
def test_cantons_in(text, want):
    assert hl.cantons_in(text) == want


def test_hls_cantons_and_span():
    art = {"place_class": ["Schweiz -> AG", "Schweiz -> BL", "Ausland -> Deutschland"],
           "time_span": ["1901–1950", "1401–1450"]}
    assert hl.hls_cantons(art) == {"AG", "BL"}
    assert hl.hls_span(art) == (1401, 1950)


@pytest.mark.parametrize("hw, text, want", [
    ("ERLACH", "(K t. Bern, Am tsbez. Erlach. S. GLS). Städtchen", "geo"),
    ("SIGGENTAL", "(K t. Aargau, Bez. Baden. S. GLS). Zwei Gem.", "geo"),
    ("KISTLER", "Familien der Kte. Bern und Schwyz.", "fam"),
    ("GRIESSENBERG", "von. Thurgauisches Freiherrengeschlecht", "fam"),
    ("TINGRY", "Pi er r e F r a n ç o i s , * 1743 in S o isso n s, f 6. II. 1821", "bio"),
    ("SZGL", "Schweizer Geschlechterbuch", "skip"),
    ("MŒRDERBURG", "", "skip"),
    ("GOTTESURTEIL", "( Ju di c i u mdei ). Proben, an deren Ausgang", "other"),
    ("AARBURG", "(VON). herren von Froburg, der Gründer von Schloss", "fam"),
    ("AARBERG", "( L A N D V O G T E I) . bezirk. Bis 1798 Landvogtei", "geo"),
    ("AARZIELE", "Siehe M. a r z il i", "skip"),
])
def test_article_kind(hw, text, want):
    assert hl.article_kind(hw, text) == want


def test_place_kind():
    assert hl.place_kind("( L A N D V O G T E I) . bezirk.") == "Landvogtei"
    assert hl.place_kind("(K t. Bern, Am tsbez. Erlach. S. GLS). Städtchen") == ""
    assert hl.place_kind("(Kt. Luzern). Kloster der Zisterzienserinnen") == "Kloster"
    assert hl.QUAL_GROUP["Landvogtei"] == hl.QUAL_GROUP["Amtsbezirk"]


def test_bio_head():
    assert hl.bio_head("Pi er r e F r a n ç o i s , * 1743 in S o isso n s, f 6. II. 1821 in C o") \
        == ("Pierre François", 1743, 1821)
    assert hl.bio_head("Wi l he l m, K unsthistoriker, * 14. ix . 1847 zu W erden") == ("Wilhelm", 1847, None)
    assert hl.bio_head("J o h a n n e s, latinisiert aus Dreifuss. 1555-1617, P farrer")[0] == "Johannes"
    assert hl.bio_head("Bürger von Delsberg, Cl a u d e")[0] is None


def test_hbls_id():
    assert hl.hbls_id(4, 510, "KISTLER") == "hbls-4-510-KISTLER"
    assert hl.hbls_id(4, 510, "KISTLER", 3) == "hbls-4-510-KISTLER#3"


# ── person matching ──────────────────────────────────────────────────────────

SCHINER = [
    {"id": "per-021302", "article_id": "021302", "family_name": "Schiner", "first_name": "Franz Joseph Ignaz",
     "birth_date": "1761-05-30", "death_date": "1845-02-27"},
    {"id": "per-021445", "article_id": "021445", "family_name": "Schiner", "first_name": "Hildebrand",
     "birth_date": "1754-10-01", "death_date": "1819-07-26"},
    {"id": "per-000001", "article_id": "000001", "family_name": "Schiner", "first_name": "Matthäus",
     "birth_date": "1465", "death_date": "1522-10-01"},
    {"id": "per-000002", "article_id": "000002", "family_name": "Schiner", "first_name": "Hans",
     "birth_date": "", "death_date": "1560"},
    {"id": "per-000003", "article_id": "000003", "family_name": "Schiner", "first_name": "Hans",
     "birth_date": "", "death_date": "1562"},
]


def test_person_levels():
    assert hl.match_person("Hildebrand", 1754, 1819, SCHINER)["level"] == "sicher"
    r = hl.match_person("Matthäus", None, 1522, SCHINER)
    assert (r["level"], r["hls_id"]) == ("wahrscheinlich", "000001")
    assert hl.match_person("Matthaeus", 1468, None, SCHINER)["level"] == "möglich"
    assert hl.match_person("Franz", 1761, 1845, SCHINER)["level"] == "sicher"  # first given name


def test_person_contradiction_and_missing_data():
    assert hl.match_person("Hildebrand", 1754, 1790, SCHINER)["level"] is None  # death 29 years off
    assert hl.match_person("Hildebrand", None, None, SCHINER)["level"] is None  # no dates: no guess
    assert hl.match_person("Peter", 1754, 1819, SCHINER)["level"] is None


def test_person_ambiguous_is_not_guessed():
    r = hl.match_person("Hans", None, 1561, SCHINER)
    assert r["level"] == "mehrdeutig" and sorted(r["candidates"]) == ["000002", "000003"]


# ── offline HLS client and full run ─────────────────────────────────────────

def test_parse_tool_result_sse_and_json():
    sse = ('event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":{"structuredContent":'
           '{"result":[{"id":"1"}]},"content":[]}}\r\n\r\n')
    assert hl.parse_tool_result(sse) == [{"id": "1"}]
    js = json.dumps({"jsonrpc": "2.0", "id": 1,
                     "result": {"content": [{"type": "text", "text": '{"id": "021009"}'}]}})
    assert hl.parse_tool_result(js) == {"id": "021009"}
    with pytest.raises(RuntimeError):
        hl.parse_tool_result(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}}))


def _cache(path, calls):
    hls = hl.HLS(path, offline=True)
    for tool, args, result in calls:
        hls.db.execute("INSERT INTO calls(tool, args, result) VALUES (?,?,?)",
                       (tool, json.dumps(args, sort_keys=True, ensure_ascii=False), json.dumps(result)))
    hls.db.commit()
    return hls


FAM = [{"id": "F1", "title": "Kistler", "category": "fam"},
       {"id": "F2", "title": "Meyer (LU)", "category": "fam"},
       {"id": "F3", "title": "Meyer (ZH)", "category": "fam"},
       {"id": "F4", "title": "Erlach, von", "category": "fam"},
       {"id": "F5", "title": "Gysin", "category": "fam"}]
GEO = [{"id": "G1", "title": "Erlach", "category": "geo"},
       {"id": "G2", "title": "Erlach Herrschaft", "category": "geo"},
       {"id": "G3", "title": "Erlach Amtsbezirk", "category": "geo"}]
ARTS = {"F1": ["SZ"], "F2": ["LU"], "F3": ["ZH"], "F4": ["BE"], "F5": ["BL", "BS"], "G1": ["BE"], "G2": ["BE"],
        "G3": ["BE"]}


@pytest.fixture
def setup(tmp_path):
    db = tmp_path / "hbls.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, headword TEXT, volume INTEGER, page INTEGER, "
              "snippet TEXT, article_text TEXT, pdf_url TEXT)")
    c.execute("CREATE TABLE members (id INTEGER PRIMARY KEY, article_id INTEGER, given TEXT, birth_year INTEGER, "
              "death_year INTEGER, member_n INTEGER)")
    c.executemany("INSERT INTO articles(id, headword, volume, page, article_text) VALUES (?,?,?,?,?)", [
        (1, "KISTLER", 4, 510, "Familien der K te. Bern und Schwyz. 1381 Peter."),
        (2, "MEYER", 5, 1, "Familien des Kts. Luzern. 1500."),
        (3, "MEYER", 5, 2, "Verbreitetes Geschlecht. 1600."),
        (4, "ERLACH", 3, 68, "(K t. Bern, Am tsbez. Erlach. S. GLS). Städtchen, 1185."),
        (5, "ERLACH", 3, 69, "von. Altangesehenes adeliges Berner Geschlecht. 1300."),
        (6, "GYSINN", 4, 1, "Geschlecht des Kts. Basel-Landschaft. 1600."),
        (7, "SZGL", 6, 1, "Schweizer Geschlechterbuch."),
    ])
    c.executemany("INSERT INTO members(article_id, given, birth_year, death_year, member_n) VALUES (?,?,?,?,?)",
                  [(5, "Rudolf", 1299, 1360, 1), (5, "Hans", None, None, 2)])
    c.commit()
    calls = [("list_articles_by_category", {"category": "fam", "limit": 500, "offset": 0}, FAM),
             ("list_articles_by_category", {"category": "geo", "limit": 500, "offset": 0}, GEO),
             ("search_persons", {"query": "Erlach", "limit": 200},
              [{"id": "per-P1", "article_id": "P1", "family_name": "von Erlach", "first_name": "Rudolf",
                "birth_date": "1299", "death_date": "1360"},
               {"id": "per-P2", "article_id": "P2", "family_name": "Erlacher", "first_name": "Rudolf",
                "birth_date": "1299", "death_date": "1360"}])]
    calls += [("get_article", {"article_id": k}, {"id": k, "place_class": [f"Schweiz -> {x}" for x in v],
                                                   "time_span": ["1251–1300", "1951–2000"]})
              for k, v in ARTS.items()]
    return db, _cache(tmp_path / "cache.db", calls), tmp_path / "out.db"


def test_run(setup):
    db, hls, out = setup
    stats = hl.run(str(db), str(out), hls, log=lambda m: None)
    o = sqlite3.connect(out)
    links = {r[0]: r[1:] for r in o.execute("SELECT hbls_id, hls_id, level, kind FROM article_links")}
    assert links["hbls-4-510-KISTLER"] == ("F1", "sicher", "fam")          # canton SZ agrees
    assert links["hbls-5-1-MEYER"] == ("F2", "sicher", "fam")              # LU picks one of two
    assert "hbls-5-2-MEYER" not in links                                    # two Meyer, no canton: review
    assert links["hbls-3-68-ERLACH"] == ("G1", "sicher", "geo")             # the town, not the lordship
    assert links["hbls-3-69-ERLACH"] == ("F4", "sicher", "fam")
    assert links["hbls-3-69-ERLACH#1"] == ("P1", "sicher", "member")       # Erlacher is another family
    assert "hbls-3-69-ERLACH#2" not in links                                # no dates
    assert links["hbls-4-1-GYSINN"][:2] == ("F5", "möglich")              # near spelling (Gysin), BL
    assert not any(k.startswith("hbls-6-1-SZGL") for k in links)
    review = {r[0]: json.loads(r[1]) for r in o.execute("SELECT hbls_id, candidates FROM article_link_review")}
    assert sorted(review["hbls-5-2-MEYER"]) == ["F2", "F3"]
    assert stats["_types"]["skip"] == 1
    # the input DB is untouched
    assert sqlite3.connect(db).execute("SELECT name FROM sqlite_master WHERE name='article_links'").fetchone() is None


def test_offline_cache_miss_raises(tmp_path):
    hls = hl.HLS(tmp_path / "c.db", offline=True)
    with pytest.raises(LookupError):
        hls.call("get_article", article_id="000001")
