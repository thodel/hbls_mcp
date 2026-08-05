# hbls_mcp

Model Context Protocol (MCP) server for the **Historisches Biographisches Lexikon der Schweiz** (HBLS), offering full-text search and structured access to the 8-volume encyclopedia published 1921–1934.

## Corpus

- **18,244 articles** (3,718 biographies), 19,707 persons
- 8 volumes covering Swiss history, families, geography, and topics
- Source PDF: [digibern.ch](https://www.digibern.ch/katalog/historisch-biographisches-lexikon-der-schweiz)
- Predecessor to the [Historisches Lexikon der Schweiz (HLS)](https://hls-dhs-ddsg.ch)

## Quick start

```bash
# Requires hbls.db at /data/hbls.db
docker compose up --build -d
```

The server listens on **port 8003** with StreamableHTTP transport.

## Tools

| Tool | Description |
|------|-------------|
| `corpus_stats` | Corpus summary: article/member counts, text size, volumes |
| `search` | FTS5 full-text search across headwords + article text |
| `get_article` | Full article by headword + volume |
| `get_article_by_page` | Article at a given volume + page |
| `list_volume` | Paginated list of all articles in a volume |
| `get_family_members` | All persons listed under a family article |
| `search_persons` | Search persons by forename or family name |
| `search_bio` | Search biographies only (bio category) |
| `get_pdf_url` | Direct PDF URL for headword/volume/page |
| `get_articles_by_category` | List articles by category (fam/bio/geo/tem) |
| `get_category_stats` | Article counts per category |

## Resources

- `hbls://stats` — static corpus statistics snapshot
- `hbls://volume/{n}` — all articles in volume n
- `hbls://article/{headword}/{volume}` — single article

## Database

The server expects the SQLite database at `/data/hbls.db` inside the container.
Mount the host directory containing `hbls.db` to `/data`.

## Transport

Uses **StreamableHTTP** transport (MCP 2.0). The `/messages/` endpoint accepts
JSON-RPC POST requests. A `/health` endpoint returns `{"status":"ok"}`.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HBLS_DB` | `/home/dh/hbls_mcp/hbls.db` | Path to SQLite DB |
| `HBLS_HOST` | `0.0.0.0` | Listen address |
| `HBLS_PORT` | `8003` | Listen port |
