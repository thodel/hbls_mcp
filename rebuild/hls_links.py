"""HBLS ↔ HLS: link HBLS articles (families, places, persons) and family members to HLS articles.

Step 4 of the HBLS rebuild plan. Reads an HBLS database (today's schema: `articles`, `members`; read-only)
and writes the table `article_links` into a separate output database, which the rebuild can take over:

    article_links(hbls_id, hls_id, level, why, kind, article_id, member_n)
    article_link_review(hbls_id, kind, article_id, member_n, why, candidates)   -- ambiguous, not guessed

IDs follow the plan: an article is `hbls-{volume}-{page}-{HEADWORD}`, a member `…#{member_n}` (see member_keys
for numbers that repeat in one article or are missing).
`article_id` is the row id in the input DB, kept for convenience (it is not stable across rebuilds).

HLS candidates come from the HLS MCP server, throttled and cached (sqlite, one row per call):
  - families and places: the title catalogue (`list_articles_by_category` fam/geo, fetched once),
    cantons and time span of a candidate from `get_article` (place_class "Schweiz -> BE", time_span);
  - persons: `search_persons` by family name (one call per surname), matched on given name and life dates.

Levels (one HLS article per HBLS item). Not linked but listed in article_link_review: several candidates that
fit equally well (mehrdeutig), and an exact name whose canton differs (prüfen).
  sicher         family/place: exact name, canton agrees (unique among the candidates)
                 person: given name agrees and birth AND death year agree (±1)
  wahrscheinlich family/place: exact name, single candidate, canton not checkable on one side
                 person: given name agrees and one life year agrees (±1), none contradicts
  möglich        family/place: a near-identical spelling with agreeing canton
                 person: given name agrees and a life year lies within ±5 years, none contradicts
A family whose HBLS years do not overlap the HLS time span drops one level.

    python -m rebuild.hls_links --db hbls.db --out hls_links.db [--cache hls_cache.db] [--kinds fam,geo,bio,member]

The HLS endpoint is $HLS_MCP_URL (default https://tei.dh.unibe.ch/mcp/hls/mcp; on tei
http://127.0.0.1:8004/mcp/hls/mcp). Public access needs $HLS_MCP_AUTH, the full Authorization header value.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

DEFAULT_URL = "https://tei.dh.unibe.ch/mcp/hls/mcp"
LEVELS = ("sicher", "wahrscheinlich", "möglich")

# ── Normalising ──────────────────────────────────────────────────────────────

# OCR of the HBLS headwords writes Ä/Ö as Æ/Œ (STÆGER, MŒRDERBURG)
_OCR_LIGATURES = str.maketrans({"Æ": "Ä", "æ": "ä", "Œ": "Ö", "œ": "ö"})
_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_PARTICLE = re.compile(r"^(von|vom|de|di|da|du|des|della|von der|von|zum|zur|zu)\s+", re.IGNORECASE)


def norm(s: str | None) -> str:
    """Compare form of a name: lower case, ä→ae (HBLS spells both), accents dropped, only letters."""
    s = (s or "").translate(_OCR_LIGATURES).lower().translate(_UMLAUT)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z]", "", s)


def squeeze(text: str | None, n: int | None = None) -> str:
    """Lower-case text without any whitespace: makes the letter-spaced type («K an ton B ern») searchable."""
    t = text or ""
    if n is not None:
        t = t[:n]
    return re.sub(r"\s+", "", t).lower()


# HLS place titles carry a qualifier ("Eschenbach Kloster", "Lenzburg Herrschaft, Amt")
QUALIFIER = re.compile(r"\s+(Gemeinde|Herrschaft|Amtsbezirk|Bezirk|Amt|Landvogtei|Vogtei|Kloster|Stift|Burg|"
                       r"Schloss|Pfarrei|Kanton|Grafschaft|Freiherrschaft|Gericht|Dorf|Weiler|Region|Tal|"
                       r"Kommende|Priorat|Propstei|Abtei|Bistum|Landschaft|Talschaft|Distrikt)\b.*$")


def hls_title_key(title: str) -> tuple[str, str]:
    """(name key, qualifier) of an HLS catalogue title: "Erlach, von" → ("erlach", ""),
    "Eschenbach Kloster" → ("eschenbach", "Kloster"), "Meyer (LU)" → ("meyer", "")."""
    t = re.sub(r"\s*\(.*?\)", "", title or "")
    t = re.sub(r",.*$", "", t)
    m = QUALIFIER.search(t)
    qual = m.group(1) if m else ""
    t = QUALIFIER.sub("", t)
    return norm(_PARTICLE.sub("", t.strip())), qual


# ── Cantons ──────────────────────────────────────────────────────────────────

CANTONS = {  # code: (names after «Kt.», adjectives before «Geschlecht/Familie»)
    "ZH": (["zürich", "zuerich", "zurich", "zürichbiet"], ["zürcher", "zürich"]),
    "BE": (["bern", "berne"], ["berner", "bernisch", "bernois"]),
    "LU": (["luzern", "lucerne"], ["luzerner", "luzernisch"]),
    "UR": (["uri"], ["urner"]),
    "SZ": (["schwyz"], ["schwyzer"]),
    "OW": (["obwalden", "unterwaldenob"], ["obwaldner"]),
    "NW": (["nidwalden", "unterwaldennid"], ["nidwaldner"]),
    "GL": (["glarus"], ["glarner"]),
    "ZG": (["zug"], ["zuger"]),
    "FR": (["freiburg", "fribourg"], ["freiburger", "fribourgeois"]),
    "SO": (["solothurn", "soleure"], ["solothurner"]),
    "BS": (["baselstadt", "basel-stadt", "basel"], ["basler", "baseler"]),
    "BL": (["baselland", "basel-land", "basellandschaft", "basel-landschaft"], ["baselbieter"]),
    "SH": (["schaffhausen"], ["schaffhauser"]),
    "AR": (["appenzella.rh", "appenzellausserrhoden", "appenzell-ausserrhoden", "appenzell"], ["appenzeller"]),
    "AI": (["appenzelli.rh", "appenzellinnerrhoden", "appenzell-innerrhoden"], []),
    "SG": (["st.gallen", "stgallen", "sanktgallen"], ["st.galler", "sanktgaller"]),
    "GR": (["graubünden", "graubuenden", "grisons", "graub."], ["bündner", "graubündner"]),
    "AG": (["aargau", "argovie"], ["aargauer", "aargauisch"]),
    "TG": (["thurgau", "thurgovie"], ["thurgauer", "thurgauisch"]),
    "TI": (["tessin", "ticino"], ["tessiner"]),
    "VD": (["waadt", "vaud", "pays de vaud", "paysdevaud"], ["waadtländer", "vaudois"]),
    "VS": (["wallis", "valais"], ["walliser"]),
    "NE": (["neuenburg", "neuchâtel", "neuchatel"], ["neuenburger", "neuchâtelois"]),
    "GE": (["genf", "genève", "geneve"], ["genfer", "genevois"]),
}
# towns that stand for their canton in «Familie in Davos», «Geschlecht von Sitten»
TOWNS = {
    "ZH": ["winterthur", "eglisau", "regensberg", "grüningen", "kyburg"],
    "BE": ["thun", "biel", "burgdorf", "langenthal", "aarberg", "erlach", "nidau", "büren", "unterseen",
           "interlaken", "laupen", "huttwil", "pruntrut", "porrentruy", "delsberg", "delémont", "neuenstadt",
           "laneuveville", "moutier", "st.immer", "brienz", "frutigen", "saanen", "zweisimmen"],
    "LU": ["sursee", "willisau", "sempach", "beromünster", "münster", "hochdorf", "entlebuch"],
    "UR": ["altdorf", "andermatt", "ursern", "flüelen", "silenen", "bürglen"],
    "SZ": ["einsiedeln", "küssnacht", "lachen", "arth", "steinen", "gersau", "march"],
    "OW": ["sarnen", "kerns", "sachseln", "giswil", "alpnach", "lungern", "engelberg"],
    "NW": ["stans", "buochs", "beckenried", "wolfenschiessen", "hergiswil"],
    "GL": ["näfels", "mollis", "schwanden", "netstal", "ennenda", "linthal"],
    "ZG": ["baar", "cham", "menzingen", "ägeri", "risch", "steinhausen"],
    "FR": ["murten", "morat", "bulle", "estavayer", "romont", "greyerz", "gruyère", "gruyères", "châtel-st.-denis"],
    "SO": ["olten", "grenchen", "balsthal", "dornach"],
    "BL": ["liestal", "sissach", "waldenburg", "läufelfingen"],
    "SH": ["stein a. rh.", "steinamrhein", "neunkirch", "hallau", "thayngen"],
    "AR": ["herisau", "trogen", "teufen", "urnäsch", "gais", "speicher", "heiden"],
    "AI": [],
    "SG": ["rapperswil", "wil", "rorschach", "altstätten", "lichtensteig", "wattwil", "sargans", "walenstadt",
           "uznach", "werdenberg", "toggenburg", "rheintal"],
    "GR": ["chur", "davos", "maienfeld", "ilanz", "thusis", "disentis", "poschiavo", "puschlav", "bergell",
           "engadin", "misox", "mesocco", "roveredo", "zuoz", "samaden", "scanfs", "klosters", "fläsch", "malans"],
    "AG": ["aarau", "baden", "zofingen", "lenzburg", "brugg", "rheinfelden", "bremgarten", "mellingen",
           "laufenburg", "kaiserstuhl", "klingnau", "zurzach", "muri"],
    "TG": ["frauenfeld", "diessenhofen", "bischofszell", "arbon", "weinfelden", "steckborn"],
    "TI": ["bellinzona", "lugano", "locarno", "mendrisio", "airolo", "faido", "biasca", "ascona", "leventina"],
    "VD": ["lausanne", "vevey", "morges", "yverdon", "nyon", "moudon", "payerne", "orbe", "aigle", "rolle",
           "cossonay", "grandson", "avenches", "lutry", "coppet", "montreux"],
    "VS": ["sitten", "sion", "brig", "visp", "leuk", "siders", "sierre", "st.maurice", "martigny", "monthey",
           "naters", "goms", "raron"],
    "NE": ["lelocle", "lachaux-de-fonds", "lachauxdefonds", "boudry", "valangin", "môtiers", "travers",
           "colombier", "landeron"],
    "GE": ["carouge"],
}
_KT = r"(?:kt[es]?\.|kanton(?:e|s)?|kantonen|cant\.|canton)(?:und(?:amt|bez\.)?)?(?:de)?"
_NOUN = r"(?:geschlecht|famili|patrizi|burger|ratsgeschlecht|landleute|adel|freiherren|ministerialen|bürger)"


def cantons_in(text: str | None, n: int = 400) -> set[str]:
    """Cantons named at the head of an HBLS article: «(Kt. Bern, Amtsbez. …», «Familien der Kte. Bern und
    Schwyz», «Berner Geschlecht», «Burgergeschlecht der Stadt Bern». Read on the squeezed text, so letter-spaced
    words count as well."""
    s = squeeze(text, n)
    found = set()
    for code, (names, adjs) in CANTONS.items():
        # the squeezed text has no word ends: only short names need a guard («wil» ≠ «wiler»)
        places = "|".join([re.escape(x) for x in names]
                          + [re.escape(x) + (r"(?![a-zäöü])" if len(x) <= 4 else "") for x in TOWNS.get(code, [])])
        nm = "(?:" + "|".join(re.escape(x) for x in names) + ")"
        pl = "(?:" + places + ")"
        if (re.search(_KT + nm, s)
                or re.search(r"(?:kte\.|kantone)[a-zäöü.,-]{0,40}(?:und|u\.|,)" + nm, s)
                or re.search(r"(?:stadt|landschaft|republik)" + pl, s)
                or re.search(r"\((?:" + places + r")[),]", s)
                or re.search(r"(?:familie|famille|geschlecht|bürger|burger)[a-zäöü]{0,10}"
                             r"(?:in|von|zu|aus|der|des|im|de|du)(?:stadt|landschaft)?" + pl, s)
                or re.search(r"(?:in|zu)(?:" + places + r")(?:eingebürgert|verbürgert|ansässig|verbreitet|"
                             r"niedergelassen)", s)
                or re.search(r"(?:" + places + r")er(?:e|es|s)?" + _NOUN, s)
                or re.search(r"^(?:\(.*?\)\.?)?(?:von|aus)(?:" + places + r")[.,;(]", s)
                or any(re.search(re.escape(adj) + r"(?:e|er|es)?" + _NOUN, s) for adj in adjs)):
            found.add(code)
    if {"BS", "BL"} <= found and not re.search(r"baselstadt|basel-stadt|stadtbasel", s):
        found.discard("BS")  # «Basel-Landschaft» also hits plain «basel»
    return found


def hls_cantons(article: dict) -> set[str]:
    """Cantons of an HLS article from its place classification ("Schweiz -> BE")."""
    out = set()
    for pc in article.get("place_class") or []:
        m = re.match(r"Schweiz -> ([A-Z]{2})\b", pc)
        if m:
            out.add(m.group(1))
    return out


def hls_span(article: dict) -> tuple[int, int] | None:
    """(first, last) year of an HLS article's half-century slots ("1401–1450")."""
    ys = [int(y) for slot in article.get("time_span") or [] for y in re.findall(r"\d{3,4}", slot)]
    return (min(ys), max(ys)) if ys else None


def years_in(text: str | None) -> list[int]:
    """Years 900–1934 mentioned in an HBLS text (the HBLS appeared 1921–1934)."""
    return [int(y) for y in re.findall(r"(?<!\d)(9\d\d|1[0-8]\d\d|19[0-3]\d)(?!\d)", text or "")]


# ── Article types ────────────────────────────────────────────────────────────

# literature abbreviations that the old segmentation took for headwords (plan, step 2)
STOP_HEADWORDS = {"SKL", "SZGL", "LLH", "DHV", "HBLS", "ADB", "ASI", "AHS", "GLS", "QSG", "MAGZ", "JSG", "ASA",
                  "ZSK", "NZZ", "SIEHE", "LL", "DHBS", "AGS", "BSG", "ZT", "MDR", "AS", "ASG", "SBB", "UB",
                  "STAZ", "STAB", "STAL", "STAF", "STAS", "WAPPENBUCH", "NACHTRAG", "NACHTRÆGE", "BERICHTIGUNGEN"}

_GEO_HEAD = re.compile(r"^\(?\s*(?:kte?\.|kanton|cant\.|canton)")
_GEO_START = re.compile(r"^(?:polit|gem\.|gemeinde|dorf|pfarrdorf|kirchdorf|weiler|städtchen|stadt|"
                        r"ortschaft|schloss|burg|burgruine|ruine|kloster|stift|herrschaft|landvogtei|vogtei|"
                        r"bezirk|amtsbez|hof|alp|pass|see|fluss|tal|village|commune|hameau|château)")
# «(VON).», «(FREIE VON).», «(HERREN VON).», «von. Thurgauisches …»: the article is about the family
_FAM_HEAD = re.compile(r"^(?:\((?:freie|herren|grafen|edle|ritter|freiherren)?von\)|von[.,;])")
# «(LANDVOGTEI).», «(SCHLOSS).», «(KLOSTER).»: the place in the qualifier
_GEO_PAREN = re.compile(r"^\((?:landvogtei|vogtei|schloss|burg|kloster|stift|herrschaft|grafschaft|amt|bezirk|"
                        r"amtsbezirk|propstei|priorat|kommende|abtei|gemeinde|dorf)\)")
_FAM = re.compile(r"geschlecht|famili|famille|ratsherrengeschl|patriziergeschl")
_BIO = re.compile(r"\*[^*]{0,25}?\d{4}|\b\d{4}\s*-\s*\d{4}\b")


def article_kind(headword: str | None, text: str | None) -> str:
    """fam / geo / bio / other / skip from the head of the article. Heuristic on purpose: today's DB has no
    reliable types; the rebuild will bring them (step 3) and can pass them in instead."""
    hw = (headword or "").strip().upper()
    if not hw or hw in STOP_HEADWORDS or not re.search(r"[AEIOUYÄÖÜÆŒ]", hw) or not text:
        return "skip"
    s = squeeze(text, 600)
    if re.match(r"^siehe", s):
        return "skip"  # cross reference («Siehe ABERLI»)
    if _FAM_HEAD.match(s):
        return "fam"
    if _GEO_HEAD.match(s) or "gls)" in s[:140] or "s.gls" in s[:140] or _GEO_PAREN.match(s):
        return "geo"
    if _FAM.search(s[:250]):
        return "fam"
    if _GEO_START.match(s):
        return "geo"
    if _BIO.search((text or "")[:200]):
        return "bio"
    if _FAM.search(s):
        return "fam"
    return "other"


# ── HLS client (plain JSON-RPC over streamable HTTP; the server is stateless) ──

class HLS:
    """Throttled, cached tool calls to the HLS MCP server. Every answer is stored by (tool, arguments);
    a re-run makes no network calls for what it has seen. `offline=True` raises on a cache miss (tests)."""

    def __init__(self, cache: str | Path, url: str | None = None, auth: str | None = None,
                 min_interval: float = 0.5, offline: bool = False):
        self.url = url or os.environ.get("HLS_MCP_URL", DEFAULT_URL)
        self.auth = auth if auth is not None else os.environ.get("HLS_MCP_AUTH", "")
        self.min_interval = min_interval
        self.offline = offline
        self._last = 0.0
        self.calls = 0
        self.db = sqlite3.connect(str(cache))
        self.db.execute("CREATE TABLE IF NOT EXISTS calls (tool TEXT, args TEXT, result TEXT, "
                        "fetched TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(tool, args))")

    def call(self, tool: str, **args):
        key = json.dumps(args, sort_keys=True, ensure_ascii=False)
        row = self.db.execute("SELECT result FROM calls WHERE tool=? AND args=?", (tool, key)).fetchone()
        if row:
            return json.loads(row[0])
        if self.offline:
            raise LookupError(f"not cached: {tool} {key}")
        result = self._post(tool, args)
        self.db.execute("INSERT OR REPLACE INTO calls(tool, args, result) VALUES (?,?,?)",
                        (tool, key, json.dumps(result, ensure_ascii=False)))
        self.db.commit()
        return result

    def _post(self, tool: str, args: dict, tries: int = 4):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-06-18"}
        if self.auth:
            headers["Authorization"] = self.auth
        for attempt in range(tries):
            try:
                self._last = time.monotonic()
                self.calls += 1
                req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
                with urllib.request.urlopen(req, timeout=60) as r:
                    return parse_tool_result(r.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == tries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404)):
                    raise
                time.sleep(5 * 2 ** attempt)


