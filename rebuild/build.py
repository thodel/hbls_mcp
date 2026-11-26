"""Build the rebuilt HBLS database (rebuild, steps 4-5).

Inputs: the articles cut from the page OCR (rebuild/segment.py), the structured families (structure.db,
rebuild/structure.py) and the page OCR itself (pages.db). Output: a new SQLite file in the server's schema
(db.SCHEMA_SQL), extended — nothing of the old schema is removed, so the tools keep working on it:

  articles  + stable_id, pdf_page, printed_page, column_no, type, target, flags, version
            (page stays the PDF page, as before: pdf_url points at #page=<page>)
            category  fam | bio | geo | tem (a verweis is tem with type 'verweis')
  members   + stable_id, branch_id, branch_label, epithet, offices (JSON), kin (JSON), text_start, text_end,
            source ('llm' | 'rule'), review (JSON list of rule violations, null = clean)
  branches  article_id, label, name, place, canton
  page_texts volume, pdf_page, printed_page, text  (the page OCR, for checking a citation)
  meta      key/value: data_version, built_at, source, counts

Stable ids: article `hbls-{volume}-{pdf_page}-{HEADWORD}` (headword folded to A-Z), a second article of the
same name on the same page gets `-2`; member `{article id}#{nr}`, unnumbered ones `#u{k}` in order. They stay
the same over rebuilds as long as volume, page and headword do.

A person article (`bio`) gets one member row for its person (forename from the headword "KISSLING, RICHARD"),
so search_persons finds single persons as well as family members.

  python -m rebuild.build --articles articles.jsonl --out hbls.staging.db --version 2026.11-ocr1
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path

from .evaluate import key
from .members import find_members, proper
from .structure import ROOT, article_id, check, connect as struct_connect, is_family, nr_value

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db as server_db  # noqa: E402  the schema contract of the server

PDF_URL = "https://biblio.unibe.ch/digibern/hist_bibliog_lexikon_schweiz/HBLS_band_{v:02d}.pdf#page={p}"

EXTRA_SQL = """
ALTER TABLE articles ADD COLUMN stable_id TEXT;
ALTER TABLE articles ADD COLUMN pdf_page INTEGER;
ALTER TABLE articles ADD COLUMN printed_page INTEGER;
ALTER TABLE articles ADD COLUMN column_no INTEGER;
ALTER TABLE articles ADD COLUMN type TEXT;
ALTER TABLE articles ADD COLUMN target TEXT;
ALTER TABLE articles ADD COLUMN flags TEXT;
ALTER TABLE articles ADD COLUMN version TEXT;
ALTER TABLE members ADD COLUMN stable_id TEXT;
ALTER TABLE members ADD COLUMN branch_id INTEGER;
ALTER TABLE members ADD COLUMN branch_label TEXT;
ALTER TABLE members ADD COLUMN epithet TEXT;
ALTER TABLE members ADD COLUMN offices TEXT;
ALTER TABLE members ADD COLUMN kin TEXT;
ALTER TABLE members ADD COLUMN text_start INTEGER;
ALTER TABLE members ADD COLUMN text_end INTEGER;
ALTER TABLE members ADD COLUMN source TEXT;
ALTER TABLE members ADD COLUMN review TEXT;
CREATE TABLE branches (id INTEGER PRIMARY KEY, article_id INTEGER REFERENCES articles(id), label TEXT,
                       name TEXT, place TEXT, canton TEXT);
