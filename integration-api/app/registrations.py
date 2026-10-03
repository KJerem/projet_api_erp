"""Cœur métier de l'API : inscription idempotente, sans surbooking, avec écriture outbox atomique."""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.errors import ApiError
from app.schemas import RegistrationRequest
from shared.events import REGISTRATION_ACCEPTED, insert_outbox, new_event

log = logging.getLogger("integration-api.registrations")

ACTIVE_EXCLUDED_STATUSES = ("COMPENSATED", "FAILED")


@dataclass
class Outcome:
    status_code: int
    body: dict
    replayed: bool = False


def request_hash(payload: RegistrationRequest) -> str:
    """Empreinte du body *normalisé* (ordre des clés, espaces et casse de l'email sans effet)."""
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_registration(
    pool: ConnectionPool, idempotency_key: str, payload: RegistrationRequest, correlation_id: str
) -> Outcome:
    req_hash = request_hash(payload)

    # Une seule transaction PostgreSQL : clé d'idempotence + réservation + inscription + outbox.
    with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        # 1. Réservation de la clé. Si une autre requête avec la même clé est en cours,
        #    l'INSERT attend son COMMIT/ROLLBACK (verrou de l'index unique) : pas de double traitement.
        cur.execute(
            "INSERT INTO api_idempotency (idempotency_key, request_hash) VALUES (%s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (idempotency_key, req_hash),
        )
        if cur.rowcount == 0:
            cur.execute(
                "SELECT request_hash, response_status, response_body FROM api_idempotency WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            previous = cur.fetchone()
            if previous["request_hash"] != req_hash:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD",
                    "Cette Idempotency-Key a déjà été utilisée avec un contenu différent.",
                )
            log.info("idempotency hit : réponse précédente rejouée", extra={"idempotencyKey": idempotency_key})
            return Outcome(previous["response_status"], previous["response_body"], replayed=True)

        # 2. Verrou exclusif sur la session : les réservations concurrentes sont sérialisées ici.
        cur.execute(
            "SELECT id, remaining_seats FROM training_session WHERE id = %s FOR UPDATE",
            (payload.sessionId,),
        )
        session = cur.fetchone()
        if session is None:
            # Exception => ROLLBACK : la clé n'est pas mémorisée (la session pourra être créée ensuite).
            raise ApiError(404, "SESSION_NOT_FOUND", f"Session inconnue : {payload.sessionId}")

        cur.execute(
            "SELECT id FROM registration WHERE session_id = %s AND lower(student_email) = %s "
            "AND status <> ALL(%s)",
            (payload.sessionId, payload.student.email, list(ACTIVE_EXCLUDED_STATUSES)),
        )
        existing = cur.fetchone()

        if existing is not None:
            outcome = Outcome(
                409,
                {
                    "error": "ALREADY_REGISTERED",
                    "message": "Cet apprenant a déjà une inscription active pour cette session.",
                    "registrationId": str(existing["id"]),
                },
            )
            registration_id = None
        elif session["remaining_seats"] <= 0:
            outcome = Outcome(409, {"error": "SESSION_FULL", "message": "Plus aucune place disponible."})
            registration_id = None
        else:
            registration_id = uuid.uuid4()
            cur.execute(
                "UPDATE training_session SET remaining_seats = remaining_seats - 1 WHERE id = %s",
                (payload.sessionId,),
            )
            cur.execute(
                """
                INSERT INTO registration (id, session_id, student_email, student_first_name, student_last_name,
                                          company_name, status, correlation_id)
                VALUES (%s, %s, %s, %s, %s, %s, 'ACCEPTED', %s)
                """,
                (
                    registration_id,
                    payload.sessionId,
                    payload.student.email,
                    payload.student.firstName,
                    payload.student.lastName,
                    payload.company.name,
                    correlation_id,
                ),
            )
            cur.execute(
                "INSERT INTO registration_status_history (registration_id, status, detail, correlation_id) "
                "VALUES (%s, 'ACCEPTED', 'place réservée', %s)",
                (registration_id, correlation_id),
            )
            event = new_event(
                REGISTRATION_ACCEPTED,
                {
                    "registrationId": str(registration_id),
                    "sessionId": payload.sessionId,
                    "student": payload.student.model_dump(),
                    "company": payload.company.model_dump(),
                },
                correlation_id,
            )
            insert_outbox(cur, event, "registration", str(registration_id))
            outcome = Outcome(202, {"registrationId": str(registration_id), "status": "ACCEPTED"})
            log.info(
                "inscription acceptée",
                extra={"registrationId": str(registration_id), "sessionId": payload.sessionId, "eventId": event["eventId"]},
            )

        # 3. Mémorisation de la réponse (succès ou refus métier) pour les rejeux de la même clé.
        cur.execute(
            "UPDATE api_idempotency SET registration_id = %s, response_status = %s, response_body = %s "
            "WHERE idempotency_key = %s",
            (registration_id, outcome.status_code, Jsonb(outcome.body), idempotency_key),
        )
        return outcome


def get_registration(pool: ConnectionPool, registration_id: uuid.UUID) -> dict:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM registration WHERE id = %s", (registration_id,))
        reg = cur.fetchone()
        if reg is None:
            raise ApiError(404, "REGISTRATION_NOT_FOUND", f"Inscription inconnue : {registration_id}")
        cur.execute(
            "SELECT entity_type, external_id FROM external_mapping WHERE internal_id = %s", (registration_id,)
        )
        mapping = {row["entity_type"]: row["external_id"] for row in cur.fetchall()}
        cur.execute(
            "SELECT status, detail, created_at FROM registration_status_history "
            "WHERE registration_id = %s ORDER BY id",
            (registration_id,),
        )
        history = [{"status": h["status"], "detail": h["detail"], "at": h["created_at"]} for h in cur.fetchall()]

    def as_int(value: str | None) -> int | None:
        return int(value) if value is not None else None

    return {
        "registrationId": str(reg["id"]),
        "status": reg["status"],
        "sessionId": reg["session_id"],
        "student": {
            "email": reg["student_email"],
            "firstName": reg["student_first_name"],
            "lastName": reg["student_last_name"],
        },
        "company": {"name": reg["company_name"]},
        "correlationId": reg["correlation_id"],
        "moodleUserId": as_int(mapping.get("moodle_user")),
        "courseId": as_int(mapping.get("moodle_course")),
        "erpLeadId": mapping.get("erp_lead"),
        "erpCustomerId": mapping.get("erp_customer"),
        "erpSalesOrderId": mapping.get("erp_sales_order"),
        "attempts": reg["attempts"],
        "lastError": reg["last_error"],
        "createdAt": reg["created_at"],
        "updatedAt": reg["updated_at"],
        "history": history,
    }
