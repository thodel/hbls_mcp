FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# corpus.py and embeddings.py are imported by search_semantic. Modules are listed one by
# one to keep the image small, so a new one has to be added here too —
# hls_mcp shipped without its and crash-looped on the import.
COPY server.py db.py corpus.py embeddings.py .

# embeddings.py is vendored across the fleet and reads HBLS_* settings by this prefix.
ENV MCP_ENV_PREFIX=HBLS

CMD ["python", "server.py", "--db", "/data/hbls.db", "--host", "0.0.0.0", "--port", "8003"]
