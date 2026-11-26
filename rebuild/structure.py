"""Structure family articles with qwen (rebuild, step 3).

For every article the rule-based reader (rebuild/members.py) finds members in, or that names itself a
family ("Geschlecht", "Familie"), qwen3.8 on GPUStack returns branches and members as JSON under a schema:
number, forename, epithet, birth, death, offices [{office, place, from, to}], kinship [{relation, of_nr,
of_name}], and the first words of the member's text (to anchor it). Long articles go in pieces cut at member
boundaries, so no answer runs into the token limit.

Then rules, and whatever breaks one goes to the review list instead of being taken silently:
- numbers ascend without gaps (26a and the like allowed),
- death not before birth, no life over 105 years, years between 600 and 1934,
- kinship "of_nr" points to a member of the article,
- the anchor is found in the article text,
- every member the rule-based reader saw is in qwen's list (by forename and number), and the other way round.

Results are cached per article (key = stable id + hash of the text + prompt version) in structure.db.

  python -m rebuild.structure --articles articles.jsonl --workers 4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


from .evaluate import key
from .members import find_members

ROOT = Path(os.environ.get("HBLS_BUILD", Path.home() / "hbls_build"))
BASE_URL = os.environ.get("GPUSTACK_BASE_URL", "https://gpustack.unibe.ch/v1")
API_KEY = os.environ.get("GPUSTACK_API_KEY", "")
MODEL = os.environ.get("HBLS_STRUCT_MODEL", "qwen3.8-27b")
PROMPT_VERSION = "s1"
PIECE = 7000

SCHEMA = {
    "type": "object",
    "properties": {
        "branches": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "name": {"type": "string"},
            "place": {"type": ["string", "null"]}, "canton": {"type": ["string", "null"]}},
            "required": ["label", "name", "place", "canton"]}},
        "members": {"type": "array", "items": {"type": "object", "properties": {
            "nr": {"type": ["string", "null"]},
            "given": {"type": "string"},
            "epithet": {"type": ["string", "null"]},
            "birth": {"type": ["integer", "null"]},
            "death": {"type": ["integer", "null"]},
            "branch": {"type": ["string", "null"]},
            "offices": {"type": "array", "items": {"type": "object", "properties": {
                "office": {"type": "string"}, "place": {"type": ["string", "null"]},
                "from": {"type": ["integer", "null"]}, "to": {"type": ["integer", "null"]}},
                "required": ["office", "place", "from", "to"]}},
            "kin": {"type": "array", "items": {"type": "object", "properties": {
                "relation": {"type": "string"}, "of_nr": {"type": ["string", "null"]},
                "of_name": {"type": ["string", "null"]}}, "required": ["relation", "of_nr", "of_name"]}},
            "anchor": {"type": "string"}},
            "required": ["nr", "given", "epithet", "birth", "death", "branch", "offices", "kin", "anchor"]}},
    },
    "required": ["branches", "members"],
}

PROMPT = """Below is {what} of a family article from the Historisch-Biographisches Lexikon der Schweiz (1921-1934). \
Family members are printed in capitals (PETER, HANS JAKOB) or between underscores (_Walter_), often numbered \
("— 3. DAVID, 1776-1850, Kaufmann ..."). Return JSON with:

