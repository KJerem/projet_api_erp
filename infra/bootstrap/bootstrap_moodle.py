"""Bootstrap Moodle via ses Web Services REST officiels (aucun accès à la base PostgreSQL de Moodle).

Le token est créé par le conteneur Moodle lui-même (setup_ws.php) dans le volume secrets.
Ce job crée les cours décrits dans catalog.json s'ils n'existent pas encore.
"""

import os

import httpx

from common import SECRETS_DIR, fail, load_catalog, log, wait_until

SERVICE = "moodle-bootstrap"
BASE_URL = os.environ.get("MOODLE_URL", "http://moodle")
TOKEN_FILE = SECRETS_DIR / "moodle_token"
DEFAULT_CATEGORY_ID = 1


class MoodleError(Exception):
    pass


def call(token: str, function: str, **params):
    resp = httpx.post(
        f"{BASE_URL}/webservice/rest/server.php",
        data={"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json", **params},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    # Moodle renvoie ses erreurs en HTTP 200 avec un objet "exception".
    if isinstance(body, dict) and "exception" in body:
        raise MoodleError(f"{function}: {body.get('errorcode')} - {body.get('message')}")
    return body


def main() -> None:
    wait_until(SERVICE, "token Moodle", TOKEN_FILE.exists)
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    wait_until(SERVICE, "Web Services Moodle", lambda: bool(call(token, "core_webservice_get_site_info").get("siteurl")))

    for session in load_catalog():
        course = session["course"]
        found = call(token, "core_course_get_courses_by_field", field="shortname", value=course["shortname"])
        if found["courses"]:
            log(SERVICE, "cours déjà présent", shortname=course["shortname"], courseId=found["courses"][0]["id"])
            continue
        created = call(
            token,
            "core_course_create_courses",
            **{
                "courses[0][fullname]": course["fullname"],
                "courses[0][shortname]": course["shortname"],
                "courses[0][categoryid]": DEFAULT_CATEGORY_ID,
                "courses[0][visible]": 1,
            },
        )
        log(SERVICE, "cours créé", shortname=course["shortname"], courseId=created[0]["id"])

    log(SERVICE, "bootstrap Moodle terminé")


if __name__ == "__main__":
    try:
        main()
    except (MoodleError, httpx.HTTPError) as exc:
        fail(SERVICE, "appel Web Service Moodle en échec", error=str(exc))
