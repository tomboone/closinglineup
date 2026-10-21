from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from closinglineup.api.app import create_app
from closinglineup.settings import Settings

# Port 1 on loopback refuses at once, standing in for a stopped Postgres.
DOWN_DSN = "postgresql+psycopg://closinglineup:secret@127.0.0.1:1/x"
INDEX = "<!doctype html><div id='root'></div>"


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(INDEX)
    (tmp_path / "assets" / "app.js").write_text("console.log('app')")
    return tmp_path


@pytest.fixture
def client(static_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=DOWN_DSN), static_dir)) as client:
        yield client


def test_health_is_200_when_the_database_answers(database_url: str, tmp_path: Path) -> None:
    with TestClient(create_app(Settings(database_url=database_url), tmp_path)) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_503_when_the_database_is_down(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_root_serves_the_bundle_index(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.text == INDEX


def test_assets_are_served(client: TestClient) -> None:
    response = client.get("/assets/app.js")

    assert response.status_code == 200
    assert "console.log" in response.text


@pytest.mark.parametrize("path", ["/teams/BOS", "/players/1628983", "/games/0022500001"])
def test_client_routes_fall_back_to_the_index(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.text == INDEX


def test_a_missing_asset_is_404(client: TestClient) -> None:
    assert client.get("/assets/missing.js").status_code == 404


def test_an_unknown_api_route_is_404_not_the_index(client: TestClient) -> None:
    response = client.get("/api/nope")

    assert response.status_code == 404
    assert response.text != INDEX


def test_without_a_bundle_only_the_api_is_served(tmp_path: Path) -> None:
    missing = tmp_path / "static"
    with TestClient(create_app(Settings(database_url=DOWN_DSN), missing)) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/health").status_code == 503