branches: the lettered or numbered parts of the family ("A. Kanton Bern.", "I. Linie von Mollis", "B. II."): \
label as printed without the full stop ("A", "B.II"), name (the words after the label), place and canton if named.
members: every family member in the text, in order. nr as printed ("3", "26a") or null; given = forename(s) in \
normal case ("Hans Jakob"); epithet = a by-name or title that belongs to the name ("der Ältere", "gen. Schwab") \
or null; birth and death = YEARS of birth and death only (from "* 1853", "† 1916", "1832-1914"); null when not \
printed, when only "urkundlich", "erwähnt" or office years are given, or when the year is "vor ..."; branch = \
the label of the branch the member stands in; offices = offices and functions with place and years (Landvogt \
zu Trachselwald 1452 -> office "Landvogt", place "Trachselwald", from 1452, to null); kin = relations stated in \
the text ("Sohn von Nr. 1" -> relation "son", of_nr "1"; "Bruder des Vorigen" -> relation "brother", of_nr = the \
previous member's nr); anchor = the first 5-8 words of the member's text exactly as in the input, starting \
with the name.
Persons only named in passing (not in capitals) are not members. Literature (SKL, ADB, LL) is not a member. \
Copy, do not invent: every value must be in the text.

{context}Text:
{text}"""

_lock = threading.Lock()


def connect(path: Path = ROOT / "structure.db") -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS structure (aid TEXT PRIMARY KEY, hash TEXT, prompt TEXT, "
                "status TEXT, result TEXT, error TEXT, seconds REAL, updated TEXT)")
    return con


def article_id(a: dict) -> str:
    return f"hbls-{a['volume']}-{a['pdf_page']}-{key(a['headword']).upper()}"


def is_family(a: dict) -> bool:
    if a.get("type") == "verweis":
        return False
    # one member is enough: many families have a single one ("KLÄY … — ALFRED, * 1853"), most of VIII
    return a.get("type") == "fam" or len(find_members(a["text"], a["headword"])) >= 1


def pieces(a: dict) -> list[str]:
    """The article cut at member starts into pieces of at most PIECE characters."""
    text = a["text"]
    if len(text) <= PIECE:
        return [text]
    cuts = [m["start"] for m in find_members(text, a["headword"])]
    out, start = [], 0
    while len(text) - start > PIECE:
        cand = [c for c in cuts if start + 1500 < c <= start + PIECE]
        end = cand[-1] if cand else text.rfind(" ", start, start + PIECE)
        if end <= start:
            end = start + PIECE
        out.append(text[start:end])
        start = end
    out.append(text[start:])
    return out


def ask(client: httpx.Client, prompt: str, attempt: int) -> dict:
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 12000,
            "temperature": 0.2 if attempt else 0, "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": {"name": "family", "schema": SCHEMA}}}
    r = client.post("/chat/completions", json=body)
    r.raise_for_status()
    ch = r.json()["choices"][0]
    if ch.get("finish_reason") == "length":
        raise ValueError("finish_reason length")
    return json.loads(ch["message"]["content"])


def structure_article(client: httpx.Client, a: dict, attempt: int = 0) -> dict:
    parts = pieces(a)
    branches, members = [], []
    for i, part in enumerate(parts):
        what = "the text" if len(parts) == 1 else f"part {i + 1} of {len(parts)}"
        context = f"Family: {a['headword']}.\n"
        if i and branches:
            context += "Branches so far: " + "; ".join(f"{b['label']}. {b['name']}" for b in branches) + "\n"
        if i and members:
            last = members[-1]
            context += f"Last member of the previous part: nr {last.get('nr')}, {last.get('given')}.\n"
        res = ask(client, PROMPT.format(what=what, context=context, text=part), attempt)
        seen = {b["label"] for b in branches}
        branches += [b for b in res.get("branches", []) if b["label"] not in seen]
        members += res.get("members", [])
    return {"branches": branches, "members": members, "pieces": len(parts)}


def nr_value(nr) -> tuple[int, str] | None:
    m = re.match(r"^\s*(\d+)\s*([a-z]?)", str(nr or ""))
    return (int(m.group(1)), m.group(2)) if m else None


def check(a: dict, res: dict) -> list[str]:
    """Rule violations of one structured article (empty = clean)."""
    issues = []
    nrs = [nr_value(m.get("nr")) for m in res["members"]]
    plain = [n for n, s in (x for x in nrs if x) if not s]
    for x, y in zip(plain, plain[1:]):
        if y != x + 1 and y != 1:  # numbering restarts in a new branch
            issues.append(f"numbering {x}->{y}")
    have = {str(m.get("nr")) for m in res["members"] if m.get("nr")}
    norm_text = re.sub(r"[\s*_]+", " ", a["text"])
    for m in res["members"]:
        b, d = m.get("birth"), m.get("death")
        for y in (b, d):
            if y is not None and not 600 <= y <= 1934:
                issues.append(f"year out of range {m['given']} {y}")
        if b and d and (d < b or d - b > 105):
            issues.append(f"life span {m['given']} {b}-{d}")
        for k in m.get("kin", []):
            if k.get("of_nr") and str(k["of_nr"]) not in have:
                issues.append(f"kin of_nr {k['of_nr']} missing ({m['given']})")
        anchor = re.sub(r"[\s*_]+", " ", m.get("anchor") or "").strip()
        if anchor and anchor[:25] not in norm_text and anchor[:25].upper() not in norm_text.upper():
            issues.append(f"anchor not in text: {anchor[:40]}")
    rule = {(key(m["name"]), m["nr"]) for m in find_members(a["text"], a["headword"])}
    llm = {(key(m["given"]), (nr_value(m.get("nr")) or (None,))[0]) for m in res["members"]}
    rule_names, llm_names = {k for k, _ in rule}, {k for k, _ in llm}
    for k, n in sorted(rule, key=str):
        if k not in llm_names or (n is not None and (k, n) not in llm):
            issues.append(f"rule-only member {k} nr {n}")
    for k, n in sorted(llm, key=str):
        if k not in rule_names or (n is not None and (k, n) not in rule):
            issues.append(f"llm-only member {k} nr {n}")
    return issues


def run(articles: list[dict], workers: int, limit: int | None = None, retry: bool = False):
    if not API_KEY:
        sys.exit("GPUSTACK_API_KEY not set")
    con = connect()
    todo = []
    for a in articles:
        if not is_family(a):
            continue
        aid, h = article_id(a), hashlib.sha1(a["text"].encode()).hexdigest()[:16]
        row = con.execute("SELECT hash, prompt, status FROM structure WHERE aid=?", (aid,)).fetchone()
        if row and row[0] == h and row[1] == PROMPT_VERSION and (row[2] == "ok" or not retry):
            continue
        todo.append((a, aid, h))
    todo = todo[:limit] if limit else todo
    print(f"{len(todo)} family articles to structure, {workers} workers, {MODEL}", flush=True)
    import httpx  # only the model run needs it; build and tests do not

    client = httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(900, connect=30),
                          headers={"Authorization": f"Bearer {API_KEY}"})
    t0, done = time.time(), 0

    def job(x):
        a, aid, h = x
        t = time.time()
        for attempt in range(2):
            try:
                res = structure_article(client, a, attempt)
                status, err = "ok", None
                break
            except Exception as e:  # noqa: BLE001
                res, status, err = None, "error", f"{type(e).__name__}: {e}"[:400]
        with _lock:
            con.execute("INSERT OR REPLACE INTO structure VALUES (?,?,?,?,?,?,?,datetime('now'))",
                        (aid, h, PROMPT_VERSION, status, json.dumps(res, ensure_ascii=False) if res else None,
                         err, round(time.time() - t, 1)))
            con.commit()
        return aid, err

    with ThreadPoolExecutor(workers) as ex:
        for fut in as_completed([ex.submit(job, x) for x in todo]):
            aid, err = fut.result()
            done += 1
            if err or done % 25 == 0 or done == len(todo):
                print(f"[{time.strftime('%H:%M:%S')}] {done}/{len(todo)} {done / max(time.time() - t0, 1) * 3600:.0f}/h"
                      + (f" ERROR {aid} {err}" if err else ""), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles", required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="comma-separated headwords")
    ap.add_argument("--retry", action="store_true")
    a = ap.parse_args()
    arts = [json.loads(ln) for ln in open(a.articles)]
    if a.only:
        want = {key(x) for x in a.only.split(",")}
        arts = [x for x in arts if key(x["headword"]) in want]
    run(arts, a.workers, a.limit, a.retry)


if __name__ == "__main__":
    main()
