"""Integration API (FastAPI) — point d'entrée : uvicorn app.main:app"""

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.errors import install_error_handlers, internal_error_response
from app.routes import health, registrations, sessions
from shared.config import get_settings
from shared.db import create_pool
from shared.logging_setup import correlation_id_var, setup_logging

settings = get_settings()
setup_logging(settings.service_name, settings.log_level)
log = logging.getLogger("integration-api")

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = settings
    app.state.pool = create_pool(settings)
    app.state.pool.open(wait=False)
    log.info("Integration API démarrée")
    yield
    app.state.pool.close()


app = FastAPI(
    title="OpenTraining Integration API",
    version="1.0.0",
    description=(
        "Integration Hub du mini SI OpenTraining : orchestre les inscriptions entre "
        "PostgreSQL, RabbitMQ, Moodle (LMS) et ERPNext (CRM + ERP).\n\n"
        "Chaque requête peut porter un en-tête `X-Correlation-ID` (généré sinon), renvoyé dans la réponse "
        "et propagé dans les logs, les événements RabbitMQ et les appels externes."
    ),
    lifespan=lifespan,
)
install_error_handlers(app)
app.include_router(registrations.router)
app.include_router(sessions.router)
app.include_router(health.router)


@app.middleware("http")
async def correlation_and_access_log(request: Request, call_next):
    incoming = request.headers.get("X-Correlation-ID", "")
    correlation_id = incoming if _CORRELATION_ID_RE.match(incoming) else f"corr-{uuid.uuid4()}"
    correlation_id_var.set(correlation_id)

    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("erreur non gérée", extra={"method": request.method, "path": request.url.path})
        response = internal_error_response()

    response.headers["X-Correlation-ID"] = correlation_id
    if request.url.path not in ("/health",):
        log.info(
            "requête HTTP",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "durationMs": round((time.perf_counter() - start) * 1000, 1),
            },
        )
    return response