def parse_tool_result(raw: str):
    """The tool's result from a JSON or SSE answer: structuredContent (unwrapped from {"result": …}),
    else the JSON in the first text content."""
    msg = None
    if raw.lstrip().startswith("{"):
        msg = json.loads(raw)
    else:
        for line in raw.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:].strip())
                if "result" in msg or "error" in msg:
                    break
    if msg is None:
        raise ValueError("no JSON-RPC message in answer")
    if "error" in msg:
        raise RuntimeError(f"HLS error: {msg['error']}")
    res = msg["result"]
    if res.get("isError"):
        raise RuntimeError(f"HLS tool error: {res.get('content')}")
    payload = res.get("structuredContent")
    if payload is None:
        content = res.get("content") or []
        payload = json.loads(content[0]["text"]) if content else None
    if isinstance(payload, dict) and set(payload) == {"result"}:
        payload = payload["result"]
    return payload


def lexical_qualifier(classes) -> str:
    """Qualifier of an HLS place article from its lexical class, for titles without one."""
    txt = " ".join(classes or [])
    if "Gemeinde" in txt or "Dorf" in txt or "Stadt" in txt:
        return ""
    for word, qual in (("Kloster", "Kloster"), ("Burg", "Burg"), ("Schloss", "Schloss"), ("Herrschaft", "Herrschaft"),
                       ("Vogtei", "Vogtei"), ("Grafschaft", "Grafschaft")):
        if word in txt:
            return qual
    return ""


