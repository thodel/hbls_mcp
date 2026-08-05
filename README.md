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

The server listens on **port 8003** with streamable HTTP transport. The endpoint is
`http://localhost:8003/mcp` by default, and whatever `--http-path` says otherwise —
see [Transport](#transport). Connect a client with:

```bash
claude mcp add hbls --transport http --url http://<server-ip>:8003/mcp
```

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
- `hbls://volume/{n}` — article index for volume n: `{volume, total, returned,
  truncated, articles: [...]}`, capped at 1000 rows and flagged when truncated
- `hbls://article/{headword}/{volume}` — single article

## Database

The server expects the SQLite database at `/data/hbls.db` inside the container.
Mount the host directory containing `hbls.db` to `/data`. `db.SCHEMA_SQL` holds the
schema the server expects — the contract between the build pipeline and this server,
and what the tests build their fixtures from.

## Transport

<a id="transport"></a>

**Streamable HTTP** — one endpoint answering `POST` (requests), `GET` (the
server→client stream), and `DELETE` (session teardown). A `/health` endpoint returns
`{"status":"ok"}`.

The endpoint path was previously hard-coded to `/messages/`, which belongs to the SSE
transport and made the endpoint impossible to guess from any client configuration. It
is now `--http-path` (default `/mcp`).

### Behind a reverse proxy

Set `--http-path` (or `HBLS_HTTP_PATH`) to the *public* path, and give nginx a
`location` with the same string. Then nginx forwards the path unchanged:

```nginx
location /mcp/hbls/mcp {
    proxy_pass         http://127.0.0.1:8003;   # no trailing slash
    proxy_http_version 1.1;
    proxy_set_header   Connection '';
    proxy_buffering    off;
    proxy_read_timeout 3600s;
    chunked_transfer_encoding on;
}
```

The app's path and the nginx `location` must agree exactly or every request 404s.
The startup line prints what is actually being served:

```
Starting HBLS MCP server on 0.0.0.0:8003/mcp/hbls/mcp
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HBLS_DB` | `/data/hbls.db` | Path to SQLite DB |
| `HBLS_HOST` | `0.0.0.0` | Listen address |
| `HBLS_PORT` | `8003` | Listen port |
| `HBLS_HTTP_PATH` | `/mcp` | Path the MCP endpoint is served at |

## Query behaviour

**Limits.** Every `limit` is clamped to at most 500; a negative, zero, or non-numeric
value falls back to that tool's own default. The previous `min(limit, 200)` guard let
every negative through, and SQLite reads `LIMIT -1` as unbounded.

**Name search.** SQL wildcards in a query are escaped, so searching for `100%` finds a
literal "100%" rather than matching every record.

**Full-text search.** `search` passes the query to FTS5, so operators work —
`Bern OR Brugg`, `Zwing*`, `NEAR(...)`. An invalid FTS5 query falls back to quoted
phrases and then to a literal headword search instead of raising.

**Result size.** Claude.ai and Claude Desktop truncate a tool or resource result at
roughly 150,000 characters. Search results therefore carry a snippet rather than the
full `article_text` — 20 full articles ran well past that limit. Use `get_article` for
the text of one article.

## Tests

```bash
pip install pytest
pytest test_hbls_mcp.py
```

Unit tests build their own throwaway database and need no setup. DB and server tests
skip unless pointed at them:

```bash
HBLS_DB=/data/hbls.db HBLS_SERVER=http://localhost:8003 pytest test_hbls_mcp.py
```

Requires Python 3.10+ (`X | None` annotations); the container image is
`python:3.12-slim`.
