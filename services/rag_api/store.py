import hashlib
import json
from pathlib import Path

import httpx
from pypdf import PdfReader

INDEX = "manual-chunks"
MAX_PDF_BYTES = 50 * 1024 * 1024


def chunks(text: str, size: int = 1200, overlap: int = 200):
    text = " ".join(text.split())
    for start in range(0, len(text), size - overlap):
        yield text[start : start + size]
        if start + size >= len(text):
            break


class ManualStore:
    def __init__(self, client: httpx.Client):
        self.client = client

    def ensure_index(self):
        response = self.client.head(f"/{INDEX}")
        if response.status_code == 404:
            response = self.client.put(
                f"/{INDEX}",
                json={
                    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                    "mappings": {
                        "properties": {
                            "filename": {"type": "keyword"},
                            "page": {"type": "integer"},
                            "text": {"type": "text"},
                        }
                    },
                },
            )
        response.raise_for_status()

    def ingest(self, path: Path):
        if path.stat().st_size > MAX_PDF_BYTES:
            raise ValueError("PDF exceeds the 50 MiB limit.")
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported.")
        documents = [
            {"filename": path.name, "page": page_number, "text": text}
            for page_number, page in enumerate(reader.pages, start=1)
            for text in chunks(page.extract_text() or "")
        ]
        if not documents:
            raise ValueError("No readable text found. Scanned PDFs need OCR first.")

        # Parse before replacing so unreadable files do not remove an existing manual.
        response = self.client.post(
            f"/{INDEX}/_delete_by_query",
            params={"refresh": "true"},
            json={"query": {"term": {"filename": path.name}}},
        )
        response.raise_for_status()
        for start in range(0, len(documents), 100):
            lines = []
            for offset, document in enumerate(documents[start : start + 100], start):
                document_id = hashlib.sha256(
                    f"{path.name}:{offset}".encode()
                ).hexdigest()
                lines.extend(
                    [json.dumps({"index": {"_id": document_id}}), json.dumps(document)]
                )
            response = self.client.post(
                f"/{INDEX}/_bulk",
                params={"refresh": "true"},
                content="\n".join(lines) + "\n",
                headers={"Content-Type": "application/x-ndjson"},
            )
            response.raise_for_status()
            if response.json()["errors"]:
                raise RuntimeError("Elasticsearch rejected one or more PDF chunks.")
        return {"filename": path.name, "chunks": len(documents)}

    def search(self, question: str, top_k: int):
        response = self.client.post(
            f"/{INDEX}/_search",
            json={"size": top_k, "query": {"match": {"text": question}}},
        )
        response.raise_for_status()
        return [
            {**hit["_source"], "score": hit["_score"]}
            for hit in response.json()["hits"]["hits"]
        ]
