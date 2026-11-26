"""Tests of the rebuild pipeline (rebuild/) and of the tools on a rebuilt database.

No model and no network: the OCR text is a hand-made excerpt of HBLS IV p. 500 (KISTLER), as qwen
writes it, and the structured family comes from the rule-based reader (members.py).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from rebuild import build as build_mod
from rebuild import evaluate, members, segment, structure

PAGE_510 = """[HEADER] 500 KISTENPASS
[COL 1]
Altdorf (1895), sowie der Denkmäler für Alfred Escher. — *SKL*. — *NZZ* 1919, Nr. 1085. [H. Br.]
**KISTENPASS** (Kte. Glarus und Graubünden. S. *GLS*). Verbindung zwischen Brigels und Linthal. [Nz.]
**KISTLER.** Familien der Kte. Bern und Schwyz.
A. Kanton Bern. I. † Burgergeschlecht der Stadt Bern. Wappen: in Blau eine goldene Truhe. PETER, Mitglied der GC 1381. — BURKART, Herr zu Schönegg usw. (siehe v. Mülinen: *Beitr.* III, 43). — PETER, Bauherr 1426, Venner 1446. — _Peter_, Mitglied der GC 1440, des Kleinen Rats 1452, Landvogt zu Trachselwald 1452; † 1480. — *SBB* I. — *ADB*. — *LL*. — Von seinen Söhnen ist zu nennen: — PETER, Chorherr zu Amsoldingen, † 1492. In warscheinlichem Zusammenhange steht die heute noch blühende — II. Familie K. in Aarberg. Aus dieser stammte — HERMANN, * 1853, Pfarrer in Täuffelen, Staatsschreiber des Kts. Bern 1891-† 15. VI. 1916. — *Hst.* 36, Nr. 11. [H. Tr.]
B. Kanton Schwyz. Alteingesessenes Geschlecht der Gem. Reichenburg — FERDINAND, Dr. med. 1832-1914, Gemeindepräsident. — [R-r.] — PIUS, * 1877 in Reichenburg, Priester 1900, † 6. II. 1925 in Zizers. — [H. Br.]
**KITT.** Altes Geschlecht der Stadt Zürich. Wappen: in Blau eine Quittenbirne. — 1. SEBASTIAN, von Feld-
[COL 2]
KLARER
kirch (Vorarlberg), Seiler, Bürger 1535. — 2. DAVID, 1718-1802, Pfarrer in Seebach 1744. — 3. DAVID, 1776-1850, Kaufmann in Feldkirch. — *LL.* — *SKL.* [H. Br.]
**KLÆGER.** Siehe KLEGER.
**SKL.** Schweizerisches Künstler-Lexikon, kein Stichwort.
**KLANX.** Siehe CLANX."""


@pytest.fixture()
def pages_db(tmp_path):
    p = tmp_path / "pages.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE pages (volume INTEGER, page INTEGER, mode TEXT, status TEXT, text TEXT)")
    con.execute("INSERT INTO pages VALUES (4, 510, 'columns', 'ok', ?)", (PAGE_510,))
    con.commit()
    return con


def test_segment_cuts_headwords_and_joins_columns(pages_db):
    arts = segment.segment_volume(pages_db, 4, "columns")
    assert [a["headword"] for a in arts] == ["KISTENPASS", "KISTLER", "KITT", "KLÆGER", "KLANX"]
    kitt = arts[2]
    assert "SEBASTIAN, von Feldkirch (Vorarlberg)" in kitt["text"]   # hyphen joined over the column
    assert "KLARER" not in kitt["text"]                               # the running head is not text
    assert all(a["printed_page"] == 500 for a in arts)
    assert arts[3]["type"] == "verweis" and arts[3]["target"] == "KLEGER"
    assert arts[1]["type"] == "fam"
    assert any("bold-not-headword:SKL" in f for f in arts[3]["flags"])  # the stoplist kept SKL out


def test_members_of_kistler_and_kitt(pages_db):
    arts = {a["headword"]: a for a in segment.segment_volume(pages_db, 4, "columns")}
    k = members.find_members(arts["KISTLER"]["text"], "KISTLER")
    assert [m["name"] for m in k] == ["Peter", "Burkart", "Peter", "Peter", "Peter", "Hermann", "Ferdinand", "Pius"]
    assert (k[5]["birth"], k[5]["death"]) == (1853, 1916)
    assert (k[6]["birth"], k[6]["death"]) == (1832, 1914)
    assert (k[7]["birth"], k[7]["death"]) == (1877, 1925)
    assert k[3]["spaced"] and k[3]["death"] == 1480
    assert k[6]["branch"] == "B"
    t = members.find_members(arts["KITT"]["text"], "KITT")
    assert [(m["nr"], m["name"], m["birth"], m["death"]) for m in t] == [
        (1, "Sebastian", None, None), (2, "David", 1718, 1802), (3, "David", 1776, 1850)]
    assert not any(m["name"] in ("Skl", "Ll", "Adb") for m in k + t)   # literature is not a member


def test_rules_catch_a_bad_structure(pages_db):
    a = {x["headword"]: x for x in segment.segment_volume(pages_db, 4, "columns")}["KITT"]
    res = {"branches": [], "members": [
        {"nr": "1", "given": "Sebastian", "birth": None, "death": None, "kin": [], "anchor": "SEBASTIAN, von Feld"},
        {"nr": "3", "given": "David", "birth": 1850, "death": 1776, "kin": [{"relation": "son", "of_nr": "9", "of_name": None}],
         "anchor": "DAVID, 1776-1850, Kaufmann"}]}
    issues = structure.check(a, res)
    assert "numbering 1->3" in issues
    assert any(i.startswith("life span David") for i in issues)
    assert any(i.startswith("kin of_nr 9") for i in issues)
    assert any(i.startswith("rule-only member david nr 2") for i in issues)


def test_semiglobal_cer():
    assert evaluate.semiglobal("abc", "xxabcxx") == 0
    assert evaluate.semiglobal("abc", "xxabxx") == 1
    assert evaluate.norm_text("B e-\nrn  **X**  _Peter_") == evaluate.norm_text("B ern X Peter")
    assert evaluate.key("KLÆN") == evaluate.key("Klän") == "klaen"


@pytest.fixture()
def rebuilt(tmp_path, pages_db, monkeypatch):
    monkeypatch.setattr(structure, "ROOT", tmp_path)
    monkeypatch.setattr(build_mod, "struct_connect", lambda: structure.connect(tmp_path / "structure.db"))
    arts = segment.segment_volume(pages_db, 4, "columns")
    out = tmp_path / "hbls.staging.db"
    build_mod.build(arts, out, "test-1", None)
    import db as db_module
    db_module.set_db_path(str(out))
    return db_module


def test_family_members_and_ids_on_the_rebuilt_db(rebuilt):
    fam = rebuilt.get_family_members("KISTLER", 4)
    assert [m["given"] for m in fam][:3] == ["Peter", "Burkart", "Peter"]
    assert fam[0]["stable_id"] == "hbls-4-510-KISTLER#u1"
    kitt = rebuilt.get_family_members("kitt", 4)     # headword case does not matter
    assert [m["stable_id"] for m in kitt] == ["hbls-4-510-KITT#1", "hbls-4-510-KITT#2", "hbls-4-510-KITT#3"]
    m = rebuilt.get_member("hbls-4-510-KITT#3")
    assert m["given"] == "David" and m["birth_year"] == 1776 and "Kaufmann in Feldkirch" in m["passage"]
    assert "Seebach" not in m["passage"]               # the passage is this member's, not the one before


def test_search_persons_by_forename_and_family(rebuilt):
    hits = rebuilt.search_persons("Hermann Kistler")
    assert [(h["forename"], h["family_name"], h["birth_year"]) for h in hits] == [("Hermann", "KISTLER", 1853)]
    assert rebuilt.search_persons("Kistler, Pius")[0]["death_year"] == 1925
    assert len(rebuilt.search_persons("Kistler")) == 8   # one word: as before, forename or family


def test_corpus_stats_reports_the_data_version(rebuilt):
    s = rebuilt.db_stats()
    assert s["data_version"] == "test-1"
    assert s["counts"]["articles"] == 5
    cats = rebuilt.get_category_stats()
    assert cats["fam"] == 2 and cats["tem"] >= 2      # KLÆGER, KLANX are cross-references (type verweis)


def test_the_text_layer_db_says_what_it_is(tmp_path):
    import db as db_module
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript(db_module.SCHEMA_SQL)
    con.close()
    db_module.set_db_path(str(p))
    assert db_module.db_stats()["data_version"] == "2025.07-textlayer"
    assert "error" in db_module.get_member("hbls-4-510-KISTLER#u1")


def test_gold_files_are_complete():
    files = sorted((evaluate.GOLD).glob("v*_p*.json"))
    listed = [tuple(map(int, ln.split())) for ln in (evaluate.HERE / "gold_pages.txt").read_text().splitlines()]
    have = {(json.loads(f.read_text())["volume"], json.loads(f.read_text())["pdf_page"]) for f in files}
    assert set(listed) <= have
    for f in files:
        g = json.loads(f.read_text())
        assert "cer_text" not in g, "the transcribed quarters stay out of the repository (rebuild/README.md)"
        assert g["cer_region"] in ("c1a", "c1b", "c2a", "c2b")
        for a in g["articles"]:
            assert a["type"] in ("fam", "bio", "geo", "tem", "verweis"), (f.name, a["headword"])
