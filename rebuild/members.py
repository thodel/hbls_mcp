"""Members of a family article read from the OCR markup (rebuild, step 3, rule-based part).

The print sets family members in small capitals; the OCR writes them in capitals ("— 3. DAVID, 1776-1850,
Kaufmann …"), a letter-spaced bold one between underscores ("— _Walter_, von Hundwil"). Literature
abbreviations are italic (`*SKL*`) and are removed before reading. This gives the members with number,
forename, years and the span of their text; rebuild/structure.py asks qwen for branches, offices and
kinship and is checked against this list.
"""
from __future__ import annotations

import re

from .segment import STOPLIST

# italic *SKL*, never the birth star "* 1853" (followed by a space or a digit)
_ITALIC = re.compile(r"(?<![*\w])\*(?![\s\d*])([^*\n]{1,120}?)(?<!\s)\*(?!\*)")
_UP = "A-ZÄÖÜÆŒÉÈ"
_WORD = rf"[{_UP}][{_UP}'\-]+"
_NAME = rf"{_WORD}(?:\s+(?:{_WORD}|von|de|v\.)){{0,3}}"
# a member opens after a dash, at a paragraph start or after a branch label, optionally numbered
_MEMBER = re.compile(
    rf"(?:(?<=—)|(?<=^)|(?<=\n)|(?<=\. )|(?<=; ))\s*(?:(\d{{1,3}})\s*[.,]?\s+)?"
    rf"(?:_([{_UP}][^_\n]{{1,40}}?)_|({_NAME}))\s*(?=[,(]|\s+(?:von|aus|zu|in|\*|†|\d))")
_ROMAN = re.compile(r"[IVXLC]+\.?")
_BRANCH = re.compile(r"(?:^|\n|— )\s*((?:[A-H]|[IVX]{1,4})\.)\s+(?=[A-ZÄÖÜ])")
_YEAR = r"\b(1[0-9]{3}|[4-9][0-9]{2})\b"
_DMY = rf"(?:\d{{1,2}}\.\s*[ivxIVX]{{1,4}}\.?\s*)?{_YEAR}"


def strip_italic(text: str) -> str:
    return _ITALIC.sub(lambda m: " " * (len(m.group(0))), text)


def proper(name: str) -> str:
    return " ".join(w if w in ("von", "de", "v.") else "-".join(p.capitalize() for p in w.split("-"))
                    for w in name.split())


def years(seg: str) -> tuple[int | None, int | None]:
    """Birth and death years from the first ~140 characters of a member's text."""
    s = seg[:140]
    birth = death = None
    m = re.search(rf"\*\s*(?:um\s+|ca\.\s+)?{_DMY}", s)
    if m:
        birth = int(m.group(1))
    m = re.search(rf"†\s*(?:um\s+|ca\.\s+|nach\s+|vor\s+)?(?:[\w. ]{{0,30}}?)?{_DMY}", s)
    if m:
        death = int(m.group(1))
    if birth is None and death is None:  # "1832-1914" / "27. II. 1499-1567" right after the name
        m = re.match(rf"^[,\s]*(?:Dr\.[\w. ]{{0,12}},?\s*)?(?:[\w. ]{{0,20}}?\s)?{_DMY}\s*[-–]\s*(?:\d{{1,2}}\.\s*[ivxIVX]{{1,4}}\.?\s*)?(\d{{2,4}})\b", s)
        if m:
            birth, d = int(m.group(1)), m.group(2)
            death = int(d) if len(d) == 4 else int(str(birth)[:4 - len(d)] + d)
            if death < birth:
                death = None
    return birth, death


def find_members(text: str, headword: str) -> list[dict]:
    t = strip_italic(text)
    # skip the headword and its parenthesis of name variants: "**KLÆN** (KLEEN, KLEIN)."
    start = 0
    m = re.match(r"^\*\*.+?\*\*[^.]*?(?:\([^)]*\))?[.,]?", t)
    if m:
        start = m.end()
    hw_words = {w for w in re.split(r"[\s,]+", headword.upper()) if w}
    branches = [(b.start(1), b.group(1).rstrip(".")) for b in _BRANCH.finditer(t)]
    out = []
    found = list(_MEMBER.finditer(t, start))
    for j, m in enumerate(found):
        nr, spaced, caps = m.group(1), m.group(2), m.group(3)
        name = spaced or caps
        if not name:
            continue
        name = name.strip()
        words = name.split()
        if caps:
            if name in STOPLIST or words[0] in STOPLIST or len(words[0]) < 3:
                continue
            if all(_ROMAN.fullmatch(w) for w in words):  # "XXV", "III": volume and part numbers
                continue
            if t.rfind("[", 0, m.start()) > t.rfind("]", 0, m.start()):  # an author signature "[J. MÜLLER, A.]"
                continue
            if set(words) <= hw_words:  # the family name itself ("KLAARER, CLARER")
                continue
        nxt = found[j + 1].start() if j + 1 < len(found) else len(t)
        b, d = years(t[m.end():nxt])  # the years of this member, never of the next one
        branch = None
        for pos, lab in branches:
            if pos < m.start():
                branch = lab if re.fullmatch(r"[A-H]", lab) or branch is None else \
                    (branch.split(".")[0] + "." + lab if re.fullmatch(r"[A-H]", branch.split(".")[0]) else lab)
        out.append({"nr": int(nr) if nr else None, "name": proper(name) if caps else name, "birth": b,
                    "death": d, "branch": branch, "start": m.start(), "spaced": bool(spaced)})
    for i, mem in enumerate(out):
        mem["end"] = out[i + 1]["start"] if i + 1 < len(out) else len(text)
    return out
