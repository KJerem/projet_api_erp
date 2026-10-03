"""Client des Web Services REST Moodle.

Fonctions utilisées (service externe `opentraining_integration`) :
  core_user_get_users_by_field, core_user_create_users, core_enrol_get_users_courses,
  enrol_manual_enrol_users, enrol_manual_unenrol_users, core_course_get_courses_by_field.
"""

import logging
import secrets
import time

import httpx

from shared.errors import PermanentError, TransientError

log = logging.getLogger("moodle")

STUDENT_ROLE_ID = 5
# Erreurs Moodle liées à l'infrastructure (base indisponible…) : retentables.
_TRANSIENT_ERRORCODES = {"dmlreadexception", "dmlwriteexception", "dbconnectionfailed", "dbsessionhandlerproblem"}


class MoodleClient:
    def __init__(self, base_url: str, token: str, timeout: float, correlation_id: str | None = None) -> None:
        headers = {"X-Correlation-ID": correlation_id} if correlation_id else {}
        self._token = token
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def call(self, function: str, **params):
        start = time.perf_counter()
        try:
            resp = self._http.post(
                "/webservice/rest/server.php",
                data={"wstoken": self._token, "wsfunction": function, "moodlewsrestformat": "json", **params},
            )
        except httpx.TimeoutException as exc:
            raise TransientError(f"Moodle timeout ({function})") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"Moodle injoignable ({function}) : {exc!r}") from exc
        latency = round((time.perf_counter() - start) * 1000, 1)
        log.info("appel Moodle", extra={"wsfunction": function, "status": resp.status_code, "latencyMs": latency})

        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"Moodle HTTP {resp.status_code} ({function})")
        if resp.status_code >= 400:
            raise PermanentError(f"Moodle HTTP {resp.status_code} ({function})")
        try:
            body = resp.json()
        except ValueError as exc:
            # Page HTML (maintenance, erreur PHP) au lieu du JSON attendu.
            raise TransientError(f"Moodle réponse non JSON ({function})") from exc

        # Moodle renvoie ses erreurs métier en HTTP 200 avec un objet "exception".
        if isinstance(body, dict) and "exception" in body:
            message = f"Moodle {function} : {body.get('errorcode')} - {body.get('message')}"
            if body.get("errorcode") in _TRANSIENT_ERRORCODES:
                raise TransientError(message)
            raise PermanentError(message)
        return body

    def find_user_by_email(self, email: str) -> int | None:
        users = self.call("core_user_get_users_by_field", field="email", **{"values[0]": email})
        return users[0]["id"] if users else None

    def create_user(self, email: str, first_name: str, last_name: str) -> int:
        """Le username (champ unique côté Moodle) est l'email : deux créations du même apprenant
        ne peuvent pas aboutir à deux comptes."""
        password = f"Ot1!{secrets.token_urlsafe(16)}"  # conforme à la politique de mot de passe Moodle
        created = self.call(
            "core_user_create_users",
            **{
                "users[0][username]": email.lower(),
                "users[0][email]": email,
                "users[0][firstname]": first_name,
                "users[0][lastname]": last_name,
                "users[0][auth]": "manual",
                "users[0][password]": password,
            },
        )
        return created[0]["id"]

    def get_course(self, shortname: str) -> dict | None:
        courses = self.call("core_course_get_courses_by_field", field="shortname", value=shortname)["courses"]
        return courses[0] if courses else None

    def is_enrolled(self, user_id: int, course_id: int) -> bool:
        courses = self.call("core_enrol_get_users_courses", userid=user_id)
        return any(c["id"] == course_id for c in courses)

    def enrol(self, user_id: int, course_id: int) -> None:
        self.call(
            "enrol_manual_enrol_users",
            **{"enrolments[0][roleid]": STUDENT_ROLE_ID, "enrolments[0][userid]": user_id,
               "enrolments[0][courseid]": course_id},
        )

    def unenrol(self, user_id: int, course_id: int) -> None:
        self.call("enrol_manual_unenrol_users",
                  **{"enrolments[0][userid]": user_id, "enrolments[0][courseid]": course_id})
