"""Extend the stoplist from the "Bibliographische Abkürzungen" pages of the eight volumes.

Each volume opens with a list of abbreviations ("SKL = Schweizerisches Künstler-Lexikon"). Every siglum
of that list that is written in capitals is a word the cutter must never take for a headword. The list
pages are found in the page OCR by their title before the first article of the volume.

  python -m rebuild.abbrev            # prints the new sigla and appends them to $HBLS_BUILD/stoplist_extra.txt
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

from .segment import EXTRA_STOPLIST, STOPLIST

_ENTRY = re.compile(r"^\*{0,2}([A-ZÄÖÜ][A-Za-zÄÖÜäöü.]{1,12}?)\*{0,2}\s*(?:=|\s—\s|:)\s", re.M)


def sigla(con: sqlite3.Connection, mode: str = "columns") -> set[str]:
    out: set[str] = set()
    for v in range(1, 9):
        for (text,) in con.execute("SELECT text FROM pages WHERE volume=? AND page<=20 AND mode=? AND status='ok'",
                                   (v, mode)):
            if "ABKÜRZUNGEN" not in (text or "").upper():
                continue
            for m in _ENTRY.finditer(text):
                s = m.group(1).rstrip(".")
                letters = [c for c in s if c.isalpha()]
                if len(letters) >= 2 and sum(c.isupper() for c in letters) / len(letters) >= 0.6:
                    out.add(s)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages-db", default=str(Path.home() / "hbls_build" / "pages.db"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.pages_db}?mode=ro", uri=True)
    new = sorted(sigla(con) - STOPLIST)
    print(len(new), "new sigla:", " ".join(new))
    if new and not a.dry_run:
        with open(EXTRA_STOPLIST, "a") as fh:
            fh.write("".join(f"{s}\n" for s in new))


if __name__ == "__main__":
    main()
