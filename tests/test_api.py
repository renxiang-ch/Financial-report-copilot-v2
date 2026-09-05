"""FastAPI endpoint tests — no real API calls (mock mode when OPENAI_API_KEY is unset)."""

from fastapi.testclient import TestClient

from copilot.api import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ask_returns_mock_when_no_openai_key(monkeypatch):
    from copilot.config import settings
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "api_key", "")  # disable auth for this test

    resp = client.post("/ask", json={"question": "What was Apple's revenue in FY2024?"})
    assert resp.status_code == 200
    data = resp.json()
    assert "answer" in data
    assert "steps" in data
    assert "citations" in data
    # Mock mode response should mention the key or the question
    assert "api key" in data["answer"].lower() or "mock" in data["answer"].lower()


def test_ask_requires_api_key_when_configured(monkeypatch):
    from copilot.config import settings
    monkeypatch.setattr(settings, "api_key", "secret-test-key")

    resp = client.post("/ask", json={"question": "test"})
    assert resp.status_code == 401


def test_ask_accepts_correct_api_key(monkeypatch):
    from copilot.config import settings
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "api_key", "secret-test-key")

    resp = client.post(
        "/ask",
        json={"question": "test"},
        headers={"X-API-Key": "secret-test-key"},
    )
    assert resp.status_code == 200
