import pika
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.schemas import HealthView, ReadinessView

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthView, summary="Liveness : le processus répond")
def health() -> dict:
    return {"status": "UP"}


@router.get(
    "/ready",
    response_model=ReadinessView,
    summary="Readiness : PostgreSQL et RabbitMQ joignables",
    responses={503: {"model": ReadinessView, "description": "Au moins une dépendance est indisponible."}},
)
def ready(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}
    pending = None

    try:
        with request.app.state.pool.connection(timeout=2) as conn:
            pending = conn.execute(
                "SELECT count(*) AS n FROM outbox_event WHERE published_at IS NULL"
            ).fetchone()["n"]
        checks["postgres"] = "UP"
    except Exception:  # noqa: BLE001 - toute erreur = dépendance indisponible
        checks["postgres"] = "DOWN"

    try:
        params = pika.URLParameters(request.app.state.settings.rabbitmq_url)
        params.connection_attempts = 1
        params.socket_timeout = 2
        params.blocked_connection_timeout = 2
        pika.BlockingConnection(params).close()
        checks["rabbitmq"] = "UP"
    except Exception:  # noqa: BLE001
        checks["rabbitmq"] = "DOWN"

    is_up = all(v == "UP" for v in checks.values())
    return JSONResponse(
        status_code=200 if is_up else 503,
        content={"status": "UP" if is_up else "DOWN", "checks": checks, "pendingOutboxEvents": pending},
    )
