"""Orchestration d'une inscription (saga) — chaque étape est idempotente et rejouable.

    ERPNext  : Lead (email) ─► Customer (entreprise)
    Moodle   : cours ─► utilisateur (email) ─► inscription au cours        → MOODLE_ENROLLED
    ERPNext  : Sales Order (po_no = registrationId), soumis                 → ERP_RECORDED
    Local    : CONFIRMED + outbox registration.confirmed (même transaction) → CONFIRMED

Règles d'idempotence (livraison at-least-once, crash, workers multiples) :
  - chaque identifiant externe obtenu est mémorisé dans external_mapping ;
  - avant toute création externe : mapping local, puis recherche sur un champ unique
    (email pour Lead / user Moodle, nom pour Customer, po_no pour Sales Order) ;
  - les find-or-create sont sérialisés par un verrou consultatif PostgreSQL (ERPNext n'impose
    aucune unicité sur le nom de Customer) ;
  - un verrou consultatif par inscription empêche deux workers de traiter la même en parallèle.

Compensation : si l'étape ERP échoue définitivement (ou après épuisement des retries) alors que
l'apprenant est inscrit dans Moodle : désinscription Moodle + place libérée + COMPENSATED.
"""

import logging
import os
import time
import uuid
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from shared.config import Settings
from shared.errors import DuplicateInFlight, PermanentError, TransientError
from shared.erpnext import ErpNextClient
from shared.events import (REGISTRATION_COMPENSATED, REGISTRATION_CONFIRMED, REGISTRATION_FAILED, insert_outbox,
                           new_event)
from shared.moodle import MoodleClient
from shared.secrets import erpnext_token, moodle_token

log = logging.getLogger("registration-processor")

CONSUMER_NAME = "registration-orchestrator"
TERMINAL_STATUSES = {"CONFIRMED", "COMPENSATED", "FAILED"}

