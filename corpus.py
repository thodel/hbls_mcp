#!/usr/bin/env python3
"""
corpus.py — what makes this repo the HBLS rather than one of the others.

embeddings.py is vendored byte-identical across the fleet (see ch-h-bot
docs/MCP_PROVIDER_PATTERN.md); whatever is particular to this corpus lives
here instead, so that the shared file can stay a checksum.

embed_db.py and vectorstore.py are shared as well (thodel/hbls_mcp#9, plan and
measurement in thodel/ch-h-bot#555). What they need from this file is the
interface at the bottom: DB_ENV, DEFAULT_DB, PARENT_TABLE, iter_documents and
clean. The head line on every passage, `headword volume`, is built by
head_line — the one place that writes it, and the one db.py cuts it with.
"""

from __future__ import annotations

import os
import re
import unicodedata

# The shared embeddings.py reads HBLS_EMBED_MODEL, HBLS_CHUNK_CHARS, ... only
# if it knows the prefix. Set before it is imported; the Dockerfile sets it too.
os.environ.setdefault("MCP_ENV_PREFIX", "HBLS")

import embeddings as emb  # noqa: E402

# Qwen3-Embedding is instruction-aware: queries carry a task instruction and
# documents do not. This one names the corpus; the shared default only says
# "Swiss history".
QUERY_PREFIX = os.environ.get(
    "HBLS_EMBED_QUERY_PREFIX",
    "Instruct: Given a question about Swiss history, retrieve passages from "
    "the Historisch-Biographisches Lexikon der Schweiz (1921-1934), an "
    "encyclopaedia of persons, families, places and institutions.\nQuery: ",
)


# ── Transcription cleanup: resolve abbreviations to their expanded reading ────
#
# The edition transcribes diplomatically. Segment boundaries are marked with ✳
# (82,062 of them), and every abbreviated word is written as the raw manuscript
# form immediately followed by the editor's expansion:
#
#     "un̄ und"           the raw form is a whole word            -> und
#     "Hein r₎ rich"      the raw form continues a fragment       -> Heinrich
#     "stif tˀin terin"                                           -> stifterin
#     "Diz ist dˀ der"                                            -> Diz ist der
#
# Embedded as-is, this is close to unusable: names arrive split in half, every
# abbreviated word is doubled, and 82k segment markers punctuate the text at
# random. Embedding the expanded reading is the decision this implements.

SEGMENT_MARKER = "✳"
# Expansion bracket, abbreviation hook, combining macron — the three ways this
# edition marks a raw form.
_MARKER_RE = re.compile("[₎ˀ\u0304]")
_PUNCT = ",.;:()[]«»\"\'"


def has_marker(token: str) -> bool:
    """True when a token is a raw manuscript form rather than a reading."""
    return bool(_MARKER_RE.search(token))


def build_vocab(texts) -> dict:
    """For each token, how often it is a fragment versus a word in its own right.

    Deciding whether an expansion joins the token before it needs to know
    whether that token is already a word: "ist" is, "Hein" and "stif" are not.

    Counting total occurrences does not separate them — "Hein" recurs
    constantly, because "Heinrich" is abbreviated the same way every time — and
    neither does a rare/common threshold, because "Hein" is also a name on its
    own. What separates them is the *ratio*: "ist" appears overwhelmingly in
    ordinary positions, "Hein" almost only before a raw form. The corpus
    answers the question itself, which beats a word list for a language with no
    settled orthography.

    Returns ``{token: (n_fragment, n_solo)}``.
    """
    counts: dict[str, list[int]] = {}
    for text in texts:
        tokens = (text or "").replace(SEGMENT_MARKER, " ").split()
        for i, token in enumerate(tokens):
            if has_marker(token):
                continue
            bare = token.strip(_PUNCT).lower()
            if not bare:
                continue
            slot = counts.setdefault(bare, [0, 0])
            followed = i + 1 < len(tokens) and has_marker(tokens[i + 1])
            slot[0 if followed else 1] += 1
    return {k: tuple(v) for k, v in counts.items()}