class Catalogue:
    """HLS titles of one category, by name key."""

    def __init__(self, hls: HLS, category: str, page: int = 500):
        self.by_key: dict[str, list[dict]] = defaultdict(list)
        self.rows: list[dict] = []
        off = 0
        while True:
            rows = hls.call("list_articles_by_category", category=category, limit=page, offset=off) or []
            self.rows += rows
            if len(rows) < page:
                break
            off += page
        for r in self.rows:
            key, qual = hls_title_key(r.get("title", ""))
            if not qual:  # «Feldbach» is the convent: the lexical class says what the title does not
                qual = lexical_qualifier(r.get("lexical_class"))
            r["_key"], r["_qual"] = key, qual
            if key:
                self.by_key[key].append(r)
        self._keys = list(self.by_key)

    def exact(self, key: str) -> list[dict]:
        return list(self.by_key.get(key, []))

    def near(self, key: str, cutoff: float = 0.9) -> list[dict]:
        """Different but near-identical spellings (Kistler/Kistlerr), for the level «möglich» only."""
        if len(key) < 5:
            return []
        hits = difflib.get_close_matches(key, self._keys, n=3, cutoff=cutoff)
        return [r for k in hits if k != key for r in self.by_key[k]]


# ── Matching ─────────────────────────────────────────────────────────────────

