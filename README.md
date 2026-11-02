# Manuals lab: MCP + RAG + Elasticsearch

A local, no-login playground for understanding how PDF manuals become searchable
context for an assistant. Elasticsearch, Kibana, the FastAPI RAG service, and the
MCP server each run in a **separate container**. A fifth container is your optional
VS Code development workspace.

No cloud account, paid API, embeddings service, or model download is needed for
the default workflow. It returns relevant **verbatim excerpts**, not generated
prose. An optional Ollama container adds local language-model generation.

## 1. What connects to what?

```text
Local manuals/ folder ──bind mount──> RAG API (:8000)
Browser/curl ──────────────────────> RAG API
MCP-capable assistant ──HTTP────────> MCP server (:8001/mcp)
                                      │
                                      └──HTTP──> RAG API
                                                   │
                                       PDF text → page chunks
                                                   │
                                                   v
                                            Elasticsearch (:9200)
                                                   ^
                                                   │
                                            Kibana (:5601)

Question → Elasticsearch keyword search → cited excerpts → response
                                                   └── optional Ollama → generated answer
```

- **Elasticsearch** stores text chunks and scores keyword matches using BM25.
  This intentionally starts with lexical search, **not vector/semantic search**,
  so there are no hidden embedding or model dependencies.
- **Kibana** lets you inspect the actual indexed documents and queries.
- **RAG API** extracts PDF text with pypdf, chunks each page into 1,200-character
  windows with 200 characters of overlap, retrieves chunks, and returns sources.
  Ollama, when enabled, generates an answer using those same sources.
- **MCP server** exposes API operations as tools. MCP is the connection protocol;
  it is not itself a model, chat UI, or database.
- **Dev container** provides Python and dependencies for editing and testing.
  The API and MCP processes remain offloaded to their own containers.

This is deliberately an unauthenticated, local-only learning environment.
Published ports bind to `127.0.0.1`. Do not expose them publicly or use this as a
production deployment. Manuals are untrusted context: verify generated answers
against the returned pages.

## 2. Prerequisites

- Docker Engine/Desktop with Docker Compose v2.
- Allow Docker at least **6 GB RAM** and several GB of disk for the base lab;
  local LLMs need additional memory and model storage.
- For the dev-container route: VS Code and the **Dev Containers** extension.
- Text-based PDFs that you are allowed to use. Scanned/image-only documents need
  OCR beforehand; encrypted PDFs are not supported.

On Linux, if Elasticsearch reports a `vm.max_map_count` bootstrap error:

```bash
sudo sysctl -w vm.max_map_count=262144
```