def _is_wordlike(token: str) -> bool:
    """Alphabetic, tolerating this edition's combining diacritics.

    ``str.isalpha()`` is False for a combining mark, so "Oͤs" (O + U+0364 + s)
    would otherwise never be recognised as a word fragment.
    """
    return bool(token) and all(
        c.isalpha() or unicodedata.combining(c) for c in token)


def clean_entry_text(text: str, vocab: dict) -> str:
    """Transcription reduced to its expanded reading (see the note above)."""
    tokens = (text or "").replace(SEGMENT_MARKER, " ").split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if has_marker(token):
            if i + 1 < len(tokens):
                expansion = tokens[i + 1]
                previous = out[-1] if out else ""
                bare = previous.strip(_PUNCT).lower()
                fragment, solo = vocab.get(bare, (0, 0))
                if _is_wordlike(bare) and fragment > solo:
                    out[-1] = previous + expansion
                else:
                    out.append(expansion)
                i += 2
            else:
                i += 1        # trailing raw form with no expansion after it
            continue
        out.append(token)
        i += 1
    return re.sub(r"\s+([,.;:])", r"\1", " ".join(out)).strip()


# ── The interface the shared embed_db.py reads ──────────────────────────────

DB_ENV = "HBLS_DB"
DEFAULT_DB = "/data/hbls.db"
PARENT_TABLE = "articles"

# The expansion vocabulary of the whole corpus. iter_documents fills it before
# its first yield; clean reads it. Module state rather than an argument because
# the shared chunker calls clean(text) with nothing else.
_VOCAB: dict = {}


def head_line(headword, volume) -> str:
    """The first line of every passage: headword and volume, as a reader cites
    an HBLS article.

    Verbatim — no strip, no normalisation. The 54,015 passages in the index
    were written with exactly this expression by the old embed_db.py, and a
    passage is found again only if its text is the same to the byte
    (measured in thodel/ch-h-bot#555). db.passage_body cuts the same line.
    """
    return " ".join(str(x) for x in (headword, volume) if x)


def clean(text: str) -> str:
    """The expanded reading, resolved with the corpus vocabulary."""
    return clean_entry_text(text, _VOCAB)


def iter_documents(conn, limit=None):
    """Yield (id, head, article_text) for every article not yet chunked.

    The vocabulary is built over the WHOLE table first, including articles
    already chunked and regardless of ``limit``: whether "Hein" is a fragment
    or a name needs every occurrence of the token, not the ones in this batch
    (see build_vocab). The shared build_chunks calls this before its first
    clean, so the order holds.
    """
    global _VOCAB
    _VOCAB = build_vocab(
        t for (t,) in conn.execute(
            "SELECT article_text FROM articles WHERE article_text IS NOT NULL"))
    # chunks.parent_id is TEXT ('1', '2', …) and articles.id INTEGER; the
    # comparison works through SQLite's affinity, as the search's join does.
    rows = conn.execute(
        """
        SELECT e.id, e.headword, e.volume, e.article_text
        FROM articles e
        WHERE e.article_text IS NOT NULL AND e.article_text <> ''
          AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.parent_id = e.id)
        ORDER BY e.id
        """ + (" LIMIT ?" if limit else ""),
        (limit,) if limit else ()).fetchall()
    for article_id, headword, volume, text in rows:
        yield article_id, head_line(headword, volume), text


def embed_query(query: str, model: str = emb.DEFAULT_MODEL, client=None) -> list[float]:
    """Embed one search query with this corpus's instruction prefix."""
    vectors = emb.embed_texts([QUERY_PREFIX + query], model=model, client=client)
    if not vectors:
        raise emb.EmbeddingError("the embedding service returned no vector for the query")
    return vectors[0]