# Entités de external_mapping (internal_id = registrationId).
ERP_LEAD, ERP_CUSTOMER, ERP_SALES_ORDER = "erp_lead", "erp_customer", "erp_sales_order"
MOODLE_USER, MOODLE_COURSE, MOODLE_ENROLMENT = "moodle_user", "moodle_course", "moodle_enrolment"
_SYSTEM = {ERP_LEAD: "erpnext", ERP_CUSTOMER: "erpnext", ERP_SALES_ORDER: "erpnext",
           MOODLE_USER: "moodle", MOODLE_COURSE: "moodle", MOODLE_ENROLMENT: "moodle"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in {"1", "true", "yes"}


class RegistrationProcessor:
    def __init__(self, settings: Settings, pool: ConnectionPool) -> None:
        self.settings = settings
        self.pool = pool
        self.erpnext_url = os.environ.get("ERPNEXT_URL", "http://erpnext-frontend:8080")
        self.moodle_url = os.environ.get("MOODLE_URL", "http://moodle")
        self.company = os.environ.get("ERPNEXT_COMPANY_NAME", "OpenTraining")
        # Injection de pannes (tests uniquement, désactivées par défaut).
        self.crash_after_moodle = _env_flag("CRASH_AFTER_MOODLE")
        self.erp_step_delay = float(os.environ.get("ERP_STEP_DELAY_SECONDS", "0") or 0)
        if self.crash_after_moodle:
            log.warning("mode CRASH_AFTER_MOODLE actif : le worker plantera juste après une inscription Moodle")
        if self.erp_step_delay:
            log.warning("ERP_STEP_DELAY_SECONDS actif : pause avant l'étape Sales Order",
                        extra={"delaySeconds": self.erp_step_delay})

    # ===================================================================== API du Consumer
    def handle(self, event: dict, attempt: int) -> None:
        reg_id = self._registration_id(event)
        with self._registration_lock(reg_id):
            reg = self._load(reg_id)
            if reg["status"] in TERMINAL_STATUSES:
                log.info("inscription déjà dans un état final, rien à faire",
                         extra={"registrationId": str(reg_id), "status": reg["status"]})
                return
            erp, moodle = self._clients(reg)
            try:
                if reg["status"] == "COMPENSATING":
                    self._compensate(reg, moodle, "reprise de la compensation")
                    return
                self._run_saga(reg, erp, moodle, attempt, event)
            finally:
                erp.close()
                moodle.close()

    def on_retry(self, event: dict, error: Exception, attempt: int, delay: int) -> None:
        reg_id = self._registration_id(event)
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT status FROM registration WHERE id = %s FOR UPDATE", (reg_id,)).fetchone()
            if row is None or row["status"] in TERMINAL_STATUSES or row["status"] == "COMPENSATING":
                return
            self._set_status(conn, reg_id, "RETRYING", f"tentative {attempt} échouée, nouvel essai dans {delay}s : "
                                                       f"{error}", attempts=attempt, error=str(error))

    def on_give_up(self, event: dict, error: Exception, attempts: int) -> str:
        try:
            reg_id = self._registration_id(event)
        except PermanentError:
            return "dlq"
        reg = self._load_or_none(reg_id)
        if reg is None:
            return "dlq"  # événement orphelin : à analyser depuis la DLQ
        if reg["status"] in TERMINAL_STATUSES:
            return "handled"
        if self._mapping(reg_id, MOODLE_ENROLMENT) is not None or reg["status"] == "COMPENSATING":
            # L'apprenant est inscrit dans Moodle mais la saga ne peut aboutir : compensation.
            _, moodle = self._clients(reg)
            try:
                self._compensate(reg, moodle, f"abandon après {attempts} tentative(s) : {error}")
            finally:
                moodle.close()
            return "handled"
        self._fail(reg, error)
        return "dlq"

    # ===================================================================== saga
    def _run_saga(self, reg: dict, erp: ErpNextClient, moodle: MoodleClient, attempt: int, event: dict) -> None:
        reg_id = reg["id"]
        with self.pool.connection() as conn, conn.transaction():
            self._set_status(conn, reg_id, "PROCESSING", f"tentative {attempt}", attempts=attempt)

        # --- ERPNext CRM : Lead (apprenant) puis Customer (entreprise) --------------------------
        lead = self._find_or_create(
            reg_id, ERP_LEAD, f"erp_lead:{reg['student_email']}",
            find=lambda: erp.find_lead(reg["student_email"]),
            create=lambda: erp.create_lead(reg["student_email"], reg["student_first_name"],
                                           reg["student_last_name"], reg["company_name"]),
        )
        customer = self._find_or_create(
            reg_id, ERP_CUSTOMER, f"erp_customer:{reg['company_name'].lower()}",
            find=lambda: erp.find_customer(reg["company_name"]),
            create=lambda: erp.create_customer(reg["company_name"], lead),
        )

        # --- Moodle : cours, utilisateur, inscription ----------------------------------------------
        course_id = self._mapping(reg_id, MOODLE_COURSE)
        if course_id is None:
            course = moodle.get_course(reg["moodle_course_shortname"])
            if course is None:
                raise PermanentError(f"cours Moodle introuvable : {reg['moodle_course_shortname']}")
            course_id = str(course["id"])
            self._save_mapping(reg_id, MOODLE_COURSE, course_id)
        course_id = int(course_id)

        user_id = int(self._find_or_create(
            reg_id, MOODLE_USER, f"moodle_user:{reg['student_email']}",
            find=lambda: moodle.find_user_by_email(reg["student_email"]),
            create=lambda: moodle.create_user(reg["student_email"], reg["student_first_name"],
                                              reg["student_last_name"]),
        ))

        if self._mapping(reg_id, MOODLE_ENROLMENT) is None:
            enrolled_now = False
            if not moodle.is_enrolled(user_id, course_id):
                moodle.enrol(user_id, course_id)
                enrolled_now = True
                log.info("inscription Moodle effectuée", extra={"registrationId": str(reg_id),
                                                                "moodleUserId": user_id, "courseId": course_id})
            if enrolled_now and self.crash_after_moodle:
                log.critical("CRASH_AFTER_MOODLE : arrêt brutal après l'inscription Moodle, avant mapping et ACK",
                             extra={"registrationId": str(reg_id)})
                logging.shutdown()
                os._exit(1)
            self._save_mapping(reg_id, MOODLE_ENROLMENT, f"{user_id}:{course_id}")
        with self.pool.connection() as conn, conn.transaction():
            self._set_status(conn, reg_id, "MOODLE_ENROLLED", f"user {user_id} inscrit au cours {course_id}")

        # --- ERPNext ERP : Sales Order (trace administrative) ---------------------------------------
        if self.erp_step_delay:
            time.sleep(self.erp_step_delay)  # fenêtre laissée au test pour couper ERPNext après Moodle
        try:
            sales_order = self._mapping(reg_id, ERP_SALES_ORDER)
            if sales_order is None:
                existing = erp.find_sales_order(str(reg_id))
                order = existing or erp.create_sales_order(customer, reg["erp_item_code"], str(reg_id))
                if order["docstatus"] == 0:
                    erp.submit_sales_order(order["name"])
                sales_order = order["name"]
                self._save_mapping(reg_id, ERP_SALES_ORDER, sales_order)
        except PermanentError as exc:
            self._compensate(reg, moodle, f"échec définitif ERPNext : {exc}")
            return
        with self.pool.connection() as conn, conn.transaction():
            self._set_status(conn, reg_id, "ERP_RECORDED", f"Sales Order {sales_order}")

        self._confirm(reg, event, {"erpLeadId": lead, "erpCustomerId": customer, "erpSalesOrderId": sales_order,
                                   "moodleUserId": user_id, "courseId": course_id})

    def _confirm(self, reg: dict, event: dict, ids: dict) -> None:
        reg_id = reg["id"]
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT status FROM registration WHERE id = %s FOR UPDATE", (reg_id,)).fetchone()
            if row["status"] in TERMINAL_STATUSES:
                return
            self._set_status(conn, reg_id, "CONFIRMED", "inscription confirmée", error=None)
            confirmed = new_event(REGISTRATION_CONFIRMED, {
                "registrationId": str(reg_id), "sessionId": reg["session_id"],
                "student": {"email": reg["student_email"], "firstName": reg["student_first_name"],
                            "lastName": reg["student_last_name"]},
                "company": {"name": reg["company_name"]},
                "courseShortname": reg["moodle_course_shortname"], **ids,
            }, reg["correlation_id"])
            insert_outbox(conn.cursor(), confirmed, "registration", str(reg_id))
            # Événement marqué traité dans la même transaction que l'effet métier local.
            conn.execute("INSERT INTO processed_events (consumer_name, event_id) VALUES (%s, %s) "
                         "ON CONFLICT DO NOTHING", (CONSUMER_NAME, event["eventId"]))
        log.info("inscription confirmée", extra={"registrationId": str(reg_id), **ids})

    # ===================================================================== compensation / échec
    def _compensate(self, reg: dict, moodle: MoodleClient, reason: str) -> None:
        reg_id = reg["id"]
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT status FROM registration WHERE id = %s FOR UPDATE", (reg_id,)).fetchone()
            if row["status"] in TERMINAL_STATUSES:
                return
            if row["status"] != "COMPENSATING":
                self._set_status(conn, reg_id, "COMPENSATING", reason, error=reason)
        log.warning("compensation démarrée", extra={"registrationId": str(reg_id), "reason": reason})

        user_id, course_id = self._mapping(reg_id, MOODLE_USER), self._mapping(reg_id, MOODLE_COURSE)
        if user_id and course_id:
            moodle.unenrol(int(user_id), int(course_id))  # idempotent ; TransientError => retry de la compensation
            log.info("désinscription Moodle effectuée", extra={"registrationId": str(reg_id),
                                                               "moodleUserId": user_id, "courseId": course_id})
        self._finalize(reg, "COMPENSATED", REGISTRATION_COMPENSATED,
                       "désinscription Moodle + place libérée", reason)

    def _fail(self, reg: dict, error: Exception) -> None:
        # Effets externes partiels possibles (timeout après succès côté Moodle) : désinscription best-effort.
        user_id, course_id = self._mapping(reg["id"], MOODLE_USER), self._mapping(reg["id"], MOODLE_COURSE)
        if user_id and course_id:
            _, moodle = self._clients(reg)
            try:
                moodle.unenrol(int(user_id), int(course_id))
            except Exception:  # noqa: BLE001 - best effort, Moodle peut être la cause de l'échec
                log.warning("désinscription best-effort impossible", extra={"registrationId": str(reg["id"])})
            finally:
                moodle.close()
        self._finalize(reg, "FAILED", REGISTRATION_FAILED, f"échec définitif : {error}", str(error))

    def _finalize(self, reg: dict, status: str, event_type: str, detail: str, error: str) -> None:
        """État final d'échec : libère la place (une seule fois) et publie l'événement via l'outbox."""
        reg_id = reg["id"]
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT status FROM registration WHERE id = %s FOR UPDATE", (reg_id,)).fetchone()
            if row["status"] in TERMINAL_STATUSES:
                return
            conn.execute("SELECT 1 FROM training_session WHERE id = %s FOR UPDATE", (reg["session_id"],))
            conn.execute("UPDATE training_session SET remaining_seats = remaining_seats + 1 WHERE id = %s",
                         (reg["session_id"],))
            self._set_status(conn, reg_id, status, detail, error=error)
            insert_outbox(conn.cursor(), new_event(event_type, {
                "registrationId": str(reg_id), "sessionId": reg["session_id"],
                "student": {"email": reg["student_email"], "firstName": reg["student_first_name"],
                            "lastName": reg["student_last_name"]},
                "reason": error[:500],
            }, reg["correlation_id"]), "registration", str(reg_id))
        log.warning(f"inscription {status}, place libérée", extra={"registrationId": str(reg_id), "reason": error})

    # ===================================================================== outillage
    @staticmethod
    def _registration_id(event: dict) -> uuid.UUID:
        try:
            return uuid.UUID(event["payload"]["registrationId"])
        except (KeyError, ValueError, TypeError) as exc:
            raise PermanentError("payload sans registrationId valide") from exc

    def _clients(self, reg: dict) -> tuple[ErpNextClient, MoodleClient]:
        timeout = self.settings.http_timeout_seconds
        secrets_dir = self.settings.secrets_dir
        return (
            ErpNextClient(self.erpnext_url, erpnext_token(secrets_dir), timeout, self.company, reg["correlation_id"]),
            MoodleClient(self.moodle_url, moodle_token(secrets_dir), timeout, reg["correlation_id"]),
        )

    @contextmanager
    def _registration_lock(self, reg_id: uuid.UUID):
        """Verrou consultatif de session : libéré automatiquement si le worker plante (connexion fermée)."""
        key = f"registration:{reg_id}"
        with self.pool.connection() as conn:
            acquired = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS ok", (key,)).fetchone()["ok"]
            if not acquired:
                raise DuplicateInFlight(str(reg_id))
            try:
                yield
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))

    def _find_or_create(self, reg_id: uuid.UUID, entity: str, lock_key: str, find, create) -> str:
        known = self._mapping(reg_id, entity)
        if known is not None:
            return known
        # Verrou transactionnel : deux workers ne peuvent pas créer le même objet externe en parallèle.
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
            external_id = find()
            if external_id is None:
                try:
                    external_id = create()
                    log.info("objet externe créé", extra={"registrationId": str(reg_id), "entity": entity,
                                                          "externalId": str(external_id)})
                except PermanentError:
                    # Création concurrente hors de notre verrou (ex. via l'interface) : on relit.
                    external_id = find()
                    if external_id is None:
                        raise
            else:
                log.info("objet externe existant réutilisé", extra={"registrationId": str(reg_id), "entity": entity,
                                                                    "externalId": str(external_id)})
        self._save_mapping(reg_id, entity, str(external_id))
        return str(external_id)

    def _mapping(self, reg_id: uuid.UUID, entity: str) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT external_id FROM external_mapping WHERE entity_type = %s AND internal_id = %s",
                (entity, reg_id),
            ).fetchone()
        return row["external_id"] if row else None

    def _save_mapping(self, reg_id: uuid.UUID, entity: str, external_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO external_mapping (entity_type, internal_id, system_name, external_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (entity_type, internal_id, system_name) DO NOTHING",
                (entity, reg_id, _SYSTEM[entity], external_id),
            )

    def _load_or_none(self, reg_id: uuid.UUID) -> dict | None:
        with self.pool.connection() as conn:
            return conn.execute(
                """
                SELECT r.*, s.moodle_course_shortname, s.erp_item_code
                FROM registration r JOIN training_session s ON s.id = r.session_id
                WHERE r.id = %s
                """,
                (reg_id,),
            ).fetchone()

    def _load(self, reg_id: uuid.UUID) -> dict:
        reg = self._load_or_none(reg_id)
        if reg is None:
            raise PermanentError(f"inscription inconnue : {reg_id}")
        return reg

    @staticmethod
    def _set_status(conn, reg_id: uuid.UUID, status: str, detail: str, attempts: int | None = None,
                    error: str | None = "__keep__") -> None:
        conn.execute(
            """
            UPDATE registration
            SET status = %s,
                attempts = COALESCE(%s, attempts),
                last_error = CASE WHEN %s = '__keep__' THEN last_error ELSE %s END,
                updated_at = now()
            WHERE id = %s
            """,
            (status, attempts, error, error, reg_id),
        )
        conn.execute(
            "INSERT INTO registration_status_history (registration_id, status, detail, correlation_id) "
            "SELECT %s, %s, %s, correlation_id FROM registration WHERE id = %s",
            (reg_id, status, detail[:2000], reg_id),
        )
