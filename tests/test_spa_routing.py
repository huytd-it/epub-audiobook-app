from fastapi.testclient import TestClient

from app.main import app


def test_spa_document_and_form_redirect_targets(monkeypatch):
    from app import main
    from fastapi.responses import HTMLResponse

    monkeypatch.setattr(main, "_spa_index", lambda: HTMLResponse("<html>studio</html>"))
    # No lifespan: these checks must not start workers or touch the real database.
    client = TestClient(app)
    document = client.get("/books/999", headers={"accept": "text/html"})
    assert document.status_code == 200
    assert document.text == "<html>studio</html>"
    redirected_fetch = client.get("/books/999", headers={"accept": "*/*", "sec-fetch-dest": "empty"})
    assert redirected_fetch.status_code == 200
    assert redirected_fetch.text == "<html>studio</html>"


def test_missing_api_and_assets_are_not_spa_documents():
    client = TestClient(app)
    for path in ("/api/missing", "/assets/missing.js"):
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 404


def test_http_readiness_does_not_need_worker_or_database():
    response = TestClient(app).get("/api/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "epub-audiobook"}


def test_index_is_not_cached():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