def hbls_id(volume, page, headword, member_n=None) -> str:
    hw = re.sub(r"\s+", "_", (headword or "").strip().upper())
    base = f"hbls-{volume}-{page}-{hw}"
    return f"{base}#{member_n}" if member_n is not None else base


def member_keys(members: list[tuple]) -> list[tuple]:
    """Append a member key to each (given, birth, death, member_n), in DB order. The numbers restart in every
    branch of a family article (A. Bern 1., 2. … B. Schwyz 1., 2.) and are sometimes missing: «3» if unique,
    «3.2» for the second member numbered 3, «x1», «x2» … for members without a number."""
    seen: dict = defaultdict(int)
    total: dict = defaultdict(int)
    for m in members:
        total[m[3]] += 1
    out = []
    for m in members:
        n = m[3]
        seen[n] += 1
        if n is None:
            key = f"x{seen[n]}"
        elif total[n] > 1:
            key = f"{n}.{seen[n]}"
        else:
            key = str(n)
        out.append((*m, key))
    return out


def _down(level: str) -> str | None:
    i = LEVELS.index(level)
    return LEVELS[i + 1] if i + 1 < len(LEVELS) else None


SETTLEMENT = re.compile(r"^(?:\(.*?\)\.?)?(?:polit|gem\.|gemeinde|dorf|pfarrdorf|kirchdorf|weiler|städtchen|stadt|"
                        r"ortschaft|village|commune|hameau|zweigem|kirchgem|bezirkshauptort)")


