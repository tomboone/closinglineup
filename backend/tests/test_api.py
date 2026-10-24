import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from closinglineup.api.app import create_app
from closinglineup.settings import Settings

# Port 1 on loopback refuses at once, standing in for a stopped Postgres.
DOWN_DSN = "postgresql+psycopg://closinglineup:secret@127.0.0.1:1/x"
INDEX = "<!doctype html><div id='root'></div>"
# The schema the frontend's types are generated from; `UPDATE_SNAPSHOTS=1` rewrites it.
OPENAPI_SNAPSHOT = Path(__file__).parent / "fixtures" / "api" / "openapi.json"


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


def test_health_is_503_when_the_database_is_down(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["reason"].startswith("database unreachable")
    assert "secret" not in body["reason"]


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


def test_the_openapi_schema_is_served_and_matches_its_snapshot(client: TestClient) -> None:
    response = client.get("/api/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    if os.environ.get("UPDATE_SNAPSHOTS"):
        OPENAPI_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        OPENAPI_SNAPSHOT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    assert schema == json.loads(OPENAPI_SNAPSHOT.read_text()), (
        "the API's schema changed; rerun with UPDATE_SNAPSHOTS=1 and commit the snapshot"
    )


def test_every_spec_route_s_page_model_is_in_the_schema(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()

    assert {"/api/league", "/api/players/{player_id}", "/api/games/{game_id}"} <= set(
        schema["paths"]
    )
    pages = {"MetaResponse", "TeamsResponse", "TeamPage", "LeaguePage", "PlayerPage", "GamePage"}
    assert pages <= set(schema["components"]["schemas"])
