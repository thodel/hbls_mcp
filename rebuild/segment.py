"""Cut the page OCR into articles (rebuild, step 2).

The OCR marks a headword as `**HEADWORD**` at the start of a paragraph (rebuild/ocr.py). A paragraph
that starts a page or column without one continues the article before it, across columns and pages.
A headword is accepted when it is mostly capitals and not a literature abbreviation (stoplist; the
bibliographic abbreviations of the volumes, rebuild/stoplist.txt). Headwords out of alphabetical
order are kept but listed for review (`flags`), never dropped silently.

Output: one dict per article — volume, pdf_page, printed_page, column, headword, text, type (verweis or
a first guess fam/bio/geo/tem, refined by rebuild/structure.py), target (for a verweis), flags.

  python -m rebuild.segment --out articles.jsonl [--mode columns] [--volume 4]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

from .evaluate import key

HERE = Path(__file__).parent
# the seed list in the repo, plus the sigla rebuild/abbrev.py read from the volumes (kept with the build data)
EXTRA_STOPLIST = Path(os.environ.get("HBLS_BUILD", Path.home() / "hbls_build")) / "stoplist_extra.txt"
STOPLIST = {ln.split("#")[0].strip() for f in (HERE / "stoplist.txt", EXTRA_STOPLIST) if f.exists()
            for ln in f.read_text().splitlines()} - {""}

# "**KISTLER.** Familien …"; the supplement (VIII) marks new articles with a leading star: "* **HOFER.** — …"
_HEAD = re.compile(r"^(?:\*\s+)?\*\*(.+?)\*\*(.*)$")
# what follows the bold part and still belongs to the headword: "**HALLAU**, von." "**KISSLING**, RICHARD, Bildhauer"
_PARTICLE = r"(?:von|vom|der|de|des|di|du|la|le|zur|zum|VON|VOM|DER|DE|DES|DI|DU|LA|LE|ZUR|ZUM)"
_SUFFIX = re.compile(rf"^\s*,\s*((?:{_PARTICLE}|[A-ZÄÖÜÆŒ][A-ZÄÖÜÆŒ\-]+)(?:\s+(?:{_PARTICLE}|[A-ZÄÖÜÆŒ][A-ZÄÖÜÆŒ\-]+))*)(?=[\s.,;(]|$)")
_HEADER = re.compile(r"^\[HEADER\]\s*(.*)$")
_COL = re.compile(r"^\[COL \d\]\s*$")
_RUNNING = re.compile(r"^[A-ZÄÖÜÆŒ0-9][A-ZÄÖÜÆŒ0-9 ,.'’()\-]{1,60}$")  # a stray running head line


def printed_number(header: str) -> int | None:
    nums = [int(x) for x in re.findall(r"\b(\d{1,4})\b", header)]
    return nums[0] if nums else None


def caps_share(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    return sum(c.isupper() for c in letters) / len(letters) if letters else 0.0


def clean_headword(h: str) -> str:
    h = h.strip().rstrip(".,:;").strip()
    return re.sub(r"\s+", " ", h)


def is_headword(h: str) -> bool:
    hw = clean_headword(h)
    if not hw or len(hw) > 80 or caps_share(hw.split(",")[0]) < 0.8:
        return False
    return hw not in STOPLIST and hw.replace(".", "") not in STOPLIST


def page_lines(text: str) -> tuple[int | None, list[tuple[int, str]]]:
    """Printed page number and the paragraph lines of one page as (column, line)."""
    printed, col, out, first_in_col = None, 1, [], True
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = _HEADER.match(ln)
        if m:
            printed = printed if printed is not None else printed_number(m.group(1))
            continue
        if _COL.match(ln):
            col = int(ln[5])
            first_in_col = True
            continue
        if first_in_col and _RUNNING.match(ln) and not ln.startswith("**") and len(ln.split()) <= 4:
            n = printed_number(ln)
            printed = printed if printed is not None else n
            first_in_col = False
            continue
        first_in_col = False
        out.append((col, ln))
    return printed, out


def join(prev: str, nxt: str) -> str:
    if prev.endswith("-") and nxt[:1].islower():
        return prev[:-1] + nxt
    return prev + " " + nxt


_SIEHE = re.compile(r"^[^.]{0,120}?\bSiehe\s+(?:auch\s+)?([A-ZÄÖÜÆŒ][\wÄÖÜÆŒäöüæœ' \-]+?)(?:[.,;]|$)")
_GEO = re.compile(r"\b(Gem\.|Gemeinde|Dorf|Pfarrdorf|Weiler|Bez\.|Bezirk|Burg|Burgruine|Schloss|Kloster|Fluss|"
                  r"Berg|Pass|See|Alp|Tal|Stadt|Hauptort|Kanton|Kreis|Herrschaft|Landvogtei|Vogtei|Ortschaft)\b|GLS")
_FAM = re.compile(r"\b(Geschlecht|Geschlechter|Familie|Familien|Bürgergeschlecht|Burgergeschlecht)\b")
_BIOSTART = re.compile(r"^\s*[,]?\s*(?:[A-ZÄÖÜ][a-zäöüé]+|[A-ZÄÖÜ]{2,})\b.{0,60}?[*†]\s*\d{3,4}")


def guess_type(headword: str, text: str) -> tuple[str, str | None]:
    m = re.match(r"^(?:\*\s+)?\*\*.+?\*\*", text)
    body = text[m.end():] if m else text
    head = re.sub(r"\*", "", body[:220])
    m = _SIEHE.match(head)
    if m and len(body) < 160:
        return "verweis", clean_headword(m.group(1)).upper()
    if _FAM.search(head[:160]):
        return "fam", None
    if "," in headword or _BIOSTART.match(head):
        return "bio", None
    if _GEO.search(head[:160]):
        return "geo", None
    return "tem", None


def lis_flags(keys: list[str]) -> set[int]:
    """Indices outside the longest non-decreasing run of two-letter prefixes (alphabetical order)."""
    pre = [k[:2] for k in keys]
    n = len(pre)
    import bisect
    tails, tails_i, parent = [], [], [-1] * n
    for i, x in enumerate(pre):
        j = bisect.bisect_right(tails, x)
        if j == len(tails):
            tails.append(x)
            tails_i.append(i)
        else:
            tails[j] = x
            tails_i[j] = i
        parent[i] = tails_i[j - 1] if j else -1
    keep, i = set(), tails_i[-1] if tails_i else -1
    while i >= 0:
        keep.add(i)
        i = parent[i]
    return set(range(n)) - keep


def segment_volume(con: sqlite3.Connection, volume: int, mode: str) -> list[dict]:
    rows = con.execute("SELECT page, text FROM pages WHERE volume=? AND mode=? AND status='ok' ORDER BY page",
                       (volume, mode)).fetchall()
    arts: list[dict] = []
    cur: dict | None = None
    last_printed, last_pdf = None, None
    prev_pdf = None
    for pdf_page, text in rows:
        if prev_pdf is not None and pdf_page != prev_pdf + 1:
            cur = None  # a gap (pilot, missing page): the text at the top belongs to an article we do not have
        prev_pdf = pdf_page
        printed, lines = page_lines(text or "")
        if printed is None and last_printed is not None and last_pdf is not None:
            printed = last_printed + (pdf_page - last_pdf)  # plates have no number; text pages count on
        if printed is not None:
            last_printed, last_pdf = printed, pdf_page
        for i, (col, ln) in enumerate(lines):
            m = _HEAD.match(ln)
            if m and is_headword(m.group(1)):
                hw = clean_headword(m.group(1))
                sm = _SUFFIX.match(m.group(2)) if "," not in hw else None
                if sm:
                    hw = f"{hw}, {sm.group(1).upper()}"
                cur = {"volume": volume, "pdf_page": pdf_page, "printed_page": printed, "column": col,
                       "headword": hw, "text": ln, "flags": []}
                arts.append(cur)
                continue
            if cur is None:  # front matter before the first article
                continue
            if m:  # bold but not a headword (stoplist, lower case): keep the text, note it
                cur["flags"].append(f"bold-not-headword:{clean_headword(m.group(1))}")
            if i == 0 or (i > 0 and lines[i - 1][0] != col):
                cur["text"] = join(cur["text"], ln)  # continuation over a page or column break
            else:
                cur["text"] += "\n" + ln
    keys = [key(a["headword"]) for a in arts]
    for i in lis_flags(keys):
        arts[i]["flags"].append("out-of-order")
    for a in arts:
        a["type"], a["target"] = guess_type(a["headword"], a["text"])
    return arts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages-db", default=str(Path.home() / "hbls_build" / "pages.db"))
    ap.add_argument("--mode", default="columns")
    ap.add_argument("--volume", type=int)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.pages_db}?mode=ro", uri=True)
    vols = [a.volume] if a.volume else range(1, 9)
    n = 0
    with open(a.out, "w") as fh:
        for v in vols:
            for art in segment_volume(con, v, a.mode):
                fh.write(json.dumps(art, ensure_ascii=False) + "\n")
                n += 1
    print(f"{n} articles -> {a.out}")


if __name__ == "__main__":
    main()
