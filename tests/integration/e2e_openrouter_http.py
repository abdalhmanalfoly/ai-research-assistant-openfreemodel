"""END-TO-END run of the real application against a REAL PostgreSQL and a LOCAL STAND-IN for the OpenAI API.

What is real:  FastAPI app + lifespan, OpenAI SDK, HTTP on 127.0.0.1, Alembic migrations, PostgreSQL + pgvector,
               PDF parsing/chunking, ingestion job, vector search, citation mapping, the Authorization header.
What is fake:  the "OpenAI" server (tests/fixtures/openrouter_fakes.FakeOpenAI behind uvicorn). It proves our wiring;
               it does NOT prove openrouter.ai accepts these requests.

Run via tests/integration/test_e2e_openrouter_http.py (needs TEST_DATABASE_URL) or:  python -m tests.integration.e2e_openrouter_http
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

"""END-TO-END run of the real application against a REAL PostgreSQL and a LOCAL STAND-IN for the OpenRouter API.

What is real:  FastAPI app + lifespan, OpenAI SDK (OpenRouter base URL), HTTP on 127.0.0.1, Alembic migrations,
               PostgreSQL + pgvector, PDF parsing/chunking, ingestion job, vector search, citation mapping,
               the Authorization header.
What is fake:  the OpenRouter server (tests/fixtures/openrouter_fakes.FakeOpenAI behind uvicorn). It proves our
               wiring; it does NOT prove openrouter.ai accepts these requests.

Run via tests/integration/test_e2e_openrouter_http.py (needs TEST_DATABASE_URL) or:
    python -m tests.integration.e2e_openrouter_http
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

URL = os.environ["TEST_DATABASE_URL"]
KEY = "sk-FAKE-E2E-KEY-do-not-leak-987654321"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
os.environ.update({
    "DATABASE_URL": URL, "OPENROUTER_API_KEY": KEY, "OPENROUTER_BASE_URL": f"http://127.0.0.1:{PORT}/v1",
    "OPENROUTER_ALLOW_CUSTOM_BASE_URL": "true",
    "EMBEDDING_DIMENSIONS": "1024", "RETRIEVAL_THRESHOLD": "0.15", "STORAGE_DIR": "./data/e2e_storage",
    "OPENROUTER_MAX_RETRIES": "0", "OCR_ENABLED": "false", "SERVE_FRONTEND": "false"})

from tests.fixtures.openrouter_fakes import CHAT_MODEL, EMBED_MODEL, FakeOpenAI  # noqa: E402 (after env is set)


def evidence_reply(body: dict) -> str:
    user = body["messages"][-1]["content"]
    ids = re.findall(r'<evidence id="(E\d+)"', user)
    if not ids:
        return json.dumps({"answer": "", "insufficient_evidence": True})
    return json.dumps({"answer": f"The paper reports this finding [{ids[0]}].", "insufficient_evidence": False})


fake = FakeOpenAI(chat_reply=evidence_reply)
stub = FastAPI()


@stub.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def anything(path: str, request: Request):
    r = fake.handler(httpx.Request(request.method, str(request.url), headers=dict(request.headers), content=await request.body()))
    return Response(r.content, status_code=r.status_code, media_type="application/json")


server = uvicorn.Server(uvicorn.Config(stub, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.05)

checks: list[str] = []


def ok(cond, msg):
    if not cond:
        print("E2E FAIL:", msg)
        server.should_exit = True
        sys.exit(1)
    checks.append(msg)
    print("  ok -", msg)


def make_pdf() -> bytes:
    import fitz
    doc = fitz.open()
    pages = ["Retinal imaging study. Deep convolutional networks detect diabetic retinopathy from fundus photographs "
             "with high sensitivity. " * 4,
             "Protein folding study. Transformer models predict protein structure from amino acid sequences "
             "with accuracy approaching experiments. " * 4]
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 550, 780), text, fontsize=11)
    return doc.tobytes()


