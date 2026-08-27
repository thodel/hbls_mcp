FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# embeddings.py is imported by search_semantic. Modules are listed one by
# one to keep the image small, so a new one has to be added here too —
# hls_mcp shipped without its and crash-looped on the import.
COPY server.py db.py embeddings.py .

CMD ["python", "server.py", "--db", "/data/hbls.db", "--host", "0.0.0.0", "--port", "8003"]