Apply that on the Docker host (or Docker Desktop's Linux VM), not in the dev
container. Persist it using your host's sysctl configuration if necessary.

## 3. Start the project

Run these commands from the repository root on your **host**:

```bash
docker compose up -d --build
docker compose ps
curl http://localhost:8000/health
```

The API waits for Elasticsearch, and MCP waits for the API. First boot may take a
few minutes; Kibana may take longer than the API to become ready.

| Service | Host address | Container-network address |
| --- | --- | --- |
| FastAPI docs / upload form | http://localhost:8000/docs | http://rag-api:8000/docs |
| RAG health | http://localhost:8000/health | http://rag-api:8000/health |
| MCP Streamable HTTP endpoint | http://localhost:8001/mcp | http://mcp:8001/mcp |
| Elasticsearch | http://localhost:9200 | http://elasticsearch:9200 |
| Kibana | http://localhost:5601 | http://kibana:5601 |

### Develop inside VS Code

Open the repository and run **Dev Containers: Reopen in Container**. This starts
the same lab services plus the `dev` workspace container. You can also use this
route without first running `docker compose up`.

The workspace is `/workspaces/rag-mcp-tools`; the interpreter and preinstalled
packages are at `/app/.venv/bin/python`. Git and curl are included in the
development image. Run tests in its terminal:

```bash
python -m unittest discover -s tests -v
```

**Inside a container, `localhost` means that container**, not your host or another
service. For example, from the dev terminal use:

```bash
python -c "import httpx; print(httpx.get('http://rag-api:8000/health').json())"
```

Use the host URLs in your normal browser. The workspace does not mount the Docker
socket; run Compose administration commands in a **host terminal**.

## 4. Add and ingest your PDFs

### Option A: copy to the shared folder

Copy your files into the repository's `manuals/` folder on your host, for example:

```bash
cp /path/to/your/manuals.pdf ./manuals/
curl -X POST http://localhost:8000/ingest
```

That folder is mounted at `/manuals` in the API container. Only PDFs directly
inside it are indexed (case-insensitive `.pdf`; no subfolders or symlinks).
Your PDFs are ignored by Git and excluded from Docker builds.

Example ingestion response:

```json
{
  "indexed": [{"filename": "manuals.pdf", "chunks": 14}],
  "errors": []
}
```

Chunk counts depend on the document. Read the `errors` list even when the HTTP
request succeeds: a bad PDF does not prevent other PDFs from being ingested.
Individual PDFs are limited to **50 MiB**.

### Option B: upload in your browser or with curl

Open http://localhost:8000/docs, expand **POST /upload**, select **Try it out**,
choose a PDF, and execute. This saves the file into the same shared folder and
immediately indexes it. No separate `/ingest` call is needed.

```bash
curl -X POST http://localhost:8000/upload \
  -F 'file=@/path/to/your/manuals.pdf'
```

Re-ingesting a filename replaces its previous indexed chunks rather than adding
duplicates. Parsing failures leave the previous indexed version intact.
Index replacement is not transactional if Elasticsearch fails during a write;
retry ingestion after restoring service health. Writes are serialized by the
single API process; this lab is not designed for multiple API replicas.

Deleting or renaming a local file does **not** automatically delete its indexed
chunks. To clear all manuals from the index, run the following and then ingest
the current folder again:

```bash
curl -X POST 'http://localhost:9200/manual-chunks/_delete_by_query?refresh=true' \
  -H 'Content-Type: application/json' -d '{"query":{"match_all":{}}}'
curl -X POST http://localhost:8000/ingest
```

## 5. Ask a question

Use words that occur in your PDF:

```bash
curl -X POST http://localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"How do I reset the pump?","top_k":3}'
```

The response contains:

- `answer`: numbered, page-cited excerpts by default, or model-generated text if
  Ollama is enabled.
- `mode`: `extractive` or `ollama`.
- `sources`: retrieved `filename`, 1-based `page`, `text`, and Elasticsearch
  relevance `score`.

With no matches, the API explicitly says no matching text was found. It never
calls the model with empty context. `top_k` defaults to 3 and accepts 1–10;
questions must contain non-whitespace text and be at most 2,000 characters.

To see retrieval without the answer stage, send the same JSON to **POST /search**.
All endpoints can also be exercised in `/docs`.

### Optional: generate conversational answers with a local LLM

The following are **host terminal** commands:

```bash
docker compose --profile llm up -d ollama
docker compose exec ollama ollama pull llama3.2:1b
OLLAMA_MODEL=llama3.2:1b docker compose --profile llm up -d rag-api mcp
```

Or create an untracked `.env` file containing `OLLAMA_MODEL=llama3.2:1b` and run
`docker compose --profile llm up -d`. Pull the model as above on first use.
For PowerShell, use `.env` instead of the inline environment-variable syntax.

Repeat `/ask`; the response mode should now be `ollama`. The first inference
can be slow on CPU. There is no GPU setup required. Ollama is internal to the
Compose network and its model cache lives in the `ollama-data` volume. The
prompt asks the model to cite sources, but models can still hallucinate.

To return to excerpt-only mode, remove `OLLAMA_MODEL` from `.env` (if set), run
`OLLAMA_MODEL= docker compose up -d rag-api mcp`, and optionally stop Ollama:
`docker compose --profile llm stop ollama`.

## 6. Connect an MCP client

The server uses **Streamable HTTP**, not stdio or the legacy SSE transport.
Configure a compatible client with `http://localhost:8001/mcp` from the host,
or `http://mcp:8001/mcp` from the dev container.

The included `.vscode/mcp.json` config registers a `manuals` HTTP server. In
VS Code, run **MCP: List Servers**, select `manuals`, and start it. When prompted
for the endpoint, use the host URL for a local workspace or the container URL
for a dev-container workspace. Allow the server/tools when VS Code asks.
Use **MCP: Reset Cached Tools** if you need to change the cached endpoint.
An MCP-capable chat/agent mode and model access in your client are separate
from this project's local services.

Available tools:

| Tool | Purpose |
| --- | --- |
| `ingest_manuals()` | Index PDFs already in the shared folder |
| `search_manuals(question, top_k=3)` | Retrieve raw cited excerpts |
| `ask_manuals(question, top_k=3)` | Get a response with its supporting sources |

For example, ask your assistant: “Use `ingest_manuals`, then use `ask_manuals`
to explain how to reset the pump.” PDF upload itself happens through the shared
folder or FastAPI, not through MCP. No PDFs or filesystem mounts are needed in
the MCP container; it delegates to the API over HTTP.

Opening `/mcp` in a browser is **not** a chat UI or health check; an ordinary GET
may return a protocol error. To test MCP without a chat client, run this from a
host Python environment with this project's dependencies installed:

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("http://localhost:8001/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(await session.list_tools())
            print(await session.call_tool("ask_manuals", {"question": "reset pump"}))

asyncio.run(main())
```

In the dev container use `http://mcp:8001/mcp` instead.

## 7. Explore Elasticsearch in Kibana

After ingesting at least one PDF:

1. Open http://localhost:5601. Choose **Explore on my own** if shown.
2. Open **Stack Management → Data Views → Create data view**.
3. Use `manual-chunks*` as the index pattern and choose **I don't want to use the
   time filter** (these documents have no timestamp).
4. Open **Discover**, select that data view, and add `filename`, `page`, and `text`
   as columns. Filter with `filename: "manuals.pdf"`.
5. In **Dev Tools → Console**, try:

```text
GET manual-chunks/_count

GET manual-chunks/_mapping

GET manual-chunks/_search
{
  "size": 3,
  "query": {
    "match": {
      "text": "reset pump"
    }
  }
}
```

Compare `_source` and `_score` with the API's `/search` output. This makes the
retrieval step visible rather than hiding it behind an assistant.

## 8. Project files and configuration

| File/folder | Responsibility |
| --- | --- |
| `compose.yaml` | Service networking, readiness checks, local ports, data volumes, optional `dev`/`llm` profiles |
| `.devcontainer/devcontainer.json` | VS Code workspace container and companion services |
| `.vscode/mcp.json` | MCP client connection with an endpoint prompt |
| `Dockerfile` | Shared Python image used by API, MCP, and development services |
| `pyproject.toml`, `uv.lock` | Python dependency declarations and reproducible dependency resolution |
| `services/rag_api/app.py` | HTTP endpoints, PDF upload, and optional Ollama answer generation |
| `services/rag_api/store.py` | PDF extraction, chunking, Elasticsearch mapping, replacement, and keyword search |
| `services/mcp_server.py` | Three MCP tools that call the RAG API |
| `manuals/` | Local shared PDFs; `.gitkeep` preserves the empty folder |
| `tests/test_lab.py` | Standard-library unittest tests with generated PDFs and mocked upstream HTTP |
| `.gitignore`, `.dockerignore` | Keep private manuals, local environment files, and build artifacts out of commits/images |

Runtime environment variables (Compose sets the container-network values):

| Variable | Python default | Meaning |
| --- | --- | --- |
| `ELASTICSEARCH_URL` | `http://localhost:9200` | Elasticsearch address |
| `MANUALS_DIR` | `/manuals` | Shared PDF directory |
| `RAG_API_URL` | `http://localhost:8000` | API address used by MCP |
| `OLLAMA_MODEL` | empty | Empty = excerpts; otherwise the installed Ollama model name |
| `OLLAMA_URL` | `http://localhost:11434` | Model server; Compose sets `http://ollama:11434` |

The index is named `manual-chunks`. Elasticsearch and Kibana use matching pinned
versions. No Elastic commercial inference features or authentication setup are
required.

### Local Python development (without the dev container)

With Python 3.12+ and [uv](https://docs.astral.sh/uv/) installed:

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
```

Tests generate their own PDFs and mock upstream HTTP, so no running containers
or model downloads are required. No linter is configured. To run a modified API
on the host, stop the Compose API/MCP first and start Elasticsearch:

```bash
docker compose stop mcp rag-api
docker compose up -d elasticsearch kibana
MANUALS_DIR=./manuals uv run uvicorn services.rag_api.app:app --reload
```

In another terminal, `uv run python -m services.mcp_server` starts MCP against
that host API. Application source is baked into the service images: after editing,
run `docker compose up -d --build rag-api mcp` on the host. For dependency changes,
rebuild the dev container too.

## 9. Troubleshooting and cleanup

- **Services still starting:** `docker compose ps` and
  `docker compose logs --tail=100 elasticsearch kibana rag-api mcp`.
- **Empty answers:** verify `/ingest` returned chunks, check its per-file errors,
  then try literal terms from the PDF. Scanned PDFs require OCR. BM25 is not
  semantic retrieval and cannot guarantee that every returned chunk is relevant.
- **503 from `/ask`:** check Elasticsearch; if a model is configured, also check
  `docker compose --profile llm logs ollama`, model download, and `OLLAMA_MODEL`.
  The API does not silently substitute excerpts when generation fails.
- **Connection refused from dev container:** use Compose service names, not
  `localhost`. From a host browser, do the reverse.
- **Port already in use:** change the host side of the relevant `127.0.0.1:PORT:PORT`
  mapping, then update your browser/client URL.
- **PDF upload permissions:** the lab image uses root for simple bind-mount
  compatibility. Uploaded files may be root-owned on Linux; host-copied PDFs
  retain their owner. This is not a hardened image.
- **Stop without losing data:** `docker compose --profile dev --profile llm down`.
  Elasticsearch documents and Ollama models survive in named volumes.
- **Reset everything:** `docker compose --profile dev --profile llm down -v`.
  This deletes indexed data, Kibana configuration stored in Elasticsearch, and
  downloaded models, but **does not delete local PDFs**. Restart and re-ingest.

There is no automatic folder watcher, OCR, vector embedding pipeline, or
production-scale ingestion queue. Those are useful next experiments once the
basic MCP → API → retrieval → response path is clear.