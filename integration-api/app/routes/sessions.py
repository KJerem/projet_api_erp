from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.errors import ApiError
from app.schemas import ErrorResponse, SessionCreate, SessionView

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

_SELECT_SESSIONS = """
    SELECT s.id, s.capacity, s.remaining_seats, s.moodle_course_shortname, s.erp_item_code,
           count(r.id) FILTER (WHERE r.status NOT IN ('COMPENSATED', 'FAILED')) AS active_registrations
    FROM training_session s
    LEFT JOIN registration r ON r.session_id = s.id
"""


def _to_view(row: dict) -> dict:
    return {
        "sessionId": row["id"],
        "capacity": row["capacity"],
        "remainingSeats": row["remaining_seats"],
        "activeRegistrations": row["active_registrations"],
        "moodleCourseShortname": row["moodle_course_shortname"],
        "erpItemCode": row["erp_item_code"],
    }


@router.post(
    "",
    status_code=201,
    response_model=SessionView,
    summary="Créer une session de formation",
    responses={
        201: {"description": "Session créée (ressource synchrone, en-tête Location)."},
        409: {"model": ErrorResponse, "description": "Une session avec cet identifiant existe déjà."},
    },
)
def create_session(payload: SessionCreate, request: Request) -> JSONResponse:
    with request.app.state.pool.connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO training_session (id, moodle_course_shortname, erp_item_code, capacity, remaining_seats)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (payload.sessionId, payload.moodleCourseShortname, payload.erpItemCode, payload.capacity, payload.capacity),
        )
    if cur.rowcount == 0:
        raise ApiError(409, "SESSION_ALREADY_EXISTS", f"La session {payload.sessionId} existe déjà.")
    body = {
        "sessionId": payload.sessionId,
        "capacity": payload.capacity,
        "remainingSeats": payload.capacity,
        "activeRegistrations": 0,
        "moodleCourseShortname": payload.moodleCourseShortname,
        "erpItemCode": payload.erpItemCode,
    }
    return JSONResponse(status_code=201, content=body, headers={"Location": f"/api/v1/sessions/{payload.sessionId}"})


@router.get("", response_model=list[SessionView], summary="Lister les sessions")
def list_sessions(request: Request) -> list[dict]:
    with request.app.state.pool.connection() as conn:
        rows = conn.execute(_SELECT_SESSIONS + " GROUP BY s.id ORDER BY s.id").fetchall()
    return [_to_view(r) for r in rows]


@router.get(
    "/{session_id}",
    response_model=SessionView,
    summary="Consulter une session (places restantes)",
    responses={404: {"model": ErrorResponse, "description": "Session inconnue."}},
)
def get_session(session_id: str, request: Request) -> dict:
    with request.app.state.pool.connection() as conn:
        row = conn.execute(_SELECT_SESSIONS + " WHERE s.id = %s GROUP BY s.id", (session_id,)).fetchone()
    if row is None:
        raise ApiError(404, "SESSION_NOT_FOUND", f"Session inconnue : {session_id}")
    return _to_view(row)
