#!/usr/bin/env python3
"""
server.py — HBLS MCP server using mcp 2.0 MCPServer (StreamableHTTP transport, port 8003).

Tools
-----
corpus_stats()                              → dict
search(query, limit=20)                     → list[dict]
get_article(headword, volume)               → dict
get_article_by_page(volume, page)           → dict
list_volume(volume, limit=100, offset=0)    → list[dict]
get_family_members(headword, volume)        → list[dict]
search_persons(query, limit=50)             → list[dict]
search_bio(query, limit=50)                 → list[dict]
get_pdf_url(headword, volume, page)         → dict
get_articles_by_category(category, volume=None, limit=100, offset=0) → list[dict]
get_category_stats(volume=None)             → dict

Resources
---------
hbls://stats
hbls://volume/{volume}
hbls://article/{headword}/{volume}
"""

import argparse
import json
import logging
import os
import sys

from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

import db as db_module

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Args / env ─────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="HBLS MCP server")
parser.add_argument(
    "--db",
    default=os.environ.get("HBLS_DB", "/home/dh/hbls_mcp/hbls.db"),
)
parser.add_argument(
    "--host",
    default=os.environ.get("HBLS_HOST", "0.0.0.0"),
)
parser.add_argument(
    "--port",
    type=int,
    default=int(os.environ.get("HBLS_PORT", "8003")),
)
parser.add_argument(
    "--sse-path",
    default="/sse",
)
args = parser.parse_args()

db_module.set_db_path(args.db)

# ── Server ─────────────────────────────────────────────────────────────────────

INSTRUCTIONS = (
    "HBLS (Historisches Biographisches Lexikon der Schweiz | "
    "Dictionnaire Historique et Biographique de la Suisse), 8 Bände (1921–1934). "
    "18,244 articles (davon 3,718 biographische), herausgegeben von der "
    "Allgemeinen Geschichtsforschenden Gesellschaft der Schweiz. "
    "Vorgängerwerk des Historischen Lexikons der Schweiz (HLS)."
)

mcp = MCPServer(
    name="HBLS",
    version="1.0.0",
    instructions=INSTRUCTIONS,
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def corpus_stats() -> dict:
    """Return high-level corpus statistics."""
    return db_module.db_stats()


@mcp.tool()
def search(query: str, limit: int = 20) -> list[dict]:
    """Full-text search across HBLS articles (FTS5 or LIKE fallback)."""
    return db_module.search_articles(query, min(limit, 100))


@mcp.tool()
def get_article(headword: str, volume: int) -> dict:
    """Fetch a full article by headword + volume number."""
    r = db_module.get_article(headword.upper().strip(), volume)
    if not r:
        return {"error": f"Article '{headword}' not found in volume {volume}."}
    return r


@mcp.tool()
def get_article_by_page(volume: int, page: int) -> dict:
    """Fetch the article appearing at a given volume + page number."""
    r = db_module.get_article_by_page(volume, page)
    if not r:
        return {"error": f"No article found at v{volume} p{page}."}
    return r


@mcp.tool()
def list_volume(volume: int, limit: int = 100, offset: int = 0) -> list[dict]:
    """List all article headwords in a given volume (paginated)."""
    return db_module.list_volume_articles(volume, min(limit, 500), offset)


@mcp.tool()
def get_family_members(headword: str, volume: int) -> list[dict]:
    """List all persons recorded under a family-article headword."""
    return db_module.get_family_members(headword.upper().strip(), volume)


@mcp.tool()
def search_persons(query: str, limit: int = 50) -> list[dict]:
    """Search persons by forename OR family name across all article categories."""
    return db_module.search_persons(query, min(limit, 200))


@mcp.tool()
def search_bio(query: str, limit: int = 50) -> list[dict]:
    """Search persons in biography ('bio') articles only, by family name + forename."""
    return db_module.search_bio(query, min(limit, 200))


@mcp.tool()
def get_pdf_url(headword: str, volume: int, page: int) -> dict:
    """Return the PDF URL for a specific article page."""
    return db_module.get_pdf_url(headword.upper().strip(), volume, page)


@mcp.tool()
def get_articles_by_category(
    category: str,
    volume: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List articles filtered by category (fam | bio | geo | tem)."""
    if category not in ("fam", "bio", "geo", "tem"):
        return {"error": "category must be one of: fam, bio, geo, tem"}
    return db_module.get_articles_by_category(category, volume, min(limit, 500), offset)


@mcp.tool()
def get_category_stats(volume: int | None = None) -> dict:
    """Return article counts broken down by category, optionally filtered to one volume."""
    return db_module.get_category_stats(volume)


# ── Resources ──────────────────────────────────────────────────────────────────

@mcp.resource("hbls://stats")
def resource_stats() -> str:
    """Static snapshot of corpus statistics."""
    return json.dumps(db_module.db_stats(), indent=2)


@mcp.resource("hbls://volume/{volume}")
def resource_volume(volume: str) -> str:
    """All articles in a given volume as JSON."""
    return json.dumps(
        db_module.list_volume_articles(int(volume), 9999),
        indent=2,
        ensure_ascii=False,
    )


@mcp.resource("hbls://article/{headword}/{volume}")
def resource_article(headword: str, volume: str) -> str:
    """Single article JSON by headword + volume."""
    r = db_module.get_article(headword, int(volume))
    return json.dumps(r if r else {}, indent=2, ensure_ascii=False)


# ── Health endpoint ────────────────────────────────────────────────────────────

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request) -> JSONResponse:
    """Simple TCP-level health check endpoint — returns 200 OK."""
    return JSONResponse({"status": "ok"})


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"HBLS MCP server starting on {args.host}:{args.port}")
    logger.info(f"  DB path   : {args.db}")
    logger.info(f"  SSE path  : {args.sse_path}")

    try:
        stats = db_module.db_stats()
        logger.info(
            f"  Corpus    : {stats['n_articles']:,} articles, "
            f"{stats['n_members']:,} members, {stats['text_mb']} MB text"
        )
    except Exception as exc:
        logger.warning(f"Could not read DB stats on startup: {exc}")

    # Use StreamableHTTP transport (more reliable than SSE for MCP 2.0.0)
    mcp.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path="/messages/",
    )
