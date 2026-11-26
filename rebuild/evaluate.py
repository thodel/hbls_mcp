"""Measure an HBLS text/database against the gold standard (rebuild, step 0).

Three numbers per system, over the 30 gold pages (gold/README.md):

- CER: each gold quarter (`cer_text`) is aligned semi-globally inside the system's text of that page
  (the gold must be matched in full, the page text around it is free), edit distance / gold length.
  Both sides are normalised first: markup and the [HEADER]/[COL] lines removed, line-end hyphens joined,
  whitespace collapsed, case folded (small capitals are capitals in one reading and not in another),
  dashes and quotes unified. Reading order counts: a quarter whose lines are interleaved with the other
  column costs what it costs.
- Headword P/R/F1: the articles that START on a gold page against the system's articles on that page,
  headwords compared without case, spaces, punctuation and diacritics (Æ = Ä = AE).
- Member P/R/F1: for every gold family article, the members printed on the page against the system's
  members of the article with that headword (same volume, page within the 8 preceding pages, for an article
  continued from there). Matched by forename (multiset); `+nr` also requires the same number. `dates` is
  the share of matched members whose birth and death years both agree.

Systems:
  live    the database as served (`articles`/`members`, page = PDF page), text = the PDF text layer the
          database was cut from
  ocr     the page OCR in pages.db (`--mode columns|page`), text only
  db:PATH a database built by the rebuild (same tables plus the new ones)

  python -m rebuild.evaluate --system live --db /home/tobias/mcp/data/hbls.db
  python -m rebuild.evaluate --system ocr
  python -m rebuild.evaluate --system db --db hbls.staging.db --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
GOLD = HERE / "gold"


# ── normalisation ────────────────────────────────────────────────────────────

def fold(s: str) -> str:
    s = s.replace("Æ", "AE").replace("æ", "ae").replace("Œ", "OE").replace("œ", "oe")
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("Ä", "AE").replace("Ö", "OE").replace("Ü", "UE")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def key(s: str | None) -> str:
    return re.sub(r"[^a-z]", "", fold(s or "").casefold())


def lemma(headword: str | None) -> str:
    """The headword without what qualifies it — "STEINMAUR (HERREN VON)", "HALLAU, VON", "TRUNS oder TRONS",
    "STEINMAR, BERTHOLD" all count as their first word(s). Where print, gold and OCR draw the end of a
    headword differently, the article is still the same; homonyms on a page are counted as a multiset."""
    h = re.split(r"\s*[,(]|\s+oder\s+", headword or "", maxsplit=1)[0]
    return key(h)


_DASH = str.maketrans({"–": "-", "—": "-", "‒": "-", "―": "-", "«": '"', "»": '"', "„": '"', "“": '"', "”": '"',
                       "‘": "'", "’": "'", "\xa0": " "})


def norm_text(s: str) -> str:
    s = re.sub(r"^\[(HEADER|COL \d)\].*$", " ", s, flags=re.M)
    s = s.replace("\xad\n", "").replace("\xad", "")
    s = re.sub(r"(\w)-\n(\w)", r"\1\2", s)
    s = s.replace("**", "").replace("_", "").replace("*", "\x00")  # keep the birth star: "* 1853"
    s = re.sub(r"\x00(?=\s*\d)", "*", s).replace("\x00", "")
    s = s.translate(_DASH)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*([,;:.)\]])", r"\1", s)    # the print sets "Wappen :", OCR writes "Wappen:"
    s = re.sub(r"([(\[])\s*", r"\1", s)
    return unicodedata.normalize("NFC", s).casefold().strip()


def semiglobal(gold: str, hyp: str) -> int:
    """Edit distance of `gold` against the best-matching substring of `hyp` (free end gaps in hyp)."""
    a = np.frombuffer(gold.encode("utf-32-le"), dtype=np.uint32)
    b = np.frombuffer(hyp.encode("utf-32-le"), dtype=np.uint32)
    m = len(b)
    prev = np.zeros(m + 1, dtype=np.int32)          # row 0: starting anywhere in hyp is free
    idx = np.arange(m + 1, dtype=np.int32)
    for i, ch in enumerate(a, 1):
        sub = prev[:-1] + (b != ch)
        cur = np.empty(m + 1, dtype=np.int32)
        cur[0] = i
        cur[1:] = np.minimum(sub, prev[1:] + 1)      # substitution/match, deletion of a gold char
        cur = np.minimum.accumulate(cur - idx) + idx  # insertion of hyp chars
        prev = cur
    return int(prev.min())


def prf(tp: int, n_pred: int, n_gold: int) -> dict:
    p = tp / n_pred if n_pred else 0.0
    r = tp / n_gold if n_gold else 0.0
    return {"p": round(p, 3), "r": round(r, 3), "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0,
            "tp": tp, "pred": n_pred, "gold": n_gold}


# ── systems ──────────────────────────────────────────────────────────────────

class System:
    def page_text(self, v: int, p: int) -> str | None: ...
    def headwords(self, v: int, p: int) -> list[str] | None: ...
    def members(self, v: int, p: int, headword: str) -> list[dict] | None: ...


class DbSystem(System):
    """`articles(headword, volume, page, …)` + `members(article_id, given, birth_year, death_year, member_n)`."""

    def __init__(self, db: str, pdf_dir: Path | None = None, text_from_layer: bool = False):
        self.con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        self.pdf_dir, self.layer = pdf_dir, text_from_layer
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(articles)")}
        self.has_pdf_page = "pdf_page" in cols

    def _page_col(self):
        return "pdf_page" if self.has_pdf_page else "page"

    def page_text(self, v, p):
        if self.layer:
            import pymupdf
            return pymupdf.open(self.pdf_dir / f"HBLS_band_{v:02d}.pdf")[p - 1].get_text()
        if "page_texts" in {r[0] for r in self.con.execute("SELECT name FROM sqlite_master")}:
            row = self.con.execute("SELECT text FROM page_texts WHERE volume=? AND pdf_page=?", (v, p)).fetchone()
            return row[0] if row else None
        return None

    def headwords(self, v, p):
        return [r[0] for r in self.con.execute(
            f"SELECT headword FROM articles WHERE volume=? AND {self._page_col()}=?", (v, p))]

    def members(self, v, p, headword):
        k = key(headword)
        col = self._page_col()
        alt = lemma(headword)
        ids = [i for i, hw in self.con.execute(
            f"SELECT id, headword FROM articles WHERE volume=? AND {col} BETWEEN ? AND ?", (v, p - 8, p))
            if key(hw) == k] or [i for i, hw in self.con.execute(
            f"SELECT id, headword FROM articles WHERE volume=? AND {col} BETWEEN ? AND ?", (v, p - 8, p))
            if lemma(hw) == alt]
        out = []
        for i in ids:
            out += [{"name": g, "birth": b, "death": d, "nr": n} for g, b, d, n in self.con.execute(
                "SELECT given, birth_year, death_year, member_n FROM members WHERE article_id=?", (i,))]
        return out


class OcrSystem(System):
    def __init__(self, pages_db: str, mode: str):
        self.con = sqlite3.connect(f"file:{pages_db}?mode=ro", uri=True)
        self.mode = mode

    def page_text(self, v, p):
        row = self.con.execute("SELECT text FROM pages WHERE volume=? AND page=? AND mode=? AND status='ok'",
                               (v, p, self.mode)).fetchone()
        return row[0] if row else None

    def headwords(self, v, p):
        t = self.page_text(v, p)
        if t is None:
            return None
        return [m.group(1) for m in re.finditer(r"^(?:\*\s+)?\*\*(.+?)\*\*", t, flags=re.M)]

    def members(self, v, p, headword):
        return None


# ── scoring ──────────────────────────────────────────────────────────────────

# The transcribed quarters (cer_text) are HBLS text and stay out of the public repository until the
# rights are settled; they lie next to the build data (idhefix ~/hbls_build/gold_text/v4_p510.txt).
GOLD_TEXT = Path(os.environ.get("HBLS_GOLD_TEXT", Path.home() / "hbls_build" / "gold_text"))


def gold_pages() -> list[dict]:
    out = []
    for f in sorted(GOLD.glob("v*_p*.json")):
        g = json.loads(f.read_text())
        t = GOLD_TEXT / f"{f.stem}.txt"
        if "cer_text" not in g and t.exists():
            g["cer_text"] = t.read_text()
        out.append(g)
    return out


def name_key(n: str | None) -> str:
    """A forename without ordinals and brackets: "Johann I." = "Johann", "Egbrecht (III.)" = "Egbrecht"."""
    n = re.sub(r"\([^)]*\)", " ", n or "")
    n = re.sub(r"\b[IVX]+\.?(?=\s|$)", " ", n)
    return key(n)


def evaluate(sys_: System) -> dict:
    pages = gold_pages()
    cer_err = cer_len = 0
    per_page = []
    hw_tp = hw_pred = hw_gold = 0
    m_tp = m_tpnr = m_pred = m_gold = m_dates = 0
    for g in pages:
        v, p = g["volume"], g["pdf_page"]
        row = {"page": f"{v}/{p}"}
        text = sys_.page_text(v, p)
        if text is not None and g.get("cer_text"):
            gt = norm_text(g["cer_text"])
            d = semiglobal(gt, norm_text(text))
            cer_err += d
            cer_len += len(gt)
            row["cer"] = round(d / len(gt), 4)
        hws = sys_.headwords(v, p)
        gold_hw = Counter(lemma(a["headword"]) for a in g["articles"] if not a.get("continued"))
        if hws is not None:
            pred = Counter(lemma(h) for h in hws)
            tp = sum((pred & gold_hw).values())
            hw_tp += tp
            hw_pred += sum(pred.values())
            hw_gold += sum(gold_hw.values())
            row["hw"] = f"{tp}/{sum(gold_hw.values())} (pred {sum(pred.values())})"
            row["hw_missing"] = sorted((gold_hw - pred).elements())
            row["hw_extra"] = sorted((pred - gold_hw).elements())
        per_page.append(row)
    # members, per article over all its gold pages: an article that starts and ends on gold pages is
    # complete and scores precision and recall; one that runs off the gold pages scores recall only
    groups: list[dict] = []
    open_: dict[tuple, dict] = {}
    for g in pages:
        v, p = g["volume"], g["pdf_page"]
        for a in g["articles"]:
            if a["type"] != "fam" and not a.get("members"):
                continue
            k = (v, lemma(a["headword"]))
            grp = open_.get(k) if a.get("continued") else None
            if grp is None or grp["last"] < p - 1:
                if a.get("continued"):
                    grp = {"v": v, "headword": a["headword"], "page": p, "members": [], "complete": False}
                else:
                    grp = {"v": v, "headword": a["headword"], "page": p, "members": [], "complete": True}
                groups.append(grp)
                open_[k] = grp
            grp["last"] = p
            grp["members"] += a.get("members") or []
            grp["ends"] = a.get("end") is not None
    for grp in groups:
        complete = grp["complete"] and grp.get("ends")
        pm = sys_.members(grp["v"], grp["page"], grp["headword"])
        if pm is None:
            continue
        gm = grp["members"]
        gk = Counter(name_key(m["name"]) for m in gm)
        pk = Counter(name_key(m["name"]) for m in pm)
        tp = sum((gk & pk).values())
        m_tp += tp
        m_gold += sum(gk.values())
        m_pred += sum(pk.values()) if complete else tp
        gnr = Counter((name_key(m["name"]), str(m.get("nr"))) for m in gm if m.get("nr") is not None)
        pnr = Counter((name_key(m["name"]), str(m.get("nr"))) for m in pm)
        unnumbered = sum((Counter(name_key(m["name"]) for m in gm if m.get("nr") is None) & pk).values())
        m_tpnr += min(sum((gnr & pnr).values()) + unnumbered, tp)
        pool = list(pm)
        for m in gm:
            for q in pool:
                if name_key(q["name"]) == name_key(m["name"]) and (m.get("nr") is None or str(q.get("nr")) in (str(m.get("nr")), "None")):
                    pool.remove(q)
                    m_dates += (q.get("birth") == m.get("birth")) and (q.get("death") == m.get("death"))
                    break
    res = {"pages": len(pages),
           "cer": round(cer_err / cer_len, 4) if cer_len else None, "cer_chars": cer_len,
           "headwords": prf(hw_tp, hw_pred, hw_gold) if hw_gold else None,
           "members": prf(m_tp, m_pred, m_gold) if m_gold and m_pred + m_tp else None,
           "members_nr": prf(m_tpnr, m_pred, m_gold) if m_gold and m_pred + m_tp else None,
           "member_dates_ok": round(m_dates / m_tp, 3) if m_tp else None,
           "member_articles": len(groups),
           "per_page": per_page}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", required=True, choices=("live", "ocr", "db"))
    ap.add_argument("--db")
    ap.add_argument("--pages-db", default=str(Path.home() / "hbls_build" / "pages.db"))
    ap.add_argument("--pdf-dir", default=str(Path.home() / "hbls_build" / "pdf"))
    ap.add_argument("--mode", default="columns")
    ap.add_argument("--json")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if a.system == "live":
        s = DbSystem(a.db, Path(a.pdf_dir), text_from_layer=True)
    elif a.system == "ocr":
        s = OcrSystem(a.pages_db, a.mode)
    else:
        s = DbSystem(a.db)
    res = evaluate(s)
    if a.verbose:
        for r in res["per_page"]:
            print(r)
    print(json.dumps({k: v for k, v in res.items() if k != "per_page"}, ensure_ascii=False, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
