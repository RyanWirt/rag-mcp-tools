import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from services import mcp_server
from services.rag_api.app import PDF_ERROR, app, get_store
from services.rag_api.store import INDEX, ManualStore, chunks


def manual_pdf(text="Reset the pump by pressing the blue button."):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class LabTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.documents = {}
        self.requests = []
        self.es = httpx.Client(
            base_url="http://elasticsearch:9200",
            transport=httpx.MockTransport(self.elasticsearch),
        )
        self.addCleanup(self.es.close)
        self.store = ManualStore(self.es)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        app.dependency_overrides[get_store] = lambda: self.store
        self.addCleanup(app.dependency_overrides.clear)
        patcher = patch("services.rag_api.app.MANUALS_DIR", self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.dict("os.environ", {"OLLAMA_MODEL": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def elasticsearch(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.method == "PUT":
            return httpx.Response(200, json={"acknowledged": True})
        if path.endswith("_delete_by_query"):
            filename = json.loads(request.content)["query"]["term"]["filename"]
            self.documents = {
                key: value for key, value in self.documents.items()
                if value["filename"] != filename
            }
        elif path.endswith("_bulk"):
            lines = request.content.decode().splitlines()
            for index in range(0, len(lines), 2):
                self.documents[json.loads(lines[index])["index"]["_id"]] = json.loads(lines[index + 1])
            return httpx.Response(200, json={"errors": False})
        elif path.endswith("_search"):
            body = json.loads(request.content)
            words = body["query"]["match"]["text"].lower().split()
            hits = [
                {"_source": document, "_score": 1.0}
                for document in self.documents.values()
                if any(word in document["text"].lower() for word in words)
            ][: body["size"]]
            return httpx.Response(200, json={"hits": {"hits": hits}})
        return httpx.Response(200, json={})

    def test_index_mapping_and_chunks(self):
        self.store.ensure_index()
        mapping = json.loads(self.requests[-1].content)
        self.assertEqual(mapping["mappings"]["properties"]["filename"]["type"], "keyword")
        self.assertEqual(self.requests[-1].url.path, f"/{INDEX}")
        self.assertEqual(list(chunks("")), [])
        self.assertEqual(list(chunks("abcde" * 4, size=10, overlap=2)), [
            "abcdeabcde", "deabcdeabc", "bcde",
        ])

    def test_upload_ask_and_reingest_replaces_old_chunks(self):
        response = self.client.post("/upload", files={"file": ("manual.pdf", manual_pdf())})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"filename": "manual.pdf", "chunks": 1})
        self.assertTrue((self.folder / "manual.pdf").is_file())
        response = self.client.post("/ask", json={"question": "reset pump"})
        self.assertEqual(response.json()["mode"], "extractive")
        self.assertIn("[1] manual.pdf, page 1", response.json()["answer"])
        self.assertIn("blue button", response.json()["sources"][0]["text"])
        (self.folder / "manual.pdf").write_bytes(manual_pdf("Use the red switch."))
        self.assertEqual(self.client.post("/ingest").json()["errors"], [])
        self.assertEqual(len(self.documents), 1)
        self.assertIn("red switch", next(iter(self.documents.values()))["text"])
        self.assertEqual(
            self.client.post("/search", json={"question": "blue"}).json()["sources"], []
        )

    def test_folder_ingestion_reports_bad_files_and_skips_non_pdfs(self):
        (self.folder / "good.PDF").write_bytes(manual_pdf())
        (self.folder / "bad.pdf").write_bytes(b"not a pdf")
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(self.folder / "scan.pdf")
        (self.folder / "notes.txt").write_text("skip")
        (self.folder / "link.pdf").symlink_to(self.folder / "good.PDF")
        result = self.client.post("/ingest").json()
        self.assertEqual([item["filename"] for item in result["indexed"]], ["good.PDF"])
        self.assertEqual({item["filename"] for item in result["errors"]}, {"bad.pdf", "scan.pdf"})
        self.assertTrue(all(item["error"] == PDF_ERROR for item in result["errors"]))

    def test_invalid_upload_does_not_replace_existing_file(self):
        target = self.folder / "manual.pdf"
        target.write_bytes(manual_pdf())
        original = target.read_bytes()
        for name in ("../escape.pdf", r"..\escape.pdf", "notes.txt"):
            response = self.client.post("/upload", files={"file": (name, b"bad")})
            self.assertEqual(response.status_code, 400)
        response = self.client.post("/upload", files={"file": ("manual.pdf", b"bad")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], PDF_ERROR)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.folder.iterdir()), [target])
        with patch("services.rag_api.app.MAX_PDF_BYTES", 5):
            response = self.client.post("/upload", files={"file": ("big.pdf", b"123456")})
        self.assertEqual(response.status_code, 413)

    def test_empty_results_and_input_validation(self):
        result = self.client.post("/ask", json={"question": "pump"}).json()
        self.assertEqual(result["sources"], [])
        self.assertIn("No matching text", result["answer"])
        for body in ({"question": ""}, {"question": "   "}, {"question": "pump", "top_k": 11}):
            self.assertEqual(self.client.post("/ask", json=body).status_code, 422)

    def test_upstream_failure_returns_actionable_error(self):
        with patch.object(self.store, "search", side_effect=httpx.ConnectError("unavailable")):
            response = self.client.post("/ask", json={"question": "pump"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("container logs", response.json()["detail"])

    def test_ollama_answer_retains_sources(self):
        self.client.post("/upload", files={"file": ("manual.pdf", manual_pdf())})
        with (
            patch.dict("os.environ", {"OLLAMA_MODEL": "test-model"}),
            patch("services.rag_api.app.httpx.Client") as client,
        ):
            client.return_value.__enter__.return_value.post.return_value = httpx.Response(
                200, json={"message": {"content": "Press the blue button [1]."}},
                request=httpx.Request("POST", "http://ollama/api/chat"),
            )
            response = self.client.post("/ask", json={"question": "pump"})
        self.assertEqual(response.json()["mode"], "ollama")
        self.assertEqual(response.json()["sources"][0]["filename"], "manual.pdf")
        self.assertIn("[1]", response.json()["answer"])


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_tools_call_api(self):
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True})

        client = httpx.AsyncClient(
            base_url="http://rag-api:8000", transport=httpx.MockTransport(handler)
        )
        with patch("services.mcp_server.httpx.AsyncClient", return_value=client):
            self.assertEqual(await mcp_server.ask_manuals("reset pump", 2), {"ok": True})
        self.assertEqual(requests[0].url.path, "/ask")
        self.assertEqual(json.loads(requests[0].content), {"question": "reset pump", "top_k": 2})
        tools = await mcp_server.mcp.list_tools()
        self.assertEqual(
            {tool.name for tool in tools}, {"ingest_manuals", "search_manuals", "ask_manuals"}
        )


if __name__ == "__main__":
    unittest.main()