QUAL_GROUP = {"": "", "Gemeinde": "", "Dorf": "", "Weiler": "",
              "Burg": "burg", "Schloss": "burg",
              "Kloster": "kloster", "Stift": "kloster", "Abtei": "kloster", "Propstei": "kloster",
              "Priorat": "kloster", "Kommende": "kloster",
              "Herrschaft": "herrschaft", "Landvogtei": "herrschaft", "Vogtei": "herrschaft", "Amt": "herrschaft",
              "Amtsbezirk": "herrschaft", "Bezirk": "herrschaft", "Grafschaft": "herrschaft",
              "Freiherrschaft": "herrschaft", "Gericht": "herrschaft", "Distrikt": "herrschaft"}


def place_kind(text: str) -> str:
    """'' (a settlement or unknown) or the HLS qualifier the HBLS article is about (Kloster, Burg …)."""
    s = squeeze(text, 200)
    m = _GEO_PAREN.match(s)
    if m:
        s = m.group(0).strip("()")
    else:
        s = re.sub(r"^\(.*?\)\.?", "", s)  # drop «(Kt. Bern, Amtsbez. Erlach. S. GLS).»
    for word, qual in (("kloster", "Kloster"), ("abtei", "Kloster"), ("kommende", "Kommende"), ("stift", "Stift"),
                       ("burgruine", "Burg"), ("burg", "Burg"), ("schloss", "Schloss"),
                       ("herrschaft", "Herrschaft"), ("landvogtei", "Landvogtei"), ("vogtei", "Vogtei"),
                       ("grafschaft", "Grafschaft"), ("amtsbezirk", "Amtsbezirk"), ("amt", "Amt"),
                       ("bezirk", "Bezirk"), ("propstei", "Propstei"), ("priorat", "Priorat")):
        if s.startswith(word):
            return qual
    return ""


