"""Format d'erreur unique : {"error": CODE, "message": ..., ...}."""

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from shared.logging_setup import correlation_id_var

log = logging.getLogger("integration-api.errors")


class ApiError(Exception):
    def __init__(self, status_code: int, error: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": error, "message": message, **extra}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 422 : la requête est bien formée (JSON valide) mais son contenu viole le contrat.
        return JSONResponse(
            status_code=422,
            content={
                "error": "VALIDATION_ERROR",
                "message": "Le contenu de la requête est invalide.",
                "details": jsonable_encoder(exc.errors(), exclude={"input", "ctx", "url"}),
            },
        )

    @app.exception_handler(OperationalError)
    @app.exception_handler(PoolTimeout)
    async def database_unavailable(_: Request, exc: Exception) -> JSONResponse:
        # 503 : dépendance indisponible, le client peut réessayer (avec la même Idempotency-Key).
        log.error("base d'intégration indisponible", extra={"error": str(exc)})
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "5"},
            content={"error": "SERVICE_UNAVAILABLE", "message": "Base d'intégration indisponible, réessayez."},
        )


def internal_error_response() -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_ERROR",
            "message": "Erreur interne inattendue.",
            "correlationId": correlation_id_var.get(),
        },
    )
