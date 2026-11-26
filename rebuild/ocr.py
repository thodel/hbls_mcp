"""Page OCR of the eight HBLS volumes with qwen3.8 on GPUStack (rebuild, step 1).

Modelled on ssrq_print/ocr.py (thodel/ssrq_print) and keeps its guards: thinking off,
a timeout on every request, `finish_reason == "length"` and loops mark a page `truncated`,
a retry runs at temperature 0.3 so it does not reproduce the same loop.

What is HBLS-specific is the prompt and the way a page is cut. The scans are 300 dpi
(2059 x 3059 px); a whole page at 200 dpi loses the small petit type of the
literature lines. `--mode columns` sends each of the two columns on its own at
300 dpi (the default), `--mode page` the whole page at 200 dpi as ssrq_print does.

Every (volume, pdf_page, mode) is a row in pages.db; a re-run only does what is not `ok`.

  python -m rebuild.ocr run --volume 4 --pages 510,68 --mode columns --workers 2
  python -m rebuild.ocr run --all --workers 4           # all pages of all volumes
  python -m rebuild.ocr status
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import pymupdf

ROOT = Path(os.environ.get("HBLS_BUILD", Path.home() / "hbls_build"))
PDF_DIR = ROOT / "pdf"
DB_PATH = ROOT / "pages.db"
BASE_URL = os.environ.get("GPUSTACK_BASE_URL", "https://gpustack.unibe.ch/v1")
API_KEY = os.environ.get("GPUSTACK_API_KEY", "")
MODEL = os.environ.get("HBLS_OCR_MODEL", "qwen3.8-27b")
MAX_TOKENS = int(os.environ.get("HBLS_OCR_MAX_TOKENS", "8192"))
TIMEOUT = httpx.Timeout(600.0, connect=30.0)

PROMPT_VERSION = "h4"
PROMPT = """You are transcribing {what} of the "Historisch-Biographisches Lexikon der Schweiz" (HBLS, 1921-1934), a German encyclopedia printed in two columns. Articles on families, persons, places and subjects follow each other alphabetically.

Transcribe exactly what is printed, in reading order{order}. Do not modernise, correct, translate or complete anything.

