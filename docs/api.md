# Référence de l'Integration API et des événements

Ce document décrit le contrat de l'Integration API (FastAPI) et le format des messages échangés sur RabbitMQ.
Il a été rédigé à partir du code et vérifié sur la stack en marche (`GET /openapi.json`, `GET` sur les ressources).

- Base URL locale : `http://localhost:8000`
- Documentation interactive générée par FastAPI : `http://localhost:8000/docs` (Swagger UI) et `http://localhost:8000/openapi.json`
- Version de l'API : `1.0.0`, préfixe `/api/v1` (`integration-api/app/main.py`)
- Corps de requête et de réponse en JSON (UTF-8)

Fichiers de référence :

| Sujet | Fichier |
|---|---|
| Routes | `integration-api/app/routes/registrations.py`, `sessions.py`, `catalog.py`, `health.py` |
| Logique d'inscription (idempotence, verrou, outbox) | `integration-api/app/registrations.py` |
| Schémas Pydantic (validation, modèles de réponse) | `integration-api/app/schemas.py` |
| Format d'erreur, 422, 503, 500 | `integration-api/app/errors.py` |
| Middleware correlation ID et log d'accès | `integration-api/app/main.py` |
| Enveloppe d'événement | `shared/events.py` |
| Topologie et propriétés AMQP | `shared/rabbit.py` |
| Retry, DLQ et en-têtes associés | `shared/consumer.py` |
| Événements émis par la saga | `workers/registration/processor.py` |

---

## 1. Conventions transverses

### 1.1 En-têtes HTTP

| En-tête | Sens | Où | Comportement |
|---|---|---|---|
| `Idempotency-Key` | requête | `POST /api/v1/registrations` | **Obligatoire.** Chaîne de 1 à 255 caractères choisie par le client (UUID conseillé), espaces de début et de fin retirés. Absente ou vide : `400 IDEMPOTENCY_KEY_MISSING`. Plus de 255 caractères : `422 VALIDATION_ERROR`. |
| `X-Correlation-ID` | requête et réponse | tous les endpoints | Facultatif en entrée. Accepté s'il respecte `^[A-Za-z0-9._:-]{1,100}$`, sinon remplacé **sans erreur** par `corr-<uuid4>`. Toujours renvoyé dans la réponse. Propagé dans les logs JSON, dans l'outbox, dans les événements RabbitMQ et dans les appels HTTP vers Moodle et ERPNext (`integration-api/app/main.py`, `shared/erpnext.py`, `shared/moodle.py`). |
| `Idempotent-Replayed: true` | réponse | `POST /api/v1/registrations` | Présent uniquement quand la réponse est rejouée depuis `api_idempotency` (même clé, même body). Le code de statut et le body sont ceux de la première réponse. |
| `Location` | réponse | `POST /api/v1/registrations` (202), `POST /api/v1/sessions` (201) | URL relative de la ressource : `/api/v1/registrations/{registrationId}` ou `/api/v1/sessions/{sessionId}`. Également présent sur un rejeu 202. |
| `X-Catalog-Degraded` | réponse | `GET /api/v1/catalog?partial=true` | Liste, séparée par des virgules, des sources indisponibles (`erpnext`, `moodle`). Absent si le catalogue est complet. |
| `Retry-After: 5` | réponse | toute route qui accède à la base | Envoyé avec `503 SERVICE_UNAVAILABLE` quand PostgreSQL d'intégration est injoignable (`integration-api/app/errors.py`). Il n'est **pas** envoyé avec `503 CATALOG_DEPENDENCY_UNAVAILABLE`. |

### 1.2 Format d'erreur

Toutes les erreurs ont la même forme (`integration-api/app/errors.py`, classe `ApiError`) :

```json
{ "error": "CODE_STABLE", "message": "Texte lisible en français." }
```

Certains codes ajoutent des champs : `registrationId` (`ALREADY_REGISTERED`), `details` (`VALIDATION_ERROR`),
`sources` (`CATALOG_DEPENDENCY_UNAVAILABLE`), `correlationId` (`INTERNAL_ERROR`).
Le client doit se baser sur `error`, pas sur `message`.