def match_named(kind: str, headword: str, text: str, cat: Catalogue, hls: HLS) -> dict:
    """Family or place article → {"level", "hls_id", "why"} or {"level": "mehrdeutig"/None, …}."""
    key = norm(_PARTICLE.sub("", headword.strip()))
    if kind == "fam":
        key = norm(re.sub(r"^(VON|DE|DI|VOM)\s+", "", headword.strip(), flags=re.I))
    hb_cant = cantons_in(text)
    cands = cat.exact(key)
    near = False
    if kind == "geo" and cands:
        want = place_kind(text)
        group = QUAL_GROUP.get(want, want)
        same = [c for c in cands if QUAL_GROUP.get(c["_qual"], c["_qual"]) == group]
        cands = same
    if not cands:
        cands = [c for c in cat.near(key) if kind != "geo" or not c["_qual"]]
        near = True
    if not cands:
        return {"level": None, "why": "kein HLS-Titel"}
    info = []
    for c in cands:
        art = hls.call("get_article", article_id=c["id"]) or {}
        info.append((c, hls_cantons(art), hls_span(art)))
    hb_years = years_in(text)
    hb_span = (min(hb_years), max(hb_years)) if hb_years else None

    def span_ok(span):
        # families only: a place lives on, and its HLS time span says little
        if kind != "fam" or not hb_span or not span:
            return True
        return hb_span[0] <= span[1] + 100 and span[0] <= hb_span[1] + 100

    agree = [x for x in info if hb_cant and x[1] & hb_cant]
    if len(agree) > 1:
        return {"level": "mehrdeutig", "candidates": [x[0]["id"] for x in agree],
                "why": f"mehrere HLS-Artikel «{key}» im Kanton {'/'.join(sorted(hb_cant))}"}
    if len(agree) == 1:
        c, cc, span = agree[0]
        level = "möglich" if near else "sicher"
        why = (f"Name {'ähnlich' if near else 'gleich'} ({c['title']}), Kanton {'/'.join(sorted(cc & hb_cant))}"
               + ("" if len(info) == 1 else f", unter {len(info)} gleichnamigen"))
    elif len(info) == 1 and not near:
        c, cc, span = info[0]
        if hb_cant and cc:
            # in the sample, 3 of 4 such cases were another family or place of the same name: review, not link
            return {"level": "prüfen", "candidates": [c["id"]],
                    "why": f"Name gleich ({c['title']}), Kanton verschieden: HBLS {'/'.join(sorted(hb_cant))}, "
                           f"HLS {'/'.join(sorted(cc))}"}
        else:
            level = "wahrscheinlich"
            why = f"Name gleich ({c['title']}), einziger HLS-Titel, Kanton nicht prüfbar"
    else:
        return {"level": "mehrdeutig" if len(info) > 1 else None,
                "candidates": [x[0]["id"] for x in info],
                "why": (f"{len(info)} HLS-Artikel «{key}», Kanton entscheidet nicht" if len(info) > 1
                        else "nur ähnlicher Name, Kanton nicht bestätigt")}
    if not span_ok(span):
        level = _down(level)
        why += f"; Zeit passt nicht (HBLS {hb_span[0]}–{hb_span[1]}, HLS {span[0]}–{span[1]})"
        if level is None:
            return {"level": None, "why": why}
    return {"level": level, "hls_id": c["id"], "why": why}


