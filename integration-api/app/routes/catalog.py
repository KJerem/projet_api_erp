from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app import catalog as service
from app.schemas import CatalogEntry, ErrorResponse

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


@router.get(
    "",
    response_model=list[CatalogEntry],
    summary="Catalogue agrégé : Moodle (cours) + PostgreSQL (places) + ERPNext (tarif)",
    responses={
        200: {"description": "Catalogue complet. En mode partiel, l'en-tête `X-Catalog-Degraded` liste les "
                             "sources manquantes (champs correspondants à null)."},
        503: {"model": ErrorResponse, "description": "Moodle ou ERPNext indisponible (mode strict)."},
    },
)
def get_catalog(
    request: Request,
    partial: Annotated[bool, Query(description="Accepter une réponse dégradée si une source est indisponible")] = False,
) -> JSONResponse:
    entries, degraded = service.get_catalog(request.app.state.pool, request.app.state.settings, partial)
    headers = {"X-Catalog-Degraded": ",".join(degraded)} if degraded else {}
    return JSONResponse(content=entries, headers=headers)