Layout of the output:
- Write running text WITHOUT the printed line breaks. Join a word hyphenated at the end of a line ("Bürger-
  geschlecht" -> "Bürgergeschlecht"); keep a hyphen that belongs to the word ("Appenzell-Ausserrhoden").
- Start a new line only where the print starts a new paragraph (an indented line): a new article, a lettered or
  numbered branch ("A. Kanton Bern.", "II. Linie"), a new paragraph inside an article.
{header_rule}
Typography, which carries the structure of the lexicon:
- The HEADWORD that opens an article is printed in bold capitals. Write it between double asterisks, exactly as
  printed, followed by what comes after it: "**KISTLER.** Familien der Kte. Bern und Schwyz."
  Cross-references are articles too: "**KLANX.** Siehe CLANX."
- Names of family members are printed in SMALL CAPITALS ("— PETER, Mitglied der GC 1381"). Write them in
  capital letters: PETER, HANS JAKOB, BURKART.
- Letter-spaced text ("W a l t e r", "K a n t o n  B e r n") is written as a normal word without the spaces.
  If it is a person's name, put it between underscores: _Walter_.
- Italic text (titles of books, abbreviations of sources like *SKL*, *ADB*) as *...*.
- Dot leaders (". . . . ." in tables and lists) are written as a single " … ".
- In capitals the print sets Ä and Ö as the ligatures Æ and Œ: write them as Ä and Ö (KLÆGER -> KLÄGER,
  ŒRI -> ÖRI). An A without the ligature stays A (KLANX, KLARER).
- Keep the symbols exactly: † (died), * (born), °, §, ½, Roman numerals, and the dashes " — " between members.
- Keep the bracketed signatures of authors at the end of articles: [H. Br.], [R-r.], [L. S.].

Transcribe every printed word exactly once. Never repeat text, never add anything that is not printed.
Output only the transcription."""

HEADER_PAGE = """- The page starts with the running head (page number and the first and last headword of the page). Write it
  on the first line as: [HEADER] 500 KISTENPASS KLARER
- Mark the start of each column with a line [COL 1] and [COL 2]: the left column completely first, then the right."""
HEADER_COL = """- If the image starts with the running head of the page (page number and headwords above the column), write it
  on the first line as: [HEADER] 500 KISTENPASS"""

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  volume INTEGER NOT NULL, page INTEGER NOT NULL,         -- page = 1-based PDF page (as in pdf_url #page=)
  mode TEXT NOT NULL,                                     -- columns | page
  status TEXT NOT NULL DEFAULT 'pending',                 -- pending | ok | truncated | empty | error
  text TEXT, model TEXT, prompt TEXT, finish TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, seconds REAL,
  error TEXT, attempts INTEGER NOT NULL DEFAULT 0, updated TEXT,
  PRIMARY KEY (volume, page, mode)
);
CREATE INDEX IF NOT EXISTS pages_status ON pages(status);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


_docs: dict[int, pymupdf.Document] = {}


def doc(volume: int) -> pymupdf.Document:
    with _lock:
        if volume not in _docs:
            _docs[volume] = pymupdf.open(PDF_DIR / f"HBLS_band_{volume:02d}.pdf")
        return _docs[volume]


def page_count(volume: int) -> int:
    return doc(volume).page_count


def gutter(page: pymupdf.Page) -> float:
    """x of the gap between the two columns: the emptiest vertical strip of the middle third.

    The scans are not centred (the binding side shifts with recto/verso), so the middle of
    the page is not the gutter. Measured on a 50 dpi grey rendering of the page body."""
    pix = page.get_pixmap(dpi=50, colorspace=pymupdf.csGRAY)
    w, h, s = pix.width, pix.height, pix.samples
    top, bot = int(h * 0.08), int(h * 0.95)
    lo, hi = int(w * 0.35), int(w * 0.65)
    best, best_x = None, w // 2
    for x in range(lo, hi):
        ink = sum(255 - s[y * w + x] for y in range(top, bot, 2))
        if best is None or ink < best:
            best, best_x = ink, x
    return best_x / w * page.rect.width


def images(volume: int, page: int, mode: str) -> list[bytes]:
    with _lock:
        p = doc(volume)[page - 1]
        if mode == "page":
            return [p.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY).tobytes("png")]
        r = p.rect
        g = gutter(p)
        clips = [pymupdf.Rect(0, 0, g + 3, r.height), pymupdf.Rect(g - 3, 0, r.width, r.height)]
        return [p.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY, clip=c).tobytes("png") for c in clips]


_REPEAT = re.compile(r"(.{1,12}?)(?:\s*\1){25,}", re.DOTALL)


def looks_looped(text: str) -> bool:
    if _REPEAT.search(text[-2000:]):
        return True
    lines = Counter(ln.strip() for ln in text.splitlines() if len(ln.strip()) > 40)
    return any(n >= 3 for n in lines.values())


def prompt_for(mode: str, col: int | None = None) -> str:
    if mode == "page":
        return PROMPT.format(what="one printed page", order=" (left column first, then right column)",
                             header_rule=HEADER_PAGE)
    side = "left" if col == 0 else "right"
    return PROMPT.format(what=f"the {side} column of one printed page", order="", header_rule=HEADER_COL)


def call(client: httpx.Client, png: bytes, prompt: str, attempt: int) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3 if attempt else 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = client.post("/chat/completions", json=body)
    r.raise_for_status()
    d = r.json()
    ch = d["choices"][0]
    u = d.get("usage") or {}
    return {"text": (ch["message"].get("content") or "").strip(), "finish": ch.get("finish_reason"),
            "pt": u.get("prompt_tokens") or 0, "ct": u.get("completion_tokens") or 0}


def ocr_page(client: httpx.Client, volume: int, page: int, mode: str, attempt: int = 0) -> dict:
    t0 = time.time()
    parts, finishes, pt, ct = [], [], 0, 0
    for i, png in enumerate(images(volume, page, mode)):
        res = call(client, png, prompt_for(mode, i), attempt)
        text = res["text"]
        if mode == "columns":  # the running head (if read) stays first, then the column marker
            head = ""
            if text.startswith("[HEADER]"):
                head, _, text = text.partition("\n")
                head += "\n"
            text = f"{head}[COL {i + 1}]\n{text}"
        parts.append(text)
        finishes.append(res["finish"])
        pt += res["pt"]
        ct += res["ct"]
    text = "\n".join(parts)
    if not text.strip():
        status = "empty"
    elif "length" in finishes or looks_looped(text):
        status = "truncated"
    else:
        status = "ok"
    return {"status": status, "text": text, "finish": ",".join(f or "" for f in finishes),
            "seconds": round(time.time() - t0, 1), "pt": pt, "ct": ct}


def save(con, volume, page, mode, res=None, err=None):
    with _lock:
        if err:
            con.execute("UPDATE pages SET status='error', error=?, attempts=attempts+1, updated=datetime('now') "
                        "WHERE volume=? AND page=? AND mode=?", (err[:500], volume, page, mode))
        else:
            con.execute(
                "UPDATE pages SET status=?, text=?, finish=?, seconds=?, prompt_tokens=?, completion_tokens=?, "
                "model=?, prompt=?, error=NULL, attempts=attempts+1, updated=datetime('now') "
                "WHERE volume=? AND page=? AND mode=?",
                (res["status"], res["text"], res["finish"], res["seconds"], res["pt"], res["ct"], MODEL,
                 PROMPT_VERSION, volume, page, mode))
        con.commit()


def register(con, items: list[tuple[int, int]], mode: str) -> None:
    con.executemany("INSERT OR IGNORE INTO pages(volume, page, mode) VALUES (?,?,?)",
                    [(v, p, mode) for v, p in items])
    con.commit()


def run(con, items, mode, workers, retry=False, max_attempts=3):
    if not API_KEY:
        sys.exit("GPUSTACK_API_KEY not set")
    register(con, items, mode)
    states = ("error", "truncated", "empty") if retry else ("pending",)
    keys = set(items)
    todo = [(v, p, a) for v, p, a in con.execute(
        f"SELECT volume, page, attempts FROM pages WHERE mode=? AND attempts < ? "
        f"AND status IN ({','.join('?' * len(states))}) ORDER BY volume, page", (mode, max_attempts, *states))
        if (v, p) in keys]
    print(f"{len(todo)} pages to do, {workers} workers, {MODEL}, mode {mode}", flush=True)
    client = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT, headers={"Authorization": f"Bearer {API_KEY}"})
    done, t0 = 0, time.time()

    def job(vpa):
        v, p, a = vpa
        try:
            save(con, v, p, mode, ocr_page(client, v, p, mode, a))
            return v, p, None
        except Exception as e:  # noqa: BLE001 — recorded on the row, retried with --retry
            save(con, v, p, mode, err=f"{type(e).__name__}: {e}")
            return v, p, e

    with ThreadPoolExecutor(workers) as ex:
        for fut in as_completed([ex.submit(job, x) for x in todo]):
            v, p, e = fut.result()
            done += 1
            if e or done % 10 == 0 or done == len(todo):
                rate = done / max(time.time() - t0, 1) * 3600
                print(f"[{time.strftime('%H:%M:%S')}] {done}/{len(todo)} {rate:.0f} p/h"
                      + (f"  ERROR {v}:{p} {e}" if e else ""), flush=True)


def status(con):
    for row in con.execute("SELECT mode, status, COUNT(*), ROUND(AVG(seconds),1), SUM(completion_tokens) "
                           "FROM pages GROUP BY 1, 2"):
        print(*row, sep="\t")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--volume", type=int)
    r.add_argument("--pages", help="comma-separated 1-based PDF pages (with --volume)")
    r.add_argument("--list", help="file with 'volume page' per line")
    r.add_argument("--all", action="store_true")
    r.add_argument("--mode", choices=("columns", "page"), default="columns")
    r.add_argument("--workers", type=int, default=2)
    r.add_argument("--retry", action="store_true")
    a = ap.parse_args()
    con = connect()
    if a.cmd == "status":
        return status(con)
    if a.list:
        items = [tuple(int(x) for x in ln.split()[:2]) for ln in Path(a.list).read_text().splitlines() if ln.strip()]
    elif a.all:
        items = [(v, p) for v in range(1, 9) for p in range(1, page_count(v) + 1)]
    else:
        items = [(a.volume, int(p)) for p in a.pages.split(",")]
    run(con, items, a.mode, a.workers, a.retry)


if __name__ == "__main__":
    main()