def main():
    env = {**os.environ}
    with __import__("sqlalchemy").create_engine(URL).begin() as c:
        c.execute(__import__("sqlalchemy").text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
    ok(r.returncode == 0, "alembic upgrade head applied to a fresh database")

    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app()
    with TestClient(app) as client:  # runs the lifespan: build_container() with the real OpenAI SDK clients
        ok(True, "application started with OPENROUTER_API_KEY from the environment (lifespan/build_container)")

        n0 = len(fake.requests)
        h = client.get("/api/health")
        ok(len(fake.requests) == n0, "GET /api/health made zero requests to the provider")
        ok(KEY not in h.text and "FAKE-E2E" not in h.text, "health response does not contain the API key")
        ok(h.json()["llm"] == "configured" and h.json()["schema_dimension"] == 1024, "health reports configured key and 1024-dim schema")

        up = client.post("/api/papers/upload", files={"file": ("study.pdf", make_pdf(), "application/pdf")},
                         data={"title": "Synthetic E2E study", "authors": "A. Author, B. Writer", "year": "2024"})
        ok(up.status_code == 202, "PDF upload accepted (202)")
        pid = up.json()["paper_id"]
        for _ in range(100):
            st = client.get(f"/api/papers/{pid}").json()["status"]
            if st in ("indexed", "failed", "needs_ocr"):
                break
            time.sleep(0.1)
        ok(st == "indexed", f"ingestion finished: paper status = {st}")

        emb_reqs = fake.bodies("/embeddings")
        ok(emb_reqs and all(b["model"] == EMBED_MODEL and b["dimensions"] == 1024 for b in emb_reqs),
           "ingestion embedded chunks with the OpenRouter embedding model @ 1024 dimensions")
        ok(all(r.headers["authorization"] == f"Bearer {KEY}" for r in fake.requests), "every provider request carried the same OPENROUTER_API_KEY")

        import sqlalchemy as sa
        with sa.create_engine(URL).connect() as c:
            rows = c.execute(sa.text("select embedding_model, embedding_dim, vector_dims(embedding), page_number from chunks")).all()
        ok(rows and all(r[:3] == (EMBED_MODEL, 1024, 1024) for r in rows), f"{len(rows)} chunks stored with 1024-dim vectors")
        ok({r[3] for r in rows} == {1, 2}, "page numbers 1 and 2 preserved on chunks")

        n_chat = fake.count("/chat/completions")
        res = client.post("/api/chat", json={"question": "How do convolutional networks detect diabetic retinopathy in fundus photographs?"})
        j = res.json()
        ok(res.status_code == 200 and not j["insufficient_evidence"], "chat answered from retrieved evidence")
        ok(fake.count("/chat/completions") == n_chat + 1, "exactly one chat completion call was made")
        cit = j["citations"][0]
        ok((cit["title"], cit["page_number"], cit["authors"]) == ("Synthetic E2E study", 1, ["A. Author", "B. Writer"]),
           "citation carries real paper title, authors and the page the passage came from (page 1)")
        ok(j["model"] == CHAT_MODEL and "[E1]" in j["answer"], "response reports the configured chat model; answer cites [E1]")
        chat_body = fake.bodies("/chat/completions")[-1]
        ok(chat_body["model"] == CHAT_MODEL and "Retinal imaging study" in chat_body["messages"][-1]["content"],
           "the retrieved passage (not the whole corpus) was sent to the chat model")
        ok("Protein folding study" not in chat_body["messages"][-1]["content"], "an unrelated page was NOT sent to the chat model")
        q_emb = fake.bodies("/embeddings")[-1]
        ok(q_emb["model"] == EMBED_MODEL and q_emb["dimensions"] == 1024, "the query was embedded with the same model and dimensions")

        n_chat = fake.count("/chat/completions")
        res = client.post("/api/chat", json={"question": "quantum chromodynamics lattice gauge"})
        ok(res.json()["insufficient_evidence"] and fake.count("/chat/completions") == n_chat, "off-topic question: safe answer and NO chat call")

        res = client.post("/api/chat", json={"question": "Which paper covers protein folding transformers?", "paper_ids": [pid]})
        ok(res.status_code == 200 and res.json()["citations"][0]["page_number"] == 2, "paper filter works; citation points at page 2")

        quota = (429, {"error": {"message": f"quota {KEY}", "type": "insufficient_quota", "code": "insufficient_quota"}}, {})
        fake.script.append(quota)  # consumed by the first provider call of a chat request = the query embedding
        res = client.post("/api/chat", json={"question": "How do convolutional networks detect diabetic retinopathy?"})
        ok(res.status_code == 503 and res.json()["error"]["code"] == "embedding_quota_exceeded", "quota error while embedding the query -> clean 503 embedding_quota_exceeded")
        fake.chat_script.append(quota)  # embedding succeeds, the chat call is the one that fails
        res = client.post("/api/chat", json={"question": "How do convolutional networks detect diabetic retinopathy?"})
        ok(res.status_code == 503 and res.json()["error"]["code"] == "llm_quota_exceeded", "quota error while generating -> clean 503 llm_quota_exceeded")
        ok("FAKE-E2E" not in res.text, "provider error body (which echoed the key) did not reach the client")

        pc = client.post("/api/health/provider-check").json()
        ok(pc["ok"] and [c["check"] for c in pc["checks"]] == ["chat_model_access", "embedding_model_access"], "explicit provider check passes (free /models calls)")

    r = subprocess.run([sys.executable, "-m", "scripts.reindex_embeddings", "--verify"], env=env, capture_output=True, text=True)
    ok(r.returncode == 0 and "PASS" in r.stdout, "scripts.reindex_embeddings --verify: PASS")
    r = subprocess.run([sys.executable, "-m", "scripts.reindex_embeddings"], env=env, capture_output=True, text=True)
    ok(r.returncode == 0 and "updated=0" in r.stdout, "re-running the re-index is a no-op (nothing re-sent to the API)")
    server.should_exit = True
    print(f"E2E OK ({len(checks)} checks)")


if __name__ == "__main__":
    main()