### 1.3 Codes d'erreur métier (liste exhaustive)

| `error` | HTTP | Endpoint(s) | Cause | Mémorisé pour la clé ? | Source |
|---|---|---|---|---|---|
| `IDEMPOTENCY_KEY_MISSING` | 400 | POST registrations | En-tête `Idempotency-Key` absent ou vide | non | `routes/registrations.py` |
| `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD` | 409 | POST registrations | Clé déjà utilisée avec un body dont l'empreinte SHA-256 diffère | non (c'est la clé qui est en conflit) | `registrations.py` |
| `SESSION_FULL` | 409 | POST registrations | `remaining_seats = 0` | **oui** | `registrations.py` |
| `ALREADY_REGISTERED` | 409 | POST registrations | L'email a déjà une inscription active (statut autre que `COMPENSATED`/`FAILED`) sur cette session | **oui** | `registrations.py` |
| `SESSION_NOT_FOUND` | 404 | POST registrations, GET sessions/{id} | Session inconnue | **non** (ROLLBACK : la session peut être créée ensuite, la même clé fonctionnera) | `registrations.py`, `routes/sessions.py` |
| `REGISTRATION_NOT_FOUND` | 404 | GET registrations/{id} | Aucune inscription avec cet UUID | sans objet | `registrations.py` |
| `SESSION_ALREADY_EXISTS` | 409 | POST sessions | `sessionId` déjà présent | sans objet | `routes/sessions.py` |
| `VALIDATION_ERROR` | 422 | tous | Body, paramètre de chemin, de requête ou d'en-tête invalide (Pydantic) | non (rien n'est écrit) | `errors.py` |
| `CATALOG_DEPENDENCY_UNAVAILABLE` | 503 | GET catalog | Moodle ou ERPNext injoignable ou en erreur, et `partial` absent ou `false` | sans objet | `catalog.py` |
| `SERVICE_UNAVAILABLE` | 503 | toute route qui accède à la base | `psycopg.OperationalError` ou `PoolTimeout` | non (rien n'a été commité) | `errors.py` |
| `INTERNAL_ERROR` | 500 | tous | Exception non prévue, interceptée par le middleware | non | `main.py`, `errors.py` |

Remarque sur l'ordre des contrôles du `POST /api/v1/registrations` : FastAPI valide le body **avant**
d'exécuter la fonction de route. Une requête sans `Idempotency-Key` **et** avec un body invalide reçoit donc
`422`, pas `400`.

### 1.4 Statuts d'une inscription

Valeurs autorisées par la contrainte `CHECK` de `registration.status` (`migrations/001_init.sql`) :

```
ACCEPTED ─► PROCESSING ─► MOODLE_ENROLLED ─► ERP_RECORDED ─► CONFIRMED
               │  ▲                │
               ▼  │                └─(échec ERPNext définitif ou retries épuisés)─► COMPENSATING ─► COMPENSATED
            RETRYING
               │
               └─(échec définitif ou retries épuisés, sans inscription Moodle)──────────────────────► FAILED
```

Schéma simplifié : chaque nouvelle tentative (retry ou redélivrance après crash, y compris depuis
`MOODLE_ENROLLED` ou `ERP_RECORDED`) repasse par `PROCESSING`. Le diagramme complet est dans
`docs/mermaid.md` (§6).

États finaux : `CONFIRMED`, `COMPENSATED`, `FAILED` (`TERMINAL_STATUSES` dans `workers/registration/processor.py`).
`COMPENSATED` et `FAILED` libèrent la place et ne comptent plus comme inscription active.
Une inscription peut rester en `COMPENSATING` si Moodle est indisponible au moment de la désinscription :
le message part alors en DLQ (voir la question 20 de `docs/soutenance.md`).

---

## 2. Inscriptions

### 2.1 `POST /api/v1/registrations`

Demande d'inscription **asynchrone et idempotente**. Dans une seule transaction PostgreSQL, l'API :

1. réserve la clé d'idempotence ;
2. verrouille la session (`SELECT … FOR UPDATE`) ;
3. vérifie les doublons et les places restantes ;
4. décrémente `remaining_seats` ;
5. insère l'inscription, son historique et l'événement `registration.accepted` dans `outbox_event` ;
6. mémorise la réponse pour les rejeux.

Elle n'appelle ni Moodle ni ERPNext (`integration-api/app/registrations.py`, `create_registration`).

**Requête**

| Champ | Type | Contraintes |
|---|---|---|
| `student.email` | string | email valide (`EmailStr`), converti en minuscules |
| `student.firstName` | string | 1 à 100 caractères après suppression des espaces de bord |
| `student.lastName` | string | 1 à 100 caractères |
| `company.name` | string | 1 à 140 caractères |
| `sessionId` | string | `^[A-Z0-9][A-Z0-9_-]{1,99}$` |

Tout champ supplémentaire est refusé (`extra="forbid"`) : `422`.

```bash
curl -i -X POST http://localhost:8000/api/v1/registrations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 6f1c2a3e-8b0d-4a51-9a77-2f1e0c9d4b10' \
  -H 'X-Correlation-ID: corr-demo-001' \
  -d '{
        "student": {"email": "alice@example.org", "firstName": "Alice", "lastName": "Martin"},
        "company": {"name": "ACME"},
        "sessionId": "SESSION-PYTHON-01"
      }'
```

**Réponses**

`202 Accepted` : place réservée ; Moodle et ERPNext seront traités par le registration-worker.

```http
HTTP/1.1 202 Accepted
location: /api/v1/registrations/0b270a99-608b-4b30-8601-7b9fce659915
x-correlation-id: corr-demo-001
content-type: application/json

{"registrationId": "0b270a99-608b-4b30-8601-7b9fce659915", "status": "ACCEPTED"}
```

`202 Accepted` rejoué : même clé, même body (après normalisation), y compris pour des requêtes simultanées.
Aucune nouvelle place n'est consommée et aucun nouvel événement n'est créé.

```http
HTTP/1.1 202 Accepted
idempotent-replayed: true
location: /api/v1/registrations/0b270a99-608b-4b30-8601-7b9fce659915

{"registrationId": "0b270a99-608b-4b30-8601-7b9fce659915", "status": "ACCEPTED"}
```

Le body rejoué est celui de la première réponse : le `status` reste `ACCEPTED` même si l'inscription a
progressé depuis. L'état courant s'obtient avec `GET /api/v1/registrations/{id}`.

Normalisation avant calcul de l'empreinte (`request_hash`) : espaces de bord retirés, email en minuscules,
JSON sérialisé avec clés triées. `"  Alice@Example.org "` et `"alice@example.org"` donnent donc la même empreinte.

`400 Bad Request` (en-tête absent) :

```json
{"error": "IDEMPOTENCY_KEY_MISSING", "message": "L'en-tête Idempotency-Key est obligatoire."}
```

`404 Not Found` :

```json
{"error": "SESSION_NOT_FOUND", "message": "Session inconnue : SESSION-INCONNUE"}
```

`409 Conflict` : trois cas.

```json
{"error": "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD",
 "message": "Cette Idempotency-Key a déjà été utilisée avec un contenu différent."}
```
```json
{"error": "SESSION_FULL", "message": "Plus aucune place disponible."}
```
```json
{"error": "ALREADY_REGISTERED",
 "message": "Cet apprenant a déjà une inscription active pour cette session.",
 "registrationId": "0b270a99-608b-4b30-8601-7b9fce659915"}
```

`SESSION_FULL` et `ALREADY_REGISTERED` sont mémorisés : rejouer la même clé renvoie le même 409 avec
`Idempotent-Replayed: true`, même si une place s'est libérée entre-temps. Pour retenter, il faut une nouvelle clé.

`422 Unprocessable Entity` :

```json
{
  "error": "VALIDATION_ERROR",
  "message": "Le contenu de la requête est invalide.",
  "details": [
    {"type": "value_error", "loc": ["body", "student", "email"],
     "msg": "value is not a valid email address: An email address must have an @-sign."}
  ]
}
```

`details` reprend les erreurs Pydantic, sans les champs `input`, `ctx` et `url`, pour ne pas renvoyer la saisie du client.

`503 Service Unavailable` : base d'intégration indisponible. Le client peut réessayer avec **la même** clé.

```http
HTTP/1.1 503 Service Unavailable
retry-after: 5

{"error": "SERVICE_UNAVAILABLE", "message": "Base d'intégration indisponible, réessayez."}
```

`500 Internal Server Error` :

```json
{"error": "INTERNAL_ERROR", "message": "Erreur interne inattendue.", "correlationId": "corr-demo-001"}
```

### 2.2 `GET /api/v1/registrations/{registrationId}`

Retourne l'état courant de l'inscription, les identifiants externes (lus dans `external_mapping`) et l'historique complet des statuts.

```bash
curl -s http://localhost:8000/api/v1/registrations/0b270a99-608b-4b30-8601-7b9fce659915
```

`200 OK` (réponse réelle d'une inscription confirmée) :

```json
{
  "registrationId": "0b270a99-608b-4b30-8601-7b9fce659915",
  "status": "CONFIRMED",
  "sessionId": "SESSION-PYTHON-01",
  "student": {"email": "notif-smtp-16c6494b@example.org", "firstName": "Test", "lastName": "notif-smtp-16c6494b"},
  "company": {"name": "RESILIENCE CORP"},
  "correlationId": "corr-notif-smtp-16c6494b",
  "moodleUserId": 368,
  "courseId": 2,
  "erpLeadId": "CRM-LEAD-2026-00365",
  "erpCustomerId": "RESILIENCE CORP",
  "erpSalesOrderId": "SAL-ORD-2026-00349",
  "attempts": 1,
  "lastError": null,
  "createdAt": "2026-10-03T16:58:34.533511Z",
  "updatedAt": "2026-10-03T16:58:35.925152Z",
  "history": [
    {"status": "ACCEPTED",        "detail": "place réservée",                  "at": "2026-10-03T16:58:34.533511Z"},
    {"status": "PROCESSING",      "detail": "tentative 1",                     "at": "2026-10-03T16:58:35.096604Z"},
    {"status": "MOODLE_ENROLLED", "detail": "user 368 inscrit au cours 2",     "at": "2026-10-03T16:58:35.582452Z"},
    {"status": "ERP_RECORDED",    "detail": "Sales Order SAL-ORD-2026-00349",  "at": "2026-10-03T16:58:35.920166Z"},
    {"status": "CONFIRMED",       "detail": "inscription confirmée",           "at": "2026-10-03T16:58:35.925152Z"}
  ]
}
```

Les identifiants externes valent `null` tant que l'étape correspondante n'a pas eu lieu. `attempts` est le
numéro de la dernière tentative du worker. `lastError` contient la dernière erreur (remis à `null` à la confirmation).

Erreurs :
- `404 REGISTRATION_NOT_FOUND` : `{"error": "REGISTRATION_NOT_FOUND", "message": "Inscription inconnue : 00000000-0000-0000-0000-000000000000"}` ;
- `422 VALIDATION_ERROR` si l'identifiant n'est pas un UUID (`"loc": ["path", "registration_id"]`) ;
- `503 SERVICE_UNAVAILABLE`.

---

## 3. Sessions

### 3.1 `POST /api/v1/sessions`

Création synchrone d'une session de formation : seule la base d'intégration est écrite. Le cours Moodle et
l'article ERPNext référencés doivent déjà exister. S'ils sont absents, l'erreur apparaît au traitement de la
première inscription : erreur permanente, puis `FAILED` ou `COMPENSATED`.

| Champ | Type | Contraintes |
|---|---|---|
| `sessionId` | string | `^[A-Z0-9][A-Z0-9_-]{1,99}$` |
| `capacity` | integer | 1 à 10 000 |
| `moodleCourseShortname` | string | 1 à 100 caractères (nom court du cours Moodle) |
| `erpItemCode` | string | 1 à 140 caractères (code article ERPNext) |

```bash
curl -i -X POST http://localhost:8000/api/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"sessionId": "SESSION-PYTHON-02", "capacity": 12,
       "moodleCourseShortname": "PYTHON-ADV", "erpItemCode": "FORMATION-PYTHON"}'
```

`201 Created` :

```http
HTTP/1.1 201 Created
location: /api/v1/sessions/SESSION-PYTHON-02

{"sessionId": "SESSION-PYTHON-02", "capacity": 12, "remainingSeats": 12, "activeRegistrations": 0,
 "moodleCourseShortname": "PYTHON-ADV", "erpItemCode": "FORMATION-PYTHON"}
```

Erreurs : `409 SESSION_ALREADY_EXISTS` (`INSERT … ON CONFLICT (id) DO NOTHING` sans ligne insérée), `422`, `503`.

```json
{"error": "SESSION_ALREADY_EXISTS", "message": "La session SESSION-PYTHON-02 existe déjà."}
```

Cette route n'exige pas d'`Idempotency-Key` : la clé primaire `sessionId` rend déjà la création idempotente.
Un second appel ne crée rien et répond 409.

### 3.2 `GET /api/v1/sessions` et `GET /api/v1/sessions/{sessionId}`

```bash
curl -s http://localhost:8000/api/v1/sessions
curl -s http://localhost:8000/api/v1/sessions/SESSION-PYTHON-01
```

`200 OK` (liste, ou objet seul pour la route avec identifiant) :

```json
{"sessionId": "SESSION-PYTHON-01", "capacity": 50, "remainingSeats": 19, "activeRegistrations": 31,
 "moodleCourseShortname": "PYTHON-ADV", "erpItemCode": "FORMATION-PYTHON"}
```

`activeRegistrations` compte les inscriptions dont le statut n'est ni `COMPENSATED` ni `FAILED`.
L'invariant `capacity - remainingSeats = activeRegistrations` est vérifiable avec cette route.
Erreur : `404 SESSION_NOT_FOUND` (route avec identifiant), `503`.

Les sessions initiales sont créées par la migration à partir de `infra/catalog.json` : `SESSION-PYTHON-01` (capacité 50) et `SESSION-CONCURRENCY` (capacité 1).

---

## 4. Catalogue agrégé : `GET /api/v1/catalog`

Agrégation **synchrone** de trois sources (`integration-api/app/catalog.py`) :

| Champ | Source |
|---|---|
| `sessionId`, `capacity`, `remainingSeats`, `moodleCourseShortname`, `erpItemCode` | PostgreSQL d'intégration |
| `courseName`, `courseId` | Moodle, `core_course_get_courses_by_field` (un seul appel) |
| `price`, `currency` | ERPNext, `Item Price` de la liste `Standard Selling` (un seul appel) |

Moodle et ERPNext sont interrogés **en parallèle** (`ThreadPoolExecutor`), chacun avec le timeout
`HTTP_TIMEOUT_SECONDS` (5 s par défaut). La latence est donc bornée par l'appel le plus lent, pas par la somme des deux.

| Paramètre | Défaut | Effet |
|---|---|---|
| `partial` | `false` | `true` : accepte une réponse dégradée si une source manque |

```bash
curl -i http://localhost:8000/api/v1/catalog
curl -i 'http://localhost:8000/api/v1/catalog?partial=true'
```

`200 OK` :

```json
[
  {"sessionId": "SESSION-CONCURRENCY", "courseName": "Concurrence et systèmes distribués", "courseId": 3,
   "remainingSeats": 1, "capacity": 1, "price": 300.0, "currency": "EUR",
   "moodleCourseShortname": "CONCURRENCY", "erpItemCode": "FORMATION-CONCURRENCY"},
  {"sessionId": "SESSION-PYTHON-01", "courseName": "Python avancé", "courseId": 2,
   "remainingSeats": 19, "capacity": 50, "price": 850.0, "currency": "EUR",
   "moodleCourseShortname": "PYTHON-ADV", "erpItemCode": "FORMATION-PYTHON"}
]
```

Un article sans prix dans ERPNext, ou un cours introuvable, donne `null` dans les champs concernés. Ce n'est
pas une panne : la réponse reste `200` sans en-tête de dégradation.

`503` en mode strict, ici avec Moodle gelé :

```json
{"error": "CATALOG_DEPENDENCY_UNAVAILABLE",
 "message": "Une source du catalogue est indisponible (réessayez, ou ?partial=true).",
 "sources": {"moodle": "Moodle timeout (core_course_get_courses_by_field)"}}
```

`200` en mode partiel avec la même panne : en-tête `X-Catalog-Degraded: moodle`, `courseName` et `courseId` à `null` sur toutes les entrées.

Seules les `IntegrationError` (timeout, connexion refusée, HTTP 4xx ou 5xx, secret absent) sont traitées
comme une indisponibilité. Toute autre exception produit `500 INTERNAL_ERROR`.

---

## 5. Santé

### 5.1 `GET /health` : liveness

Répond si le processus tourne, sans tester aucune dépendance. Cette route n'est pas journalisée dans les logs d'accès.

```bash
curl -s http://localhost:8000/health
```
```json
{"status": "UP"}
```

### 5.2 `GET /ready` : readiness

Vérifie PostgreSQL (une requête, timeout 2 s) et RabbitMQ (ouverture d'une connexion AMQP, timeout 2 s), et
renvoie le nombre d'événements de l'outbox pas encore publiés.

```json
{"status": "UP", "checks": {"postgres": "UP", "rabbitmq": "UP"}, "pendingOutboxEvents": 0}
```

`503` si au moins une dépendance est indisponible, avec le même body et `"status": "DOWN"`.
`pendingOutboxEvents` vaut `null` si PostgreSQL est indisponible.
`POST /registrations` continue d'accepter les inscriptions quand RabbitMQ est indisponible (voir `docs/soutenance.md`, question 13).
`/ready` reflète cette indisponibilité, `/health` non.

---

## 6. Récapitulatif des codes HTTP

| Code | Où | Justification |
|---|---|---|
| 200 | GET, catalogue (y compris dégradé) | Lecture réussie |
| 201 | POST sessions | Ressource créée de façon synchrone et immédiatement consultable (`Location`) |
| 202 | POST registrations | Demande acceptée et persistée ; le traitement Moodle/ERPNext n'est pas terminé (`Location` pour suivre l'état) |
| 400 | POST registrations | Requête incomplète au niveau du protocole : `Idempotency-Key` manquante |
| 404 | GET par id, POST registrations | Ressource référencée inexistante |
| 409 | POST registrations, POST sessions | Conflit avec l'état actuel : clé réutilisée, session pleine, déjà inscrit, session existante |
| 422 | tous | Requête bien formée mais contenu contraire au contrat (Pydantic) |
| 500 | tous | Bug ou cas non prévu, avec `correlationId` pour retrouver les logs |
| 503 | toute route qui accède à la base, catalogue, `/ready` | Dépendance indisponible : le client peut réessayer plus tard |

Couverture par les tests : `scripts/test-http-codes.py` (200, 201, 202, 400, 404, 409, 422 et correlation ID),
`scripts/test-concurrency.py` (409 `SESSION_FULL`), `scripts/test-idempotency.py` (409 clé réutilisée, rejeu),
`scripts/test-db-down.sh` (503 ; l'en-tête `Retry-After: 5` est posé par `database_unavailable` dans `integration-api/app/errors.py` mais n'est pas vérifié par un test), `scripts/test-catalog.py` (503 et mode partiel).
Le 500 n'a pas de test automatisé : il faudrait injecter un bug.

---

## 7. Événements RabbitMQ

### 7.1 Topologie

Topologie déclarée en code par chaque service au démarrage (`shared/rabbit.py`, `declare_topology`) :

| Exchange | Type | Rôle |
|---|---|---|
| `si.events` | topic | Événements métier ; la routing key est le type d'événement |
| `si.events.dlx` | direct | Dead-letter : routing key = nom de la queue d'origine |
| `si.events.retry` | direct | Retries différés vers les queues `<queue>.retry.{2,4,8}s` |

| Queue | Type | Bindings sur `si.events` | Consumer |
|---|---|---|---|
| `registration.orchestrator` | quorum, `x-delivery-limit` 10 | `registration.accepted` | registration-worker |
| `notifications` | quorum, `x-delivery-limit` 10 | `registration.confirmed`, `registration.compensated`, `registration.failed`, `notification.requested` | notification-worker |
| `audit` | quorum, `x-delivery-limit` 10 | `#` (tout) | audit-worker |
| `registration.dlq`, `notifications.dlq`, `audit.dlq` | classiques durables | via `si.events.dlx` | aucun (inspection : `scripts/inspect-dlq.py`) |
| `<queue>.retry.2s`, `.4s`, `.8s` | classiques, `x-message-ttl` | via `si.events.retry` | aucun : à expiration, le message revient dans `<queue>` par l'exchange par défaut |

### 7.2 Propriétés AMQP des messages publiés par l'outbox

Propriétés posées par `shared/rabbit.py` (`event_properties`) :

| Propriété | Valeur |
|---|---|
| `content_type` | `application/json` |
| `delivery_mode` | 2 (persistant) |
| `message_id` | `eventId` |
| `correlation_id` | `correlationId` |
| `type` | `eventType` |
| `timestamp` | heure de publication (secondes epoch) |
| `headers.schemaVersion` | `1` |
| `headers.x-correlation-id` | `correlationId` |

Les messages sont publiés avec `mandatory=True` sur un canal en mode *publisher confirms*. Un message non
routable ou refusé par le broker lève une exception, et l'événement reste alors non publié dans l'outbox.

En-têtes ajoutés par le consumer générique (`shared/consumer.py`) :

| En-tête | Posé lors de | Contenu |
|---|---|---|
| `x-attempt` | republication en retry | numéro de la prochaine tentative (2, 3, 4) |
| `x-last-error` | republication en retry | dernière erreur (500 caractères max) |
| `x-error`, `x-error-kind`, `x-attempts`, `x-consumer`, `x-failed-at` | envoi en DLQ par le consumer | cause, `permanent` ou `retries exhausted`, nombre de tentatives, nom du consumer, horodatage ISO |
| `x-give-up-error` | envoi en DLQ si l'abandon lui-même échoue (compensation impossible) | erreur de la compensation |
| `x-death` | rejet `basic.reject` (message illisible) ou dépassement de `x-delivery-limit` | posé par RabbitMQ |

### 7.3 Enveloppe v1

Toute publication passe par `shared/events.py` (`new_event`) :

```json
{
  "eventId": "a4118cec-cd0d-468d-86a1-17bdbf86b444",
  "eventType": "registration.accepted",
  "schemaVersion": 1,
  "correlationId": "corr-notif-smtp-16c6494b",
  "occurredAt": "2026-10-03T16:58:34.537Z",
  "payload": { }
}
```

| Champ | Rôle |
|---|---|
| `eventId` | UUID v4, clé de déduplication des consumers (`processed_events (consumer_name, event_id)`) et de l'outbox (`outbox_event.event_id`, clé primaire) |
| `eventType` | type et routing key |
| `schemaVersion` | version du contrat. Un consumer refuse une version qu'il ne connaît pas : `InvalidMessage`, puis `basic.reject`, puis DLQ (`shared/consumer.py`, `parse_event`) |
| `correlationId` | identifiant de la requête HTTP d'origine, réutilisé par tous les événements de l'inscription |
| `occurredAt` | horodatage UTC ISO-8601 en millisecondes |
| `payload` | objet propre au type d'événement |

Un message est rejeté vers la DLQ sans retry s'il n'est pas du JSON, si `eventId` n'est pas un UUID, si
`payload` n'est pas un objet, si `eventType` est vide ou si `schemaVersion` est différent de 1.

### 7.4 Payloads par type

Les exemples ci-dessous sont extraits de la table `audit_log` de la stack de test.

**`registration.accepted`**. Producteur : Integration API (`integration-api/app/registrations.py`), dans la transaction du POST. Consumers : registration-worker, audit-worker.

```json
{
  "registrationId": "0b270a99-608b-4b30-8601-7b9fce659915",
  "sessionId": "SESSION-PYTHON-01",
  "student": {"email": "notif-smtp-16c6494b@example.org", "firstName": "Test", "lastName": "notif-smtp-16c6494b"},
  "company": {"name": "RESILIENCE CORP"}
}
```

**`registration.confirmed`**. Producteur : registration-worker (`processor.py`, `_confirm`), dans la même transaction que le passage à `CONFIRMED` et l'écriture dans `processed_events`. Consumers : notification-worker (email de confirmation), audit-worker.

```json
{
  "registrationId": "0b270a99-608b-4b30-8601-7b9fce659915",
  "sessionId": "SESSION-PYTHON-01",
  "student": {"email": "notif-smtp-16c6494b@example.org", "firstName": "Test", "lastName": "notif-smtp-16c6494b"},
  "company": {"name": "RESILIENCE CORP"},
  "courseShortname": "PYTHON-ADV",
  "erpLeadId": "CRM-LEAD-2026-00365",
  "erpCustomerId": "RESILIENCE CORP",
  "erpSalesOrderId": "SAL-ORD-2026-00349",
  "moodleUserId": 368,
  "courseId": 2
}
```

**`registration.compensated`**. Producteur : registration-worker (`_compensate`, puis `_finalize`), après la désinscription Moodle, dans la transaction qui libère la place. Consumers : notification-worker (email d'annulation), audit-worker.

```json
{
  "registrationId": "4f323020-f5e9-48e5-9397-9341aea68d8d",
  "sessionId": "SESSION-NOTIF-16C6494B",
  "student": {"email": "notif-comp-16c6494b@example.org", "firstName": "Test", "lastName": "notif-comp-16c6494b"},
  "reason": "échec définitif ERPNext : ERPNext HTTP 417 (POST /api/resource/Sales Order) : LinkValidationError Could not find Row #1: Item Code: ARTICLE-INEXISTANT"
}
```

**`registration.failed`**. Producteur : registration-worker (`_fail`, puis `_finalize`), quand la saga échoue avant toute inscription Moodle (erreur permanente ou retries épuisés). La place est libérée. Consumers : notification-worker (email « Inscription non aboutie »), audit-worker. Le message d'origine part aussi en `registration.dlq`.

```json
{
  "registrationId": "da33e4c8-93a3-4aa1-a60f-73e09a18f609",
  "sessionId": "SESSION-PYTHON-01",
  "student": {"email": "dlq-exhausted-aa6d3622@example.org", "firstName": "Test", "lastName": "dlq-exhausted-aa6d3622"},
  "reason": "ERPNext timeout (GET /api/resource/Lead)"
}
```

`reason` est tronqué à 500 caractères. `compensated` et `failed` ne portent pas `company`.

**`notification.requested`** : routing key liée à la queue `notifications`, et rendue par le notification-worker
(`payload.to`, `payload.subject`, `payload.body`). **Aucun producteur ne l'émet dans le code actuel** : c'est
un point d'extension prévu.

### 7.5 Évolution du schéma (v2)

Règles adoptées :

- **Changement additif** (nouveau champ facultatif dans `payload`) : on reste en `schemaVersion: 1`. Les consumers ignorent les champs inconnus, puisqu'ils lisent `payload` avec `.get()` ou par clé explicite.
- **Changement cassant** (renommage, suppression, changement de type ou de sens) : passage à `schemaVersion: 2`. Le producteur publie la v2. Les consumers sont déployés **avant** lui et acceptent v1 et v2 pendant la transition, ce qui suppose d'élargir le contrôle `event.get("schemaVersion") != SCHEMA_VERSION` de `shared/consumer.py`.
- Un consumer qui reçoit une version inconnue n'essaie pas de deviner : le message part en DLQ, d'où il pourra être rejoué une fois le consumer mis à jour.
- La version est portée deux fois : dans le body (`schemaVersion`) et dans l'en-tête AMQP `schemaVersion`. Un routeur peut ainsi trier les messages sans lire le body.