CREATE TABLE page_texts (volume INTEGER, pdf_page INTEGER, printed_page INTEGER, text TEXT,
                         PRIMARY KEY (volume, pdf_page));
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE UNIQUE INDEX ix_articles_stable ON articles(stable_id);
CREATE INDEX ix_articles_headword ON articles(headword);
CREATE INDEX ix_members_stable ON members(stable_id);
CREATE INDEX ix_members_given ON members(given);
CREATE INDEX ix_branches_article ON branches(article_id);
"""

LEXICAL = {"fam": "Familien", "bio": "Personen", "geo": "Orte & Räume", "tem": "Themen"}


def plain(text: str) -> str:
    """The stored article text: OCR markup removed, paragraphs kept."""
    t = text.replace("**", "")
    t = re.sub(r"(?<![\w*])_([^_\n]+)_(?!\w)", r"\1", t)
    t = re.sub(r"(?<!\*)\*(?!\s|\d)([^*\n]+?)(?<!\s)\*", r"\1", t)  # italics, not the birth star "* 1853"
    return t


def bio_person(a: dict) -> dict | None:
    if "," not in a["headword"]:
        return None
    given = a["headword"].split(",", 1)[1].strip()
    if not given or given.upper() in ("VON", "DE", "DI", "DE LA"):
        return None
    from .members import years
    body = plain(a["text"])
    m = re.match(r"^[^,]+,[^,.]+[.,]?", body)
    b, d = years(body[m.end():] if m else body)
    return {"nr": None, "given": proper(given), "birth": b, "death": d}


def build(articles: list[dict], out: Path, version: str, pages_db: Path | None):
    out.unlink(missing_ok=True)
    con = sqlite3.connect(out)
    con.executescript(server_db.SCHEMA_SQL)
    for stmt in EXTRA_SQL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    scon = struct_connect()
    structured = {aid: json.loads(r) for aid, r in scon.execute(
        "SELECT aid, result FROM structure WHERE status='ok'")}
    seen_ids: dict[str, int] = {}
    n_mem = {"llm": 0, "rule": 0, "bio": 0}
    n_review = 0
    for a in articles:
        base = article_id(a)
        seen_ids[base] = seen_ids.get(base, 0) + 1
        sid = base if seen_ids[base] == 1 else f"{base}-{seen_ids[base]}"
        text = plain(a["text"])
        typ = a["type"]
        res = structured.get(base) if seen_ids[base] == 1 else None
        if res and res["members"] and typ not in ("verweis",):
            typ = "fam"
        cat = "tem" if typ == "verweis" else typ
        cur = con.execute(
            "INSERT INTO articles (headword, volume, page, snippet, article_text, pdf_url, category, lexical_class, "
            "stable_id, pdf_page, printed_page, column_no, type, target, flags, version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a["headword"], a["volume"], a["pdf_page"], text[:300], text,
             PDF_URL.format(v=a["volume"], p=a["pdf_page"]), cat, json.dumps([LEXICAL[cat]], ensure_ascii=False),
             sid, a["pdf_page"], a.get("printed_page"), a.get("column"), typ, a.get("target"),
             json.dumps(a["flags"], ensure_ascii=False) if a["flags"] else None, version))
        art_id = cur.lastrowid
        if typ == "bio":
            p = bio_person(a)
            if p:
                con.execute("INSERT INTO members (article_id, given, birth_year, death_year, member_n, stable_id, "
                            "source) VALUES (?,?,?,?,?,?,?)",
                            (art_id, p["given"], p["birth"], p["death"], None, f"{sid}#u1", "bio"))
                n_mem["bio"] += 1
            continue
        if not is_family(a) and not res:
            continue
        branch_ids = {}
        if res:
            for b in res["branches"]:
                bc = con.execute("INSERT INTO branches (article_id, label, name, place, canton) VALUES (?,?,?,?,?)",
                                 (art_id, b["label"], b["name"], b.get("place"), b.get("canton")))
                branch_ids[b["label"]] = bc.lastrowid
            issues = check(a, res)
            mems = [dict(m, source="llm") for m in res["members"]]
            # a member the rule reader saw and qwen did not: keep it, marked
            llm_keys = {key(m["given"]) for m in mems}
            for m in find_members(a["text"], a["headword"]):
                if key(m["name"]) not in llm_keys:
                    mems.append({"nr": m["nr"], "given": m["name"], "birth": m["birth"], "death": m["death"],
                                 "branch": m["branch"], "source": "rule", "offices": [], "kin": [],
                                 "anchor": a["text"][m["start"]:m["start"] + 40]})
        else:
            issues = ["not structured"]
            mems = [{"nr": m["nr"], "given": m["name"], "birth": m["birth"], "death": m["death"],
                     "branch": m["branch"], "source": "rule", "offices": [], "kin": [],
                     "anchor": a["text"][m["start"]:m["start"] + 40]} for m in find_members(a["text"], a["headword"])]
        review = json.dumps(issues, ensure_ascii=False) if issues else None
        n_review += bool(issues)
        starts = []
        for m in mems:
            anchor = re.sub(r"[*_]", "", m.get("anchor") or "")[:30]
            pos = text.find(anchor) if anchor else -1
            starts.append(pos if pos >= 0 else None)
        order = sorted(p for p in starts if p is not None)
        unnumbered = 0
        for m, s in zip(mems, starts):
            nv = nr_value(m.get("nr"))
            if nv:
                msid = f"{sid}#{nv[0]}{nv[1]}"
            else:
                unnumbered += 1
                msid = f"{sid}#u{unnumbered}"
            e = None
            if s is not None:
                later = [p for p in order if p > s]
                e = later[0] if later else len(text)
            con.execute(
                "INSERT INTO members (article_id, given, birth_year, death_year, member_n, stable_id, branch_id, "
                "branch_label, epithet, offices, kin, text_start, text_end, source, review) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (art_id, m["given"], m.get("birth"), m.get("death"), nv[0] if nv else None, msid,
                 branch_ids.get(m.get("branch")), m.get("branch"), m.get("epithet"),
                 json.dumps(m.get("offices") or [], ensure_ascii=False),
                 json.dumps(m.get("kin") or [], ensure_ascii=False), s, e, m["source"], review))
            n_mem[m["source"]] += 1
    if pages_db and pages_db.exists():
        pcon = sqlite3.connect(f"file:{pages_db}?mode=ro", uri=True)
        printed = {(a["volume"], a["pdf_page"]): a.get("printed_page") for a in articles}
        con.executemany("INSERT INTO page_texts VALUES (?,?,?,?)",
                        [(v, p, printed.get((v, p)), t) for v, p, t in pcon.execute(
                            "SELECT volume, page, text FROM pages WHERE mode='columns' AND status='ok'")])
    con.execute("INSERT INTO fts_articles(rowid, headword, article_text) "
                "SELECT id, headword, COALESCE(article_text,'') FROM articles")
    counts = {"articles": len(articles), "members": n_mem, "articles_with_review": n_review}
    meta = {"data_version": version, "built_at": dt.datetime.now().isoformat(timespec="seconds"),
            "source": "DigiBern scans (HBLS I-VIII), page OCR qwen3.8-27b, rebuild/ in thodel/hbls_mcp",
            "counts": json.dumps(counts)}
    con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(json.dumps(counts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--pages-db", default=str(ROOT / "pages.db"))
    a = ap.parse_args()
    arts = [json.loads(ln) for ln in open(a.articles)]
    build(arts, Path(a.out), a.version, Path(a.pages_db))


if __name__ == "__main__":
    main()
