# Diagrammes Mermaid — OpenTraining

Ces diagrammes décrivent le code tel qu'il est. Le texte de référence est
[`docs/architecture.md`](architecture.md). Chaque diagramme indique les fichiers qu'il représente.

1. [Architecture](#1-architecture)
2. [Séquence : happy path](#2-séquence--happy-path)
3. [Séquence : Transactional Outbox](#3-séquence--transactional-outbox)
4. [Idempotence HTTP](#4-idempotence-http)
5. [Consumer idempotent, retry et DLQ](#5-consumer-idempotent-retry-et-dlq)
6. [Machine à états d'une inscription](#6-machine-à-états-dune-inscription)
7. [Séquence : compensation](#7-séquence--compensation)
8. [Topologie RabbitMQ](#8-topologie-rabbitmq)

---

## 1. Architecture

Sources : `docker-compose.yml`, `shared/rabbit.py`, `workers/*/main.py`.

```mermaid
flowchart LR
    client["Client HTTP<br/>(front, curl, tests)"]

    subgraph hub["Integration Hub (image unique, Dockerfile racine)"]
        api["integration-api<br/>FastAPI :8000"]
        disp["outbox-dispatcher"]
        regw["registration-worker<br/>(saga)"]
        notw["notification-worker"]
        audw["audit-worker"]
        mig["migrate<br/>(one-shot)"]
    end

    db[("integration-db<br/>PostgreSQL 17<br/>sessions, inscriptions,<br/>outbox, idempotence, mapping")]
    mq{{"RabbitMQ 4<br/>si.events / retry / dlx"}}
    secrets[["volume integration-secrets<br/>erpnext.json, moodle_token"]]

    subgraph ext["Systèmes métier"]
        erp["ERPNext v16<br/>CRM : Lead, Customer<br/>ERP : Sales Order, Item Price"]
        moodle["Moodle 5.3<br/>utilisateurs, cours,<br/>inscriptions"]
        mail["Mailpit<br/>SMTP :1025"]
    end

    boot["erpnext-bootstrap<br/>moodle-bootstrap"]

    client -- "POST /api/v1/registrations<br/>Idempotency-Key" --> api
    client -- "GET /api/v1/catalog" --> api
    api -- "1 transaction : place + inscription + outbox" --> db
    api -. "catalogue : cours (parallèle, timeout 5 s)" .-> moodle
    api -. "catalogue : tarifs (parallèle, timeout 5 s)" .-> erp
    mig -- "migrations + sessions du catalogue" --> db
    disp -- "SELECT FOR UPDATE SKIP LOCKED" --> db
    disp -- "publish + confirm" --> mq
    mq -- "registration.orchestrator" --> regw
    mq -- "notifications" --> notw
    mq -- "audit" --> audw
    regw -- "statuts, mapping, outbox" --> db
    regw -- "REST /api/resource" --> erp
    regw -- "Web Services REST" --> moodle
    notw -- "SMTP" --> mail
    notw -- "notification + processed_events" --> db
    audw -- "audit_log + processed_events" --> db
    boot -- "clé API ERPNext" --> secrets
    moodle -- "token WS (setup_ws.php)" --> secrets
    secrets -. "lecture seule" .-> regw
    secrets -. "lecture seule" .-> api
```

---

## 2. Séquence : happy path

Sources : `integration-api/app/registrations.py`, `workers/outbox/main.py`, `shared/consumer.py`,
`workers/registration/processor.py`, `workers/notifications/main.py`, `workers/audit/main.py`.

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant API as integration-api
    participant DB as integration-db
    participant D as outbox-dispatcher
    participant MQ as RabbitMQ
    participant W as registration-worker
    participant ERP as ERPNext
    participant M as Moodle
    participant N as notification-worker
    participant A as audit-worker

    C->>API: POST /api/v1/registrations (Idempotency-Key, X-Correlation-ID)
    API->>DB: BEGIN, réserve la clé, session FOR UPDATE
    API->>DB: places - 1, registration ACCEPTED, historique
    API->>DB: INSERT outbox_event registration.accepted, réponse mémorisée, COMMIT
    API-->>C: 202 ACCEPTED (registrationId, Location)

    D->>DB: SELECT non publiés FOR UPDATE SKIP LOCKED
    D->>MQ: publish si.events registration.accepted
    MQ-->>D: publisher confirm
    D->>DB: published_at = now(), COMMIT
    MQ->>W: registration.orchestrator
    MQ->>A: audit (binding tous événements)
    A->>DB: audit_log + processed_events

    W->>DB: processed_events ? non, verrou consultatif registration
    W->>DB: statut PROCESSING
    W->>ERP: find-or-create Lead (email) puis Customer (nom)
    W->>DB: external_mapping erp_lead, erp_customer
    W->>M: cours (shortname), find-or-create utilisateur (email)
    W->>M: is_enrolled ? non, enrol_manual_enrol_users
    W->>DB: mapping moodle_enrolment, statut MOODLE_ENROLLED
    W->>ERP: find Sales Order (po_no) sinon create, puis submit
    W->>DB: mapping erp_sales_order, statut ERP_RECORDED
    W->>DB: 1 transaction : CONFIRMED + outbox registration.confirmed + processed_events
    W->>MQ: ACK

    D->>MQ: publish registration.confirmed (confirmé)
    MQ->>N: notifications
    MQ->>A: audit
    N->>N: SMTP vers Mailpit (Message-ID = eventId)
    N->>DB: notification + processed_events
    N->>MQ: ACK
    C->>API: GET /api/v1/registrations/{id}
    API-->>C: 200 CONFIRMED + identifiants externes + historique
```

---

## 3. Séquence : Transactional Outbox

Sources : `shared/events.py` (`insert_outbox`), `workers/outbox/main.py` (`dispatch_batch`, `run`),
`shared/rabbit.py` (`publish_event`).

```mermaid
sequenceDiagram
    autonumber
    participant P as Producteur (API ou worker)
    participant DB as integration-db
    participant D as outbox-dispatcher
    participant MQ as RabbitMQ

    P->>DB: BEGIN
    P->>DB: changement d'état métier
    P->>DB: INSERT outbox_event (published_at NULL)
    P->>DB: COMMIT (état et événement ensemble, ou rien)

    loop toutes les 0,5 s si le lot est incomplet
        D->>DB: BEGIN, SELECT ... WHERE published_at IS NULL ORDER BY created_at LIMIT 50 FOR UPDATE SKIP LOCKED
        loop pour chaque ligne
            D->>MQ: basic_publish persistant, mandatory, canal en confirm_delivery
            alt ack du broker
                MQ-->>D: confirm
            else nack, non routable ou connexion perdue
                MQ--xD: AMQPError
                D->>DB: publish_attempts + 1, last_error
                Note over D: fin de la boucle du lot
            end
        end
        D->>DB: UPDATE published_at = now() des lignes confirmées
        D->>DB: COMMIT
        opt une AMQPError a eu lieu
            Note over D,MQ: erreur relancée après le COMMIT, reconnexion dans 2 s, les lignes restantes attendent
        end
    end

    Note over D,DB: crash entre le confirm et le COMMIT, republication au redémarrage (at-least-once), les consumers dédupliquent par eventId
```

---

## 4. Idempotence HTTP

Sources : `integration-api/app/routes/registrations.py`, `integration-api/app/registrations.py`.

```mermaid
flowchart TD
    start(["POST /api/v1/registrations"]) --> val{"Body valide ?<br/>(Pydantic)"}
    val -- "non" --> r422["422 VALIDATION_ERROR"]
    val -- "oui" --> hk{"Idempotency-Key<br/>présente ?"}
    hk -- "non" --> r400["400 IDEMPOTENCY_KEY_MISSING"]
    hk -- "oui" --> hash["Empreinte SHA-256<br/>du body normalisé"]
    hash --> begin["BEGIN"]
    begin --> ins["INSERT api_idempotency<br/>ON CONFLICT DO NOTHING<br/>(attend une transaction concurrente<br/>sur la même clé)"]
    ins --> new{"Ligne insérée ?"}
    new -- "non : clé connue" --> same{"Même empreinte ?"}
    same -- "non" --> r409k["409 IDEMPOTENCY_KEY_REUSED_<br/>WITH_DIFFERENT_PAYLOAD"]
    same -- "oui" --> replay["Réponse mémorisée rejouée<br/>(202 ou 409 d'origine)<br/>+ Idempotent-Replayed: true"]
    new -- "oui" --> lock["SELECT training_session<br/>FOR UPDATE"]
    lock --> found{"Session existe ?"}
    found -- "non" --> r404["ROLLBACK : clé non mémorisée<br/>404 SESSION_NOT_FOUND"]
    found -- "oui" --> dup{"Inscription active<br/>même email ?"}
    dup -- "oui" --> r409a["409 ALREADY_REGISTERED"]
    dup -- "non" --> seats{"remaining_seats > 0 ?"}
    seats -- "non" --> r409f["409 SESSION_FULL"]
    seats -- "oui" --> ok["places - 1<br/>INSERT registration ACCEPTED<br/>historique + outbox accepted"]
    ok --> r202["202 ACCEPTED + Location"]
    r409a --> mem["UPDATE api_idempotency<br/>statut + body mémorisés<br/>COMMIT"]
    r409f --> mem
    r202 --> mem

    db503["Base indisponible<br/>(OperationalError, PoolTimeout)"] -.-> r503["503 SERVICE_UNAVAILABLE<br/>Retry-After: 5"]
```

---

## 5. Consumer idempotent, retry et DLQ

Source : `shared/consumer.py` (`_process`, `_give_up`, `_finish`), utilisé par les trois workers.

```mermaid
flowchart TD
    msg(["Message livré<br/>prefetch 1, ACK manuel"]) --> parse{"JSON + enveloppe valides<br/>et schemaVersion = 1 ?"}
    parse -- "non" --> reject["basic_reject requeue=false"]
    reject --> dlx["si.events.dlx"]
    parse -- "oui" --> seen{"eventId dans<br/>processed_events<br/>pour ce consumer ?"}
    seen -- "oui" --> ackdup["ACK sans effet (doublon)"]
    seen -- "non" --> handle["handler.handle(event, attempt)<br/>thread dédié"]
    handle -- "succès" --> mark["INSERT processed_events<br/>ON CONFLICT DO NOTHING"]
    mark --> ack["ACK"]
    handle -- "DuplicateInFlight" --> ackinflight["ACK (autre worker<br/>sur la même inscription)"]
    handle -- "TransientError ou<br/>erreur inattendue" --> left{"attempt inférieur à<br/>MAX_ATTEMPTS = 4 ?"}
    left -- "oui" --> onretry["handler.on_retry<br/>(registration : RETRYING)"]
    onretry --> pubretry["publish si.events.retry<br/>vers QUEUE.retry.2s / 4s / 8s<br/>x-attempt + 1, confirmé"]
    pubretry --> ackr["ACK de l'original"]
    pubretry -. "TTL expiré, exchange par défaut" .-> msg
    left -- "non : épuisé" --> giveup["handler.on_give_up"]
    handle -- "PermanentError" --> giveup
    giveup -- "handled<br/>(compensation, état final)" --> markh["INSERT processed_events"] --> ackh["ACK"]
    giveup -- "dlq" --> pubdlq["publish si.events.dlx<br/>x-error, x-error-kind,<br/>x-attempts, x-consumer, x-failed-at"]
    giveup -- "exception<br/>(compensation impossible)" --> pubdlq
    pubdlq --> ackd["ACK de l'original"]
    pubdlq --> dlx
    dlx --> dlq[("QUEUE.dlq<br/>classic")]

    crash["Crash avant l'ACK"] -. "redélivrance par RabbitMQ" .-> msg
    limit["Plus de 10 redélivrances<br/>x-delivery-limit"] -. "dead-letter par le broker" .-> dlx
```

---

## 6. Machine à états d'une inscription

Sources : `migrations/001_init.sql` (CHECK des statuts), `integration-api/app/registrations.py`
(`ACCEPTED`), `workers/registration/processor.py` (toutes les autres transitions).

```mermaid
stateDiagram-v2
    [*] --> ACCEPTED: API, place réservée (202)

    ACCEPTED --> PROCESSING: début de tentative
    RETRYING --> PROCESSING: retour du message retry
    PROCESSING --> PROCESSING: redélivrance après crash
    MOODLE_ENROLLED --> PROCESSING: redélivrance après crash
    ERP_RECORDED --> PROCESSING: redélivrance après crash

    PROCESSING --> MOODLE_ENROLLED: inscription Moodle + mapping
    MOODLE_ENROLLED --> ERP_RECORDED: Sales Order créé et soumis
    ERP_RECORDED --> CONFIRMED: CONFIRMED + outbox confirmed + processed_events

    ACCEPTED --> RETRYING: erreur temporaire avant la saga
    PROCESSING --> RETRYING: erreur temporaire, essais restants
    MOODLE_ENROLLED --> RETRYING: erreur temporaire ERPNext, essais restants
    ERP_RECORDED --> RETRYING: erreur temporaire (base), essais restants

    MOODLE_ENROLLED --> COMPENSATING: erreur permanente Sales Order, ou essais épuisés
    COMPENSATING --> COMPENSATING: désinscription Moodle en erreur temporaire, retry
    COMPENSATING --> COMPENSATED: désinscription Moodle, place libérée, outbox compensated

    PROCESSING --> COMPENSATING: essais épuisés, mapping moodle_enrolment déjà présent

    PROCESSING --> FAILED: erreur permanente ou essais épuisés, sans inscription Moodle
    RETRYING --> FAILED: dernier essai en échec avant PROCESSING

    CONFIRMED --> [*]
    COMPENSATED --> [*]
    FAILED --> [*]

    note right of COMPENSATING
        Si la désinscription reste impossible
        après les retries, le message part
        en registration.dlq et le statut
        reste COMPENSATING (intervention manuelle)
    end note

    note right of FAILED
        Place libérée, outbox failed,
        désinscription Moodle best-effort,
        message conservé en registration.dlq
    end note
```

Remarques :

- `on_retry` ne modifie jamais un statut final ni `COMPENSATING`.
- Une inscription abandonnée par le broker (`x-delivery-limit`) garde son statut non final. Ce cas
  n'est pas une transition du code, il n'apparaît donc pas dans le diagramme.
- `RETRYING --> FAILED` correspond à une dernière tentative qui échoue avant `_run_saga`, par exemple
  sur un secret absent ou une base indisponible. Le chemin courant vers `FAILED` passe par `PROCESSING`.
- `PROCESSING --> COMPENSATING` se produit quand une tentative reprise après un crash échoue avant
  d'avoir réécrit `MOODLE_ENROLLED`, alors que le mapping `moodle_enrolment` existe déjà.

---

## 7. Séquence : compensation

Sources : `workers/registration/processor.py` (`_run_saga`, `on_give_up`, `_compensate`, `_finalize`),
`shared/consumer.py`.

```mermaid
sequenceDiagram
    autonumber
    participant MQ as RabbitMQ
    participant W as registration-worker
    participant DB as integration-db
    participant M as Moodle
    participant ERP as ERPNext
    participant N as notification-worker

    Note over W,M: l'apprenant est déjà inscrit dans Moodle, statut MOODLE_ENROLLED
    W->>ERP: create Sales Order
    alt erreur permanente (ex. 417 LinkValidationError)
        ERP-->>W: 4xx, PermanentError
        Note over W: _run_saga appelle _compensate directement
    else erreur temporaire répétée (ERPNext arrêté ou gelé)
        ERP--xW: 502 ou timeout, TransientError
        W->>MQ: retry 2 s, 4 s, 8 s (statut RETRYING)
        Note over W: 4e tentative en échec, on_give_up voit le mapping moodle_enrolment
    end

    W->>DB: BEGIN, registration FOR UPDATE, statut COMPENSATING, COMMIT
    W->>M: enrol_manual_unenrol_users (idempotent)
    alt Moodle répond
        M-->>W: ok
        W->>DB: BEGIN, session FOR UPDATE, remaining_seats + 1
        W->>DB: statut COMPENSATED, outbox registration.compensated, COMMIT
        W->>DB: processed_events
        W->>MQ: ACK (pas de DLQ)
        MQ->>N: registration.compensated (via le dispatcher)
        N->>N: email "Inscription annulée"
    else Moodle indisponible
        M--xW: TransientError
        alt appelé depuis _run_saga et essais restants
            W->>MQ: retry, handle reprendra la compensation (statut COMPENSATING)
        else appelé depuis on_give_up
            W->>MQ: publish registration.dlq (x-give-up-error), ACK
            Note over DB: statut reste COMPENSATING, intervention manuelle
        end
    end
```

---

## 8. Topologie RabbitMQ

Source : `shared/rabbit.py` (`declare_topology`, `_CONSUMER_QUEUES`, `_DLQS`, `RETRY_DELAYS`,
`DELIVERY_LIMIT`). Au total : 3 exchanges et 15 queues.

```mermaid
flowchart LR
    disp["outbox-dispatcher"] -- "publish (confirm)" --> ex{{"si.events<br/>topic, durable"}}

    ex -- "registration.accepted" --> qreg[("registration.orchestrator<br/>quorum, delivery-limit 10")]
    ex -- "registration.confirmed<br/>registration.compensated<br/>registration.failed<br/>notification.requested" --> qnot[("notifications<br/>quorum, delivery-limit 10")]
    ex -- "#35; (tout)" --> qaud[("audit<br/>quorum, delivery-limit 10")]

    qreg --> wreg["registration-worker"]
    qnot --> wnot["notification-worker"]
    qaud --> waud["audit-worker"]

    wreg & wnot & waud -- "retry : routing key = nom de la queue retry" --> rex{{"si.events.retry<br/>direct"}}
    rex --> rreg[("registration.orchestrator.retry.2s / 4s / 8s<br/>TTL 2000 / 4000 / 8000 ms")]
    rex --> rnot[("notifications.retry.2s / 4s / 8s")]
    rex --> raud[("audit.retry.2s / 4s / 8s")]
    rreg -. "expiration : exchange par défaut,<br/>routing key registration.orchestrator" .-> qreg
    rnot -. "expiration : exchange par défaut,<br/>routing key notifications" .-> qnot
    raud -. "expiration : exchange par défaut,<br/>routing key audit" .-> qaud

    qreg -. "reject ou delivery-limit<br/>(x-dead-letter-exchange)" .-> dlx{{"si.events.dlx<br/>direct"}}
    qnot -.-> dlx
    qaud -.-> dlx
    wreg & wnot & waud -- "abandon : publish DLQ" --> dlx
    dlx -- "registration.orchestrator" --> dreg[("registration.dlq<br/>classic")]
    dlx -- "notifications" --> dnot[("notifications.dlq<br/>classic")]
    dlx -- "audit" --> daud[("audit.dlq<br/>classic")]
```

| Élément | Arguments déclarés |
|---|---|
| Queues consumer | `x-queue-type=quorum`, `x-dead-letter-exchange=si.events.dlx`, `x-dead-letter-routing-key=<queue>`, `x-delivery-limit=10` |
| Queues retry | `x-message-ttl` = 2000, 4000 ou 8000, `x-dead-letter-exchange=""`, `x-dead-letter-routing-key=<queue>` (type par défaut du vhost) |
| DLQ | `x-queue-type=classic` |
