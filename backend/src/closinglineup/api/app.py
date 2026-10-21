"""The FastAPI app: `/api` routes plus the built frontend as static files (spec §2, §3.1, §8)."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import override

from fastapi import FastAPI, Response, status
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException
from starlette.responses import Response as StarletteResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from closinglineup.api.schemas import HealthResponse
from closinglineup.db.session import make_engine, ping
from closinglineup.logging import configure_logging
from closinglineup.settings import Settings

# Where the image copies the frontend's `dist/`. Absent in local dev, where Vite serves the UI.
STATIC_DIR = Path(__file__).parent / "static"


class SpaStaticFiles(StaticFiles):
    """Serves `index.html` for unknown client-side routes so React Router can resolve them.

    Paths under `api/` and paths with a file suffix (a missing asset) still 404.
    """

    @override
    async def get_response(self, path: str, scope: Scope) -> StarletteResponse:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            route = PurePosixPath(path)
            if exc.status_code != 404 or route.parts[:1] == ("api",) or route.suffix:
                raise
            return await super().get_response("index.html", scope)


def create_app(settings: Settings, static_dir: Path = STATIC_DIR) -> FastAPI:
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        engine.dispose()

    app = FastAPI(title="closinglineup", lifespan=lifespan, docs_url=None, redoc_url=None)

    # DB check only; the freshness rule against the last final game lands with the derive layer.
    @app.get("/api/health")
    def health(response: Response) -> HealthResponse:
        try:
            ping(engine)
        except OperationalError:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(status="unavailable")
        return HealthResponse(status="ok")

    # Mounted last so every `/api` route matches before the catch-all.
    if static_dir.is_dir():
        app.mount("/", SpaStaticFiles(directory=static_dir, html=True), name="frontend")

    return app


def asgi() -> FastAPI:
    """uvicorn's factory: reads the environment in the serving process, reload child included."""
    settings = Settings()
    configure_logging(settings.log_level)
    return create_app(settings)
