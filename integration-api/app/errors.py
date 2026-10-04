"""Format d'erreur unique : {"error": CODE, "message": ..., ...}."""

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

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

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Erreurs levées par FastAPI/Starlette elles-mêmes (avant nos routes), ramenées au format commun.
        if exc.status_code == 400 and "parsing the body" in str(exc.detail):
            # Corps illisible : typiquement un JSON non encodé en UTF-8 (accents envoyés en latin-1).
            error, message = "MALFORMED_BODY", "Le corps de la requête est illisible : JSON encodé en UTF-8 attendu."
        elif exc.status_code == 404:
            error, message = "NOT_FOUND", f"Ressource inconnue : {request.method} {request.url.path}"
        elif exc.status_code == 405:
            error, message = "METHOD_NOT_ALLOWED", f"Méthode {request.method} non autorisée sur {request.url.path}"
        else:
            error, message = "HTTP_ERROR", str(exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"error": error, "message": message},
                            headers=getattr(exc, "headers", None))  # conserve par ex. l'en-tête Allow du 405

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
