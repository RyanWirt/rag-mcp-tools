import os
from typing import Annotated

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP(
    "Manuals",
    host="0.0.0.0",
    port=8001,
    stateless_http=True,
    json_response=True,
)


async def api_call(path: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(
        base_url=os.getenv("RAG_API_URL", "http://localhost:8000"), timeout=240
    ) as client:
        response = await client.post(path, json=payload)
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def ingest_manuals() -> dict:
    """Index PDFs from the shared manuals folder. Report any per-file errors."""
    return await api_call("/ingest")


@mcp.tool()
async def search_manuals(
    question: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")],
    top_k: Annotated[int, Field(ge=1, le=10)] = 3,
) -> dict:
    """Retrieve manual excerpts with filenames, page numbers, and relevance scores."""
    return await api_call("/search", {"question": question, "top_k": top_k})


@mcp.tool()
async def ask_manuals(
    question: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")],
    top_k: Annotated[int, Field(ge=1, le=10)] = 3,
) -> dict:
    """Answer from indexed manuals with sources; optional Ollama generates prose."""
    return await api_call("/ask", {"question": question, "top_k": top_k})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
