import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app import registrations as service
from app.errors import ApiError
from app.schemas import ErrorResponse, RegistrationAccepted, RegistrationRequest, RegistrationView
from shared.logging_setup import correlation_id_var

router = APIRouter(prefix="/api/v1/registrations", tags=["registrations"])


@router.post(
    "",
    status_code=202,
    response_model=RegistrationAccepted,
    summary="Demander une inscription (asynchrone, idempotente)",
    responses={
        202: {"description": "Place réservée ; le traitement Moodle/ERPNext continue en asynchrone. "
                             "Un rejeu de la même clé renvoie la même réponse avec `Idempotent-Replayed: true`."},
        400: {"model": ErrorResponse, "description": "En-tête Idempotency-Key absent."},
        404: {"model": ErrorResponse, "description": "Session inconnue."},
        409: {"model": ErrorResponse, "description": "Clé réutilisée avec un autre body, session pleine "
                                                     "ou apprenant déjà inscrit."},
        422: {"description": "Body invalide (validation Pydantic)."},
        503: {"model": ErrorResponse, "description": "Base d'intégration indisponible."},
    },
)
def create_registration(
    payload: RegistrationRequest,
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=255, description="Clé unique choisie par le client (UUID conseillé)."),
    ] = None,
) -> JSONResponse:
    if idempotency_key is None or not idempotency_key.strip():
        raise ApiError(400, "IDEMPOTENCY_KEY_MISSING", "L'en-tête Idempotency-Key est obligatoire.")

    outcome = service.create_registration(
        request.app.state.pool, idempotency_key.strip(), payload, correlation_id_var.get()
    )
    headers = {"Idempotent-Replayed": "true"} if outcome.replayed else {}
    if outcome.status_code == 202:
        headers["Location"] = f"/api/v1/registrations/{outcome.body['registrationId']}"
    return JSONResponse(status_code=outcome.status_code, content=outcome.body, headers=headers)


@router.get(
    "/{registration_id}",
    response_model=RegistrationView,
    summary="Consulter le statut d'une inscription",
    responses={404: {"model": ErrorResponse, "description": "Inscription inconnue."}},
)
def get_registration(registration_id: uuid.UUID, request: Request) -> dict:
    return service.get_registration(request.app.state.pool, registration_id)