def _year(d) -> int | None:
    m = re.match(r"\s*(\d{3,4})", str(d or ""))
    return int(m.group(1)) if m else None


def given_key(given: str | None) -> list[str]:
    """Given names as compare keys; drops OCR junk letters after a name («Hans Jakobf» stays two names)."""
    return [norm(g) for g in re.split(r"[\s\-,]+", given or "") if len(norm(g)) >= 2]


def match_person(given: str | None, birth: int | None, death: int | None, cands: list[dict]) -> dict:
    """One HBLS person (given name, birth and death year) against HLS persons of the same family name."""
    gk = given_key(given)
    if not gk or not (birth or death):
        return {"level": None, "why": "Vorname oder Lebensdaten fehlen"}
    scored = []
    for c in cands:
        ck = given_key(c.get("first_name"))
        if not ck:
            continue
        if gk == ck:
            name_ok = "gleich"
        elif gk[0] == ck[0]:
            name_ok = "erster Vorname gleich"
        elif difflib.SequenceMatcher(None, " ".join(gk), " ".join(ck)).ratio() >= 0.85:
            name_ok = "ähnlich"
        else:
            continue
        hb, hd = _year(c.get("birth_date")), _year(c.get("death_date"))
        pairs = [(a, b) for a, b in ((birth, hb), (death, hd)) if a and b]
        if not pairs:
            continue
        diffs = [abs(a - b) for a, b in pairs]
        if max(diffs) > 5:
            continue  # a contradicting year: another person
        exact = sum(d <= 1 for d in diffs)
        if exact == 2:
            level = "sicher"
        elif exact == 1 and max(diffs) <= 1:
            level = "wahrscheinlich"
        else:
            level = "möglich"
        if name_ok == "ähnlich":
            level = "möglich"
        parts = []
        if birth and hb:
            parts.append(f"* {birth}/{hb}")
        if death and hd:
            parts.append(f"† {death}/{hd}")
        why = f"Vorname {name_ok} ({c.get('first_name')}), " + ", ".join(parts) + " (HBLS/HLS)"
        scored.append((LEVELS.index(level), c, level, why))
    if not scored:
        return {"level": None, "why": "kein HLS-Kandidat mit passendem Vornamen und Daten"}
    scored.sort(key=lambda x: x[0])
    best = [x for x in scored if x[0] == scored[0][0]]
    if len(best) > 1:
        return {"level": "mehrdeutig", "candidates": [x[1].get("article_id") for x in best],
                "why": f"{len(best)} HLS-Personen gleich gut ({best[0][2]})"}
    _, c, level, why = best[0]
    return {"level": level, "hls_id": c.get("article_id") or re.sub(r"^per-", "", c.get("id", "")), "why": why}


def persons_named(hls: HLS, surname: str) -> list[dict]:
    """HLS persons whose family name is the HBLS headword (search_persons also matches given names)."""
    key = norm(_PARTICLE.sub("", surname))
    rows = hls.call("search_persons", query=surname.title(), limit=200) or []
    return [r for r in rows if norm(_PARTICLE.sub("", r.get("family_name") or "")) == key]


def bio_head(text: str) -> tuple[str | None, int | None, int | None]:
    """Given name, birth and death year at the head of an HBLS person article
    («Ch a r l e s , * 1736, t in Orbe … 1809», «Jo h a n n e s , Dr., 1510-1513 …»). The name is what stands
    before the first comma, spaces removed (letter-spaced type), after a variant in parentheses and a
    member number («— 1.»)."""
    t = (text or "")[:250]
    head = re.sub(r"^\W*(?:\(.*?\)\W*)?(?:\d+\.\s*)?", "", t)
    name = head.split(",")[0].strip()
    if re.search(r"\b(?:von|vom|aus|in|zu|der|de|bei|und)\b", name):
        return None, None, None  # «Bürger von Delsberg, Claude» — not a name at the head
    name = re.sub(r"\s+", "", name) if name.replace(" ", "").isupper() else re.sub(r"\s+(?=[a-zäöüéèç])", "", name)
    name = re.sub(r"\s*[IVX]+\.$", "", name).strip()
    ok = re.fullmatch(r"[A-Za-zÄÖÜäöüÉÈéèçÇ'’ -]{2,40}", name) and all(w[:1].isupper() for w in name.split())
    given = name.title() if ok else None
    b = re.search(r"\*[^*†]{0,25}?(\d{4})", t)
    d = re.search(r"(?:†|,\s*[tf]\s)[^†*]{0,25}?(\d{4})", t)  # OCR reads † as t or f
    span = re.search(r"\b(1[0-9]\d\d)\s*-\s*(1[0-9]\d\d)\b", t)
    birth = int(b.group(1)) if b else (int(span.group(1)) if span and not d else None)
    death = int(d.group(1)) if d else (int(span.group(2)) if span and not b else None)
    if birth and death and death < birth:
        death = None
    return given, birth, death


