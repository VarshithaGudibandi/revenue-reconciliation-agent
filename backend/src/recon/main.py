from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from recon.api.idempotency import IdempotencyKeyError
from recon.api.routes import demo_router, router
from recon.config import Settings
from recon.db import create_db_engine
from recon.ingestion.normalize import MappingError
from recon.persistence.case_actions import (
    CaseNotFoundError,
    InvalidTransitionError,
    StaleCaseError,
)
from recon.persistence.repository import UnknownProfileError, UnknownSourceError
from recon.persistence.runs import MissingInputsError, UnknownRunError


def error_response(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, **extra}}, status_code=status_code
    )


def register_error_handlers(app: FastAPI) -> None:
    def not_found(_: Request, error: Exception) -> JSONResponse:
        return error_response(404, "not_found", str(error))

    for exception in (CaseNotFoundError, UnknownProfileError, UnknownSourceError, UnknownRunError):
        app.add_exception_handler(exception, not_found)

    @app.exception_handler(StaleCaseError)
    def stale(_: Request, error: StaleCaseError) -> JSONResponse:
        return error_response(409, "stale_case", str(error), current_version=error.actual)

    @app.exception_handler(InvalidTransitionError)
    def invalid_transition(_: Request, error: InvalidTransitionError) -> JSONResponse:
        return error_response(409, "invalid_transition", str(error))

    @app.exception_handler(MissingInputsError)
    def missing_inputs(_: Request, error: MissingInputsError) -> JSONResponse:
        return error_response(409, "missing_inputs", str(error))

    @app.exception_handler(MappingError)
    def unreadable(_: Request, error: MappingError) -> JSONResponse:
        return error_response(422, "unreadable_file", str(error))

    @app.exception_handler(IdempotencyKeyError)
    def bad_key(_: Request, error: IdempotencyKeyError) -> JSONResponse:
        return error_response(422, "idempotency_key", str(error))

    @app.exception_handler(ValueError)
    def invalid(_: Request, error: ValueError) -> JSONResponse:
        return error_response(422, "invalid_request", str(error))

    @app.exception_handler(RequestValidationError)
    def validation(_: Request, error: RequestValidationError) -> JSONResponse:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"][1:])
        return error_response(422, "invalid_request", f"{where}: {first['msg']}".strip(": "))

    @app.exception_handler(StarletteHTTPException)
    def http_error(_: Request, error: StarletteHTTPException) -> JSONResponse:
        return error_response(error.status_code, "http_error", str(error.detail))


class SinglePageApp(StaticFiles):
    """Serve a built frontend, falling back to its index page for client-side routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except (HTTPException, StarletteHTTPException) as error:
            if error.status_code != 404 or path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    engine = create_db_engine(resolved.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(title="Revenue Reconciliation", lifespan=lifespan)
    app.state.settings = resolved
    app.state.sessions = sessionmaker(engine)
    register_error_handlers(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(response: Response) -> dict[str, str]:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable", "database": "unreachable"}
        return {"status": "ok", "database": "ok"}

    app.include_router(router)
    if resolved.demo:
        app.include_router(demo_router)

    static: Path | None = resolved.static_dir
    if static is not None and (static / "index.html").exists():
        app.mount("/", SinglePageApp(directory=static, html=True), name="frontend")
    return app