# ── Run ──────────────────────────────────────────────────────────────────────

OUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS article_links (
    hbls_id    TEXT NOT NULL,      -- hbls-{volume}-{page}-{HEADWORD}[#{member_n}]
    hls_id     TEXT NOT NULL,      -- HLS article number, e.g. 023797
    level      TEXT NOT NULL CHECK (level IN ('sicher','wahrscheinlich','möglich')),
    why        TEXT,
    kind       TEXT,               -- fam / geo / bio / member
    article_id INTEGER,            -- row id in the input DB (not stable)
    member_n   INTEGER,
    PRIMARY KEY (hbls_id, hls_id)
);
CREATE TABLE IF NOT EXISTS article_link_review (
    hbls_id    TEXT NOT NULL,
    kind       TEXT,
    article_id INTEGER,
    member_n   INTEGER,
    why        TEXT,
    candidates TEXT                -- JSON list of HLS ids
);
CREATE TABLE IF NOT EXISTS link_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def run(db: str, out: str, hls: HLS, kinds=("fam", "geo", "bio", "member"), limit: int | None = None,
        log=lambda m: print(m, flush=True)) -> dict:
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    dst.executescript(OUT_SCHEMA)
    dst.execute("DELETE FROM article_links")
    dst.execute("DELETE FROM article_link_review")
    cats = {}
    stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    reviewed: set = set()

    def store(kind, hid, aid, mn, res):
        lvl = res.get("level")
        stats[kind][lvl or "kein"] += 1
        if lvl in LEVELS:
            dst.execute("INSERT OR REPLACE INTO article_links VALUES (?,?,?,?,?,?,?)",
                        (hid, res["hls_id"], lvl, res["why"], kind, aid, mn))
        elif lvl in ("mehrdeutig", "prüfen") and (hid, res["why"]) not in reviewed:  # the DB repeats rows
            reviewed.add((hid, res["why"]))
            dst.execute("INSERT INTO article_link_review VALUES (?,?,?,?,?,?)",
                        (hid, kind, aid, mn, res["why"], json.dumps(res.get("candidates", []))))

    rows = src.execute("SELECT id, headword, volume, page, article_text FROM articles ORDER BY id").fetchall()
    if limit:
        rows = rows[:limit]
    members = defaultdict(list)
    if "member" in kinds:
        rows_m = src.execute("SELECT article_id, given, birth_year, death_year, member_n FROM members "
                             "ORDER BY article_id, id").fetchall()
        for r in rows_m:
            members[r[0]].append(r[1:])
        members = {a: member_keys(ms) for a, ms in members.items()}
    n = 0
    for aid, hw, vol, page, text in rows:
        kind = article_kind(hw, text)
        hid = hbls_id(vol, page, hw)
        stats["_types"][kind] += 1
        if kind in ("fam", "geo") and kind in kinds:
            if kind not in cats:
                cats[kind] = Catalogue(hls, kind)
            store(kind, hid, aid, None, match_named(kind, hw, text, cats[kind], hls))
        elif kind == "bio" and "bio" in kinds:
            given, b, d = bio_head(text)
            store("bio", hid, aid, None, match_person(given, b, d, persons_named(hls, hw)))
        if members.get(aid) and "member" in kinds:
            cands = persons_named(hls, hw)
            for given, b, d, mn, mkey in members[aid]:
                store("member", hbls_id(vol, page, hw, mkey), aid, mn, match_person(given, b, d, cands))
        n += 1
        if n % 500 == 0:
            dst.commit()
            log(f"{n}/{len(rows)} Artikel, {hls.calls} HLS-Aufrufe")
    meta = {"input": str(db), "hls_url": hls.url, "built": time.strftime("%Y-%m-%d %H:%M"),
            "stats": json.dumps({k: dict(v) for k, v in stats.items()}, ensure_ascii=False)}
    dst.executemany("INSERT OR REPLACE INTO link_meta VALUES (?,?)", meta.items())
    dst.commit()
    return {k: dict(v) for k, v in stats.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="HBLS database (read only)")
    ap.add_argument("--out", required=True, help="output database with article_links")
    ap.add_argument("--cache", default="hls_cache.db", help="cache of HLS answers")
    ap.add_argument("--kinds", default="fam,geo,bio,member")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--interval", type=float, default=0.5, help="seconds between HLS calls")
    a = ap.parse_args(argv)
    hls = HLS(a.cache, min_interval=a.interval)
    stats = run(a.db, a.out, hls, kinds=tuple(a.kinds.split(",")), limit=a.limit)
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"{hls.calls} HLS-Aufrufe (Rest aus dem Cache)", file=sys.stderr)


if __name__ == "__main__":
    main()
