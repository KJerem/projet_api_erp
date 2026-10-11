# Architecture du mini SI OpenTraining

Ce document décrit l'architecture **telle qu'elle est implémentée**. Chaque affirmation renvoie au fichier
qui la porte (chemins relatifs à la racine du dépôt). Les diagrammes correspondants sont dans
[`docs/mermaid.md`](mermaid.md).

Sommaire :

1. [Vue d'ensemble et responsabilités](#1-vue-densemble-et-responsabilités)
2. [Flux d'une inscription de bout en bout](#2-flux-dune-inscription-de-bout-en-bout)
3. [Modèle de données PostgreSQL](#3-modèle-de-données-postgresql-base-dintégration)
4. [Événements et topologie RabbitMQ](#4-événements-et-topologie-rabbitmq)
5. [Machine à états d'une inscription](#5-machine-à-états-dune-inscription)
6. [Mécanismes de fiabilité](#6-mécanismes-de-fiabilité)
7. [Transactions : ce qui est atomique et ce qui ne peut pas l'être](#7-transactions--ce-qui-est-atomique-et-ce-qui-ne-peut-pas-lêtre)
8. [Observabilité](#8-observabilité)
9. [Limites connues](#9-limites-connues)

---

## 1. Vue d'ensemble et responsabilités

Toute la stack est décrite dans un seul fichier, `docker-compose.yml` (projet Compose `opentraining`,
réseau `si`). Les services de l'Integration Hub partagent une image unique, construite par le
`Dockerfile` racine (Python 3.12, utilisateur non-root `hub` uid/gid 10001). Seule la commande change
d'un service à l'autre.

### 1.1 Composants

| Composant | Service(s) Compose | Rôle | Code / config |
|---|---|---|---|
| **Integration API** | `integration-api` (port 8000, `/docs`) | Reçoit les demandes d'inscription, réserve la place, écrit l'événement dans l'outbox. Expose aussi les sessions, le catalogue agrégé et `/health`, `/ready`. | `integration-api/app/` |
| **Outbox Dispatcher** | `outbox-dispatcher` | Lit `outbox_event` et publie dans RabbitMQ avec publisher confirms. | `workers/outbox/main.py` |
| **Registration Worker** | `registration-worker` | Orchestre la saga : ERPNext (CRM), puis Moodle, puis ERPNext (ERP), et confirme ou compense. | `workers/registration/` |
| **Notification Worker** | `notification-worker` | Envoie les emails (confirmation, annulation, échec) via SMTP. | `workers/notifications/main.py` |
| **Audit Worker** | `audit-worker` | Journalise tous les événements du SI dans `audit_log`. | `workers/audit/main.py` |
| **Migrations** | `migrate` (one-shot) | Applique `migrations/*.sql` et crée les sessions de `infra/catalog.json`. | `shared/migrate.py` |
| **Base d'intégration** | `integration-db` (postgres:17, port hôte 5433) | État de coordination du SI (voir §3). | `migrations/` |
| **Broker** | `rabbitmq` (rabbitmq:4-management, 5672 / 15672) | Transport des événements, retry, DLQ. | `shared/rabbit.py` |
| **ERPNext v16.37.0** | `erpnext-*` (frontend sur 8080) | CRM (Lead, Customer) et ERP (Sales Order, Item, Item Price). | repris de `pwd.yml` (frappe_docker) |
| **Moodle 5.3.0** | `moodle` (8081), `moodle-db` | LMS : comptes apprenants, cours, inscriptions aux cours. | `infra/moodle/` |
| **Mailpit** | `mailpit` (UI 8025, SMTP 1025) | Serveur SMTP de démonstration. | — |
| **Bootstraps** | `erpnext-bootstrap`, `moodle-bootstrap` | Initialisation par les API officielles : setup wizard ERPNext, clé API, articles et prix ; cours Moodle. | `infra/bootstrap/` |

Code partagé entre l'API et les workers (`shared/`) :

| Module | Contenu |
|---|---|
| `shared/config.py` | Configuration lue uniquement depuis l'environnement (`INTEGRATION_DB_PASSWORD` et `RABBITMQ_PASSWORD` obligatoires). |
| `shared/db.py` | Pool psycopg en autocommit. Chaque unité de travail ouvre explicitement `conn.transaction()`. |
| `shared/events.py` | Enveloppe d'événement v1 et `insert_outbox()`. |
| `shared/rabbit.py` | Connexion, topologie déclarée en code, publication persistante. |
| `shared/consumer.py` | Consumer générique : ACK manuel, déduplication, retry/backoff, DLQ. |
| `shared/errors.py` | Classification `TransientError` / `PermanentError` / `DuplicateInFlight`. |
| `shared/erpnext.py`, `shared/moodle.py` | Clients HTTP avec timeout et classification des erreurs. |
| `shared/secrets.py` | Lecture des credentials générés dans le volume `integration-secrets`. |
| `shared/logging_setup.py` | Logs JSON et `correlation_id_var` (contextvar). |

### 1.2 Qui est maître de quelle donnée

| Donnée | Système maître | Copie / référence dans la base d'intégration |
|---|---|---|
| Comptes apprenants, cours, inscriptions pédagogiques | **Moodle** | `external_mapping` (`moodle_user`, `moodle_course`, `moodle_enrolment`) |
| Prospects (Lead), clients entreprises (Customer) | **ERPNext CRM** | `external_mapping` (`erp_lead`, `erp_customer`) |
| Commandes (Sales Order), articles, tarifs | **ERPNext ERP** | `external_mapping` (`erp_sales_order`). Le prix n'est pas copié : il est lu à la demande par le catalogue. |
| Sessions, capacité, **places restantes** | **Base d'intégration** (`training_session`) | Initialisées depuis `infra/catalog.json` par `shared/migrate.py`. |
| Demande d'inscription, statut d'orchestration, historique | **Base d'intégration** (`registration`, `registration_status_history`) | — |
| Événements, journal d'audit, emails envoyés | **Base d'intégration** (`outbox_event`, `audit_log`, `notification`) | — |

La base d'intégration ne remplace ni Moodle ni ERPNext (commentaire d'en-tête de `migrations/001_init.sql`).
Elle porte uniquement l'état de coordination : la réservation de places, l'avancement de la saga,
l'idempotence et la correspondance des identifiants.

`infra/catalog.json` est la source de configuration unique des sessions, cours et articles. Le service
`migrate` en tire les sessions, `erpnext-bootstrap` les articles et leurs prix, `moodle-bootstrap` les cours.

### 1.3 Secrets

- Les mots de passe d'infrastructure sont dans `.env` (gitignoré), généré par `scripts/init-env.py`.
  `.env.example` ne contient aucun secret. Les variables critiques sont requises par la syntaxe
  `${VAR:?…}` de `docker-compose.yml`.
- Les credentials applicatifs (clé API ERPNext, token Moodle) sont **générés au démarrage** dans le
  volume Docker `integration-secrets`. La clé ERPNext est écrite par `infra/bootstrap/bootstrap_erpnext.py`
  dans `erpnext.json`. Le token Moodle est généré par `infra/moodle/setup_ws.php` et écrit dans
  `moodle_token` par `infra/moodle/50-opentraining-setup.sh`. Les deux fichiers sont en mode 0440,
  propriétaire uid/gid 10001 (utilisateur non-root du Hub). Les services
  du Hub montent ce volume en lecture seule (`/secrets:ro`, ancre `x-hub` de `docker-compose.yml`).
- `shared/secrets.py` relit les fichiers à chaque utilisation : une rotation ne demande pas de
  redémarrage. Un fichier absent lève `TransientError` (bootstrap pas terminé), ce qui déclenche un retry.

---

## 2. Flux d'une inscription de bout en bout

Diagramme : *Séquence happy path* dans [`mermaid.md`](mermaid.md#2-séquence--happy-path).

1. **Requête HTTP.** Le client envoie `POST /api/v1/registrations` avec l'en-tête `Idempotency-Key`
   et, s'il le souhaite, `X-Correlation-ID`. Le middleware de `integration-api/app/main.py` valide le
   correlation ID (regex `^[A-Za-z0-9._:-]{1,100}$`) ou en génère un (`corr-<uuid>`), puis le place dans
   `correlation_id_var`. Pydantic valide le body (`integration-api/app/schemas.py` : champs inconnus
   refusés, email en minuscules, `sessionId` au format `^[A-Z0-9][A-Z0-9_-]{1,99}$`). Si le body est
   invalide, l'API répond 422. Si l'en-tête est absent, elle répond 400 (`integration-api/app/routes/registrations.py`).
2. **Transaction locale unique** (`integration-api/app/registrations.py`, `create_registration`) :
   1. `INSERT INTO api_idempotency … ON CONFLICT DO NOTHING` réserve la clé ;
   2. `SELECT … FROM training_session … FOR UPDATE` verrouille la session ;
   3. contrôle de doublon (même email, inscription active), puis contrôle des places restantes ;
   4. `UPDATE training_session SET remaining_seats = remaining_seats - 1` ;
   5. `INSERT registration` (statut `ACCEPTED`), `INSERT registration_status_history` ;
   6. `insert_outbox()` écrit l'événement `registration.accepted` ;
   7. `UPDATE api_idempotency` mémorise la réponse (statut HTTP et body) ;
   8. `COMMIT`.
3. **Réponse 202** avec `{"registrationId", "status": "ACCEPTED"}`, l'en-tête `Location` et
   `X-Correlation-ID`. Le traitement Moodle/ERPNext n'a pas encore commencé.
4. **Outbox Dispatcher** (`workers/outbox/main.py`) : il sélectionne les événements non publiés
   (`FOR UPDATE SKIP LOCKED`), publie chacun sur l'exchange `si.events` avec la routing key
   `registration.accepted` et attend le publisher confirm. Il passe ensuite `published_at` à jour et commite.
5. **RabbitMQ** route le message vers `registration.orchestrator` et vers `audit`, qui est lié à tous
   les événements (binding `#`).
6. **Registration Worker** (`shared/consumer.py` + `workers/registration/processor.py`) :
   1. contrôle de `processed_events` : si l'événement est déjà traité, ACK sans effet ;
   2. verrou consultatif de session `registration:<id>` (`pg_try_advisory_lock`) ;
   3. statut `PROCESSING` ;
   4. **ERPNext CRM** : find-or-create du Lead (par `email_id`), puis du Customer (par `customer_name`) ;
   5. **Moodle** : recherche du cours par shortname, find-or-create de l'utilisateur (par email),
      inscription au cours si elle n'existe pas déjà (`is_enrolled`), puis statut `MOODLE_ENROLLED` ;
   6. **ERPNext ERP** : recherche ou création du Sales Order (`po_no = registrationId`), soumission
      (`docstatus = 1`), puis statut `ERP_RECORDED` ;
   7. **Confirmation** : dans une seule transaction, statut `CONFIRMED`, écriture de
      `registration.confirmed` dans l'outbox et ajout dans `processed_events` ;
   8. ACK du message.
7. Le dispatcher publie `registration.confirmed`, qui part vers `notifications` et `audit`.
8. **Notification Worker** : envoi SMTP vers Mailpit (`Message-ID` = `<eventId@opentraining.local>`),
   puis, dans une même transaction, insertion dans `notification` et dans `processed_events`. ACK.
9. **Audit Worker** : `INSERT INTO audit_log … ON CONFLICT (event_id) DO NOTHING` et `processed_events`. ACK.
10. Le client suit l'avancement avec `GET /api/v1/registrations/{id}`. La réponse contient le statut,
    les identifiants externes (lus dans `external_mapping`) et l'historique complet.

Chaque identifiant externe obtenu est enregistré dans `external_mapping` dès qu'il est connu. Une
tentative suivante reprend donc là où la précédente s'est arrêtée.

---

## 3. Modèle de données PostgreSQL (base d'intégration)

Les migrations sont versionnées dans `migrations/` et appliquées par `shared/migrate.py`. L'application
est faite dans une transaction, sous le verrou consultatif `727001`, et chaque fichier appliqué est
enregistré dans la table `schema_migrations`.

| Table | Migration | Rôle | Contraintes notables |
|---|---|---|---|
| `training_session` | 001 | Session de formation : capacité et places restantes (donnée maître). | `CHECK remaining_seats >= 0`, `CHECK remaining_seats <= capacity` : la base refuse le surbooking même en cas de bug applicatif. |
| `registration` | 001 | Demande d'inscription et statut d'orchestration. | `CHECK status IN (9 valeurs)`. Index unique partiel `uq_registration_active_student (session_id, lower(student_email)) WHERE status NOT IN ('COMPENSATED','FAILED')`. |
| `registration_status_history` | 001 | Historique append-only des transitions, avec détail et correlation ID. | Index `(registration_id, id)`. |
| `api_idempotency` | 001 | Idempotence HTTP : clé, empreinte SHA-256 du body normalisé, réponse mémorisée. | PK `idempotency_key`. `registration_id` NULL pour un refus métier mémorisé. |
| `outbox_event` | 001 | Transactional Outbox : événement complet (enveloppe JSON), état de publication. | Index partiel `ix_outbox_unpublished (created_at) WHERE published_at IS NULL`. |
| `processed_events` | 001 | Déduplication des consumers (at-least-once). | PK `(consumer_name, event_id)`. |
| `external_mapping` | 001 | Correspondance entre identifiants internes et identifiants Moodle/ERPNext. | `UNIQUE (entity_type, internal_id, system_name)`. |
| `audit_log` | 001 | Journal de tous les événements, alimenté par l'audit-worker. | `event_id UNIQUE`. |
| `notification` | 002 | Trace des emails envoyés (une ligne par événement notifié). | PK `event_id`, index `registration_id`. |
| `schema_migrations` | `shared/migrate.py` | Versions de migration appliquées. | PK `version`. |

Valeurs de `external_mapping.entity_type` (`workers/registration/processor.py`) : `erp_lead`,
`erp_customer`, `erp_sales_order` (système `erpnext`) et `moodle_user`, `moodle_course`,
`moodle_enrolment` (système `moodle`). Pour toutes ces lignes, `internal_id` est le `registrationId`.

Valeurs de `processed_events.consumer_name` : `registration-orchestrator`, `notification-worker`,
`audit-worker`.

---

## 4. Événements et topologie RabbitMQ

### 4.1 Enveloppe d'événement (schemaVersion 1)

Définie dans `shared/events.py` (`new_event`) :

```json
{
  "eventId": "uuid v4",
  "eventType": "registration.accepted",
  "schemaVersion": 1,
  "correlationId": "corr-…",
  "occurredAt": "2026-…Z",
  "payload": { "registrationId": "…", "sessionId": "…", "student": {…}, "company": {…} }
}
```

| `eventType` (= routing key) | Produit par | Contenu spécifique du payload |
|---|---|---|
| `registration.accepted` | API (`integration-api/app/registrations.py`) | `student`, `company`, `sessionId` |
| `registration.confirmed` | `_confirm` (`workers/registration/processor.py`) | identifiants `erpLeadId`, `erpCustomerId`, `erpSalesOrderId`, `moodleUserId`, `courseId`, `courseShortname` |
| `registration.compensated` | `_finalize` via `_compensate` | `reason` |
| `registration.failed` | `_finalize` via `_fail` | `reason` |

Propriétés AMQP (`shared/rabbit.py`, `event_properties`) : `delivery_mode` persistant,
`message_id = eventId`, `correlation_id`, `type = eventType`, et les en-têtes `schemaVersion` et
`x-correlation-id`. Un consumer refuse toute `schemaVersion` différente de 1 (`parse_event` dans
`shared/consumer.py`) : le message est rejeté vers la DLQ au lieu d'être mal interprété.

### 4.2 Topologie (déclarée en code)

La topologie est déclarée par `declare_topology()` dans `shared/rabbit.py`. Le dispatcher et chaque
consumer l'appellent à chaque connexion. Les déclarations AMQP sont idempotentes, donc il n'y a pas de
`definitions.json`.

**Exchanges** (tous durables) :

| Exchange | Type | Usage |
|---|---|---|
| `si.events` | topic | Publication des événements métier par le dispatcher. |
| `si.events.dlx` | direct | Dead-letter exchange. La routing key est le nom de la queue d'origine. |
| `si.events.retry` | direct | Republication différée. La routing key est le nom de la queue de retry. |

**Queues consumer** : type quorum, durables, avec les arguments `x-dead-letter-exchange = si.events.dlx`,
`x-dead-letter-routing-key = <nom de la queue>` et `x-delivery-limit = 10` :

| Queue | Bindings sur `si.events` | Consumer (`consumer_name`) |
|---|---|---|
| `registration.orchestrator` | `registration.accepted` | registration-worker (`registration-orchestrator`) |
| `notifications` | `registration.confirmed`, `registration.compensated`, `registration.failed`, `notification.requested` | notification-worker (`notification-worker`) |
| `audit` | `#` (tous les événements) | audit-worker (`audit-worker`) |

**DLQ** : type classic, durables, sans delivery-limit. Elles sont liées à `si.events.dlx` avec pour
routing key le nom de la queue d'origine.

| DLQ | Reçoit les messages abandonnés de |
|---|---|
| `registration.dlq` | `registration.orchestrator` |
| `notifications.dlq` | `notifications` |
| `audit.dlq` | `audit` |

Les DLQ sont classiques pour une raison précise. Inspecter un message (peek + requeue) incrémente son
compteur de livraisons. Une quorum queue finirait par l'abandonner une fois sa limite dépassée, alors
qu'une queue classique le conserve (commentaire dans `declare_topology`).

**Queues de retry** : 3 par queue consumer, soit 9 au total. Elles sont durables et déclarées sans
`x-queue-type`, donc avec le type par défaut du vhost :

| Queue | `x-message-ttl` | Liée à `si.events.retry` par | À expiration |
|---|---|---|---|
| `<queue>.retry.2s` | 2000 ms | `<queue>.retry.2s` | DLX `""` (exchange par défaut), routing key `<queue>` |
| `<queue>.retry.4s` | 4000 ms | `<queue>.retry.4s` | idem |
| `<queue>.retry.8s` | 8000 ms | `<queue>.retry.8s` | idem |

`<queue>` vaut `registration.orchestrator`, `notifications` ou `audit`. Un message expiré revient
**directement** dans sa queue d'origine par l'exchange par défaut. Il ne repasse pas par `si.events` :
un retry de l'orchestrateur n'est donc pas redistribué à l'audit ni aux notifications.

Au total : 3 exchanges et 15 queues (3 consumer, 3 DLQ, 9 retry).

### 4.3 Comment un message arrive en DLQ

| Cas | Mécanisme | En-têtes visibles |
|---|---|---|
| JSON invalide, enveloppe incomplète, `schemaVersion` ≠ 1 | `basic_reject(requeue=False)`, dead-lettering par le broker | `x-death` (reason `rejected`) |
| `PermanentError`, ou retries épuisés, avec un handler qui répond `"dlq"` | Le consumer publie lui-même sur `si.events.dlx` (confirmé), puis ACK | `x-error`, `x-error-kind` (`permanent` / `retries exhausted`), `x-attempts`, `x-consumer`, `x-failed-at` |
| `on_give_up` lève une exception (compensation impossible) | Publication DLQ par le consumer | les mêmes, plus `x-give-up-error` |
| Message redélivré plus de 10 fois (crash systématique du worker) | `x-delivery-limit` de la quorum queue | `x-death` (reason `delivery_limit`) |

`scripts/inspect-dlq.py [registration.dlq|notifications.dlq|audit.dlq]` affiche ces informations sans
consommer la DLQ.

---

## 5. Machine à états d'une inscription

Les statuts autorisés par la contrainte `CHECK` de `migrations/001_init.sql` sont `ACCEPTED`,
`PROCESSING`, `MOODLE_ENROLLED`, `ERP_RECORDED`, `CONFIRMED`, `RETRYING`, `COMPENSATING`, `COMPENSATED`
et `FAILED`. Les états finaux (`TERMINAL_STATUSES`, `workers/registration/processor.py`) sont
`CONFIRMED`, `COMPENSATED` et `FAILED`.

Chaque transition passe par `_set_status`, qui met à jour `registration` et ajoute une ligne dans
`registration_status_history` dans la même transaction. Seul `ACCEPTED` est écrit ailleurs, par l'API.

| Transition | Déclencheur (code) |
|---|---|
| ∅ → `ACCEPTED` | L'API réserve une place (`create_registration`). |
| `ACCEPTED` / `RETRYING` / `PROCESSING` / `MOODLE_ENROLLED` / `ERP_RECORDED` → `PROCESSING` | Début de chaque tentative de saga (`_run_saga`). La tentative suit un retry, ou une redélivrance après un crash du worker (y compris entre le commit d'`ERP_RECORDED` et celui de `_confirm`). |
| `PROCESSING` → `MOODLE_ENROLLED` | Inscription Moodle faite et mapping `moodle_enrolment` enregistré. |
| `MOODLE_ENROLLED` → `ERP_RECORDED` | Sales Order créé ou retrouvé, puis soumis. |
| `ERP_RECORDED` → `CONFIRMED` | `_confirm` : statut, outbox `registration.confirmed` et `processed_events` dans une seule transaction. |
| état non final ≠ `COMPENSATING` → `RETRYING` | `on_retry` : `TransientError` (ou erreur inattendue) avec des tentatives restantes. Cela couvre aussi `ACCEPTED`, si l'échec survient avant `PROCESSING` (secret absent, base indisponible). |
| `MOODLE_ENROLLED` → `COMPENSATING` | `PermanentError` à l'étape Sales Order (`_run_saga`, puis `_compensate`). |
| non final, mapping `moodle_enrolment` présent → `COMPENSATING` | Retries épuisés : `on_give_up` puis `_compensate`. |
| `COMPENSATING` → `COMPENSATING` | La désinscription Moodle échoue de façon transitoire : le message est retenté (`on_retry` ne touche pas au statut) et `handle` reprend la compensation. |
| `COMPENSATING` → `COMPENSATED` | Désinscription Moodle réussie : `_finalize` libère la place et écrit `registration.compensated` dans l'outbox. |
| non final, sans mapping `moodle_enrolment` → `FAILED` | `PermanentError` ou retries épuisés avant l'inscription Moodle : `on_give_up` puis `_fail`. Le message part aussi en `registration.dlq` pour analyse. |

Deux situations laissent une inscription dans un état non final et demandent une intervention :

- `on_give_up` lève une exception (Moodle indisponible pendant la désinscription) : le statut reste
  `COMPENSATING` et le message est en DLQ avec `x-give-up-error` ;
- le broker abandonne le message (`x-delivery-limit`, crash répété du worker) : le message est en DLQ
  sans que `on_give_up` ait été appelé, et le statut reste celui du moment de l'abandon (par exemple
  `PROCESSING`).

Comportements de garde :

- `handle` ne fait rien si l'inscription est déjà dans un état final, ce qui rend un rejeu tardif
  inoffensif. Si l'état est `COMPENSATING`, `handle` reprend la compensation au lieu de la saga.
- `_confirm` et `_finalize` relisent le statut sous `SELECT … FOR UPDATE` et sortent sans rien faire
  si un état final est déjà atteint. La place n'est donc libérée qu'une seule fois.
- `on_give_up` renvoie `"handled"` pour une inscription déjà finale ou compensée, ce qui donne un ACK
  sans passage en DLQ. Il renvoie `"dlq"` après `FAILED`, ou si l'inscription est inconnue (événement orphelin).

---

## 6. Mécanismes de fiabilité

### 6.1 Idempotence HTTP (`Idempotency-Key`)

Code : `integration-api/app/registrations.py`.

- L'en-tête est obligatoire : sans lui, l'API répond 400 `IDEMPOTENCY_KEY_MISSING`. Sa longueur
  maximale est de 255 caractères et les espaces en bordure sont retirés.
- L'empreinte est un SHA-256 du body **normalisé** (`model_dump` puis JSON à clés triées). L'ordre des
  clés, les espaces en bordure et la casse de l'email n'ont donc pas d'effet sur l'empreinte.
- La clé est réservée par `INSERT … ON CONFLICT DO NOTHING`, dans la même transaction que la réservation
  de place. Deux requêtes concurrentes avec la même clé se sérialisent sur l'index unique : la seconde
  attend le `COMMIT` (ou le `ROLLBACK`) de la première.
- Si la clé est connue et que l'empreinte est identique, l'API renvoie la réponse mémorisée avec le
  même statut HTTP (202 ou 409) et l'en-tête `Idempotent-Replayed: true`.
- Si la clé est connue avec une empreinte différente, l'API répond 409
  `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD`.
- Les refus métier `SESSION_FULL` et `ALREADY_REGISTERED` (409) sont mémorisés. Le 404
  `SESSION_NOT_FOUND` ne l'est pas : il lève une exception, donc un `ROLLBACK`, et la clé reste libre.

### 6.2 Concurrence (dernière place)

- `SELECT … FROM training_session WHERE id = … FOR UPDATE` sérialise toutes les réservations d'une
  même session. Le contrôle `remaining_seats <= 0` et la décrémentation se font sous ce verrou.
- Deux filets de sécurité en base : `CHECK (remaining_seats >= 0)` et l'index unique partiel
  `uq_registration_active_student`, qui interdit deux inscriptions actives du même email sur une session.
- Côté worker, deux mécanismes complètent ce dispositif :
  - **verrou consultatif de session** par inscription (`_registration_lock`, `pg_try_advisory_lock`) :
    si un autre worker traite déjà la même inscription, le doublon est acquitté (`DuplicateInFlight`
    → ACK). Le verrou est libéré automatiquement si le worker meurt, car la connexion se ferme ;
  - **verrou consultatif transactionnel** autour de chaque find-or-create externe (`_find_or_create`,
    `pg_advisory_xact_lock` sur `erp_lead:<email>`, `erp_customer:<entreprise>`, `moodle_user:<email>`).
    Il est nécessaire parce qu'ERPNext accepte deux Customer de même nom (le second reçoit le
    suffixe « - 1 »).
- `_confirm` et `_finalize` utilisent `SELECT status … FOR UPDATE` sur `registration`, et `_finalize`
  verrouille aussi la session avant de rendre la place.

### 6.3 Transactional Outbox

- L'événement est inséré dans `outbox_event` **dans la transaction métier** de l'appelant
  (`insert_outbox`, `shared/events.py`). Les appelants sont l'API pour `accepted`, `_confirm` pour
  `confirmed` et `_finalize` pour `compensated` et `failed`. Les changements d'état qui publient un
  événement (`ACCEPTED`, `CONFIRMED`, `COMPENSATED`, `FAILED`) n'existent donc jamais sans lui, et
  inversement. Les statuts intermédiaires (`PROCESSING`, `MOODLE_ENROLLED`, `ERP_RECORDED`, `RETRYING`,
  `COMPENSATING`) ne publient rien : ils servent au suivi via `GET /registrations/{id}`.
- Algorithme du dispatcher (`workers/outbox/main.py`, `dispatch_batch`) :
  1. `SELECT … WHERE published_at IS NULL ORDER BY created_at LIMIT 50 FOR UPDATE SKIP LOCKED` ;
  2. publication de chaque ligne (`mandatory=True`, canal en `confirm_delivery()`) : l'appel bloque
     jusqu'à l'ack du broker et lève une exception en cas de nack ou si le message n'est pas routable ;
  3. `UPDATE published_at = now()` pour les lignes confirmées, puis `COMMIT`.
- En cas d'erreur AMQP au milieu d'un lot, les événements déjà confirmés sont commités comme publiés,
  `publish_attempts` et `last_error` de la ligne en échec sont incrémentés, puis le dispatcher se reconnecte.
- `SKIP LOCKED` permet de lancer plusieurs dispatchers sans qu'un même événement soit publié deux fois
  en même temps.
- Un crash entre le confirm du broker et le `COMMIT` entraîne une republication. C'est le cas
  at-least-once assumé : les consumers dédupliquent par `eventId`.

### 6.4 Consumers idempotents (at-least-once et déduplication)

Code : `shared/consumer.py`.

- `basic_qos(prefetch_count=1)` et `auto_ack=False` : l'ACK n'est envoyé qu'en fin de traitement.
  Un crash avant l'ACK entraîne une redélivrance par RabbitMQ.
- Le traitement s'exécute dans un thread dédié, ce qui laisse passer les heartbeats AMQP. L'ACK et les
  publications sont renvoyés au thread de la connexion (`add_callback_threadsafe`).
- Avant le handler, une ligne `processed_events` pour `(consumer_name, eventId)` produit un ACK sans
  effet. Après un succès, la ligne est insérée avec `ON CONFLICT DO NOTHING`.
- Les handlers enregistrent `processed_events` **dans la même transaction** que leur effet local :
  `_confirm` (registration), insertion dans `notification` (notifications), insertion dans `audit_log` (audit).
- Les effets externes sont rendus idempotents par `external_mapping` et par une recherche préalable sur
  une clé naturelle : email pour le Lead et l'utilisateur Moodle, nom pour le Customer, `po_no` pour le
  Sales Order, et `is_enrolled` avant l'inscription au cours.

### 6.5 Retry avec backoff et classification des erreurs

- Classification (`shared/erpnext.py`, `shared/moodle.py`) :
  - **temporaire** : timeout, erreur de transport, HTTP 429 ou 5xx, réponse Moodle non JSON, codes
    d'erreur Moodle de type base de données (`dmlreadexception`, …), secret absent ;
  - **permanente** : autre HTTP 4xx (par exemple le 417 `LinkValidationError` d'ERPNext pour un article
    inexistant), exception métier Moodle renvoyée en HTTP 200, cours Moodle introuvable.
  - Une exception inattendue (base indisponible, bug) est traitée comme temporaire.
- `MAX_ATTEMPTS = 4` : 1 tentative immédiate puis 3 retries après 2, 4 et 8 s (`RETRY_DELAYS`,
  `shared/rabbit.py`). La fenêtre totale est d'environ 14 s.
- Pour un retry, le consumer republie le message sur `si.events.retry` vers `<queue>.retry.Ns` avec les
  en-têtes `x-attempt = n+1` et `x-last-error`, **attend le confirm**, puis acquitte l'original. Un crash
  entre les deux produit au pire un doublon, jamais une perte.
- Le registration-worker passe l'inscription en `RETRYING` (`on_retry`). Les autres workers ne font rien
  dans `on_retry`.

### 6.6 DLQ

Voir §4.3. Choix de chaque handler une fois les tentatives épuisées :

- registration : `"handled"` (compensation réussie, ACK sans DLQ) ou `"dlq"` (`FAILED` ou événement orphelin) ;
- notifications : `"dlq"`, l'email n'a pas été envoyé et peut être renvoyé à la main ;
- audit : `"dlq"`.

### 6.7 Compensation (saga)

Code : `_compensate` et `_finalize` dans `workers/registration/processor.py`.

- **Déclencheurs** : `PermanentError` à l'étape Sales Order, ou retries épuisés alors que l'apprenant
  est déjà inscrit dans Moodle (mapping `moodle_enrolment` présent).
- **Étapes** :
  1. statut `COMPENSATING` (transaction) ;
  2. `enrol_manual_unenrol_users`, qui est idempotent ;
  3. `_finalize` : verrou de la session, `remaining_seats + 1`, statut `COMPENSATED`, outbox
     `registration.compensated`, le tout dans une seule transaction ;
  4. ACK.
- La notification d'annulation est ensuite envoyée par le notification-worker.
- `_fail` tente aussi une désinscription Moodle *best-effort* quand les mappings `moodle_user` et
  `moodle_course` existent. Cela couvre le cas d'une inscription Moodle réussie dont la réponse s'est
  perdue (timeout).
- La compensation ne touche pas ERPNext. Le Lead et le Customer sont conservés, ce sont des données CRM
  légitimes. Voir §9 pour le Sales Order.

### 6.8 Timeouts

- Tous les appels HTTP sortants ont un timeout `HTTP_TIMEOUT_SECONDS` (5 s par défaut,
  `shared/config.py`), appliqué par `httpx.Client(timeout=…)`. L'envoi SMTP utilise le même délai
  (`workers/notifications/main.py`).
- Base d'intégration : `connect_timeout=5` dans le DSN et `timeout=10` pour obtenir une connexion du
  pool (`shared/db.py`). `/ready` prend une connexion avec `timeout=2`.
- Catalogue (`integration-api/app/catalog.py`) : Moodle et ERPNext sont interrogés **en parallèle**.
  La latence est donc bornée par l'appel le plus lent, pas par la somme des deux. En mode strict, une
  source indisponible donne 503 `CATALOG_DEPENDENCY_UNAVAILABLE`. Avec `?partial=true`, l'API répond
  200 avec les champs manquants à `null` et l'en-tête `X-Catalog-Degraded`.
- RabbitMQ : heartbeat 30 s, `blocked_connection_timeout` 30 s, reconnexion avec backoff plafonné à 15 s
  (`connect`, `shared/rabbit.py`).

### 6.9 Correlation ID

Le correlation ID suit le chemin suivant :

1. il est reçu ou généré par le middleware (`integration-api/app/main.py`) et renvoyé dans l'en-tête de
   réponse `X-Correlation-ID` ;
2. il est stocké dans `registration.correlation_id`, `registration_status_history.correlation_id` et
   `outbox_event.correlation_id` ;
3. il est porté par l'enveloppe (`correlationId`), par la propriété AMQP `correlation_id` et par
   l'en-tête `x-correlation-id` ;
4. le consumer le replace dans `correlation_id_var` pour la durée du traitement ;
5. il est envoyé dans l'en-tête `X-Correlation-ID` des appels ERPNext et Moodle (clients de `shared/`) ;
6. il apparaît dans l'en-tête `X-Correlation-ID` de l'email et dans `audit_log.correlation_id` et
   `notification.correlation_id`.

Une requête SQL ou un filtre de logs sur ce correlation ID retrouve donc tout le parcours d'une inscription.

### 6.10 Logs JSON

`shared/logging_setup.py` écrit une ligne JSON par événement sur stdout, avec les champs `timestamp`,
`level`, `service`, `logger`, `correlationId` (s'il est connu), `message`, la trace d'exception le cas
échéant, et tous les champs `extra` (`registrationId`, `eventId`, `attempt`, `latencyMs`, …).

- Les access logs d'uvicorn sont désactivés. Le middleware les remplace par sa propre ligne, avec
  méthode, chemin, statut et `durationMs`, sauf pour `/health`.
- Chaque appel ERPNext ou Moodle est journalisé avec son statut HTTP et sa latence.

---

## 7. Transactions : ce qui est atomique et ce qui ne peut pas l'être

### 7.1 Transactions ACID locales (PostgreSQL d'intégration)

| Unité atomique | Contenu | Fichier |
|---|---|---|
| Acceptation d'une inscription | clé d'idempotence, verrou de session, décrément de place, `registration`, historique, outbox `accepted`, réponse mémorisée | `integration-api/app/registrations.py` |
| Chaque changement de statut | `UPDATE registration` et `INSERT registration_status_history` | `_set_status` (`workers/registration/processor.py`) |
| Confirmation | statut `CONFIRMED`, outbox `confirmed`, `processed_events` | `_confirm` |
| Fin en échec (`COMPENSATED` / `FAILED`) | place rendue, statut, outbox `compensated` / `failed` | `_finalize` |
| Find-or-create externe | verrou consultatif transactionnel tenu pendant la recherche et la création (l'appel externe lui-même n'est pas transactionnel) | `_find_or_create` |
| Publication d'un lot | `published_at` des événements confirmés par le broker | `workers/outbox/main.py` |
| Notification | ligne `notification` et `processed_events` | `workers/notifications/main.py` |
| Audit | ligne `audit_log` et `processed_events` | `workers/audit/main.py` |
| Migration | verrou, scripts SQL, `schema_migrations` ; sessions du catalogue | `shared/migrate.py` |

### 7.2 Ce qui ne peut pas être dans une transaction

| Frontière | Problème | Réponse apportée |
|---|---|---|
| PostgreSQL et RabbitMQ | Pas de transaction distribuée entre base et broker. | Transactional Outbox, publisher confirms, at-least-once et déduplication. |
| Base d'intégration, Moodle et ERPNext | Trois bases distinctes, chaque appel REST est sa propre transaction. | Saga orchestrée : étapes idempotentes (`external_mapping` et recherche par clé naturelle), retry, compensation. |
| Traitement et ACK RabbitMQ | L'ACK n'est pas transactionnel avec la base. | ACK en dernier, `processed_events`, handlers idempotents. |
| Envoi SMTP | SMTP n'est pas transactionnel. | Contrôle de `processed_events` avant l'envoi, trace après. Un doublon reste possible (voir §9), avec `Message-ID` égal à l'`eventId`. |
| Création externe et enregistrement du mapping | Un crash entre les deux laisse un objet externe sans mapping. | À la tentative suivante, `find()` le retrouve : email, nom, `po_no`, `is_enrolled`. |

---

## 8. Observabilité

| Outil | Ce qu'il donne | Source |
|---|---|---|
| `GET /health` | Liveness : `{"status":"UP"}`. Utilisé par le healthcheck Compose de `integration-api`. | `integration-api/app/routes/health.py`, `docker-compose.yml` |
| `GET /ready` | Readiness PostgreSQL et RabbitMQ (200, ou 503 si une dépendance est `DOWN`) et `pendingOutboxEvents` (événements non publiés). | `integration-api/app/routes/health.py` |
| `GET /api/v1/registrations/{id}` | Statut, tentatives, dernière erreur, identifiants externes, historique horodaté. | `integration-api/app/registrations.py` |
| `GET /api/v1/sessions[/{id}]` | Capacité, places restantes, inscriptions actives. | `integration-api/app/routes/sessions.py` |
| `docker compose logs <service>` | Logs JSON corrélés (§6.10). | `shared/logging_setup.py` |
| Table `audit_log` | Journal de tous les événements publiés, avec correlation ID. | `workers/audit/main.py` |
| Table `registration_status_history` | Chronologie de chaque inscription. | `migrations/001_init.sql` |
| Table `outbox_event` | `published_at`, `publish_attempts`, `last_error` du dispatcher. | `workers/outbox/main.py` |
| `scripts/inspect-dlq.py` | Contenu d'une DLQ sans la consommer : type, eventId, motif, tentatives, consumer, erreur. | `scripts/inspect-dlq.py` |
| RabbitMQ Management (port 15672) | Profondeur des queues, consumers, débits. | `rabbitmq:4-management` |
| Mailpit (port 8025) | Emails envoyés, avec les en-têtes `X-Correlation-ID` et `X-Registration-ID`. | `workers/notifications/main.py` |
| `scripts/check-infra.sh` | Vérification de l'état de la stack. | `scripts/check-infra.sh` |

---

## 9. Limites connues

1. **Doublon tardif côté ERPNext.** Si ERPNext reçoit une création (Lead, Customer ou Sales Order)
   mais que la réponse dépasse le timeout, le worker classe l'erreur comme temporaire. La recherche
   préalable de la tentative suivante retrouve normalement l'objet. Un doublon reste toutefois possible
   si ERPNext le crée *après* cette recherche.
2. **Sales Order non annulé lors d'une compensation.** `_compensate` ne touche pas ERPNext. Si un Sales
   Order a été créé avant l'erreur (par exemple une création réussie puis une soumission en échec), il
   reste dans ERPNext, en brouillon ou soumis, alors que l'inscription est `COMPENSATED`. Son `po_no`
   (égal au `registrationId`) permet de le retrouver.
3. **Compensation impossible.** Si Moodle est indisponible pendant la désinscription après épuisement
   des retries, le message part en `registration.dlq` (en-tête `x-give-up-error`) et le statut reste
   `COMPENSATING`. Une intervention manuelle est nécessaire. La place n'est pas rendue tant que la
   compensation n'a pas abouti.
4. **Abandon par `x-delivery-limit`.** Au-delà de 10 redélivrances sans ACK (11e livraison, crash répété ;
   comportement vérifié sur RabbitMQ 4.3.6, version épinglée : une fermeture de connexion sans ACK
   incrémente bien `x-delivery-count`), le broker envoie le
   message en DLQ sans appeler `on_give_up`. L'inscription garde son statut non final et la place reste
   réservée.
5. **Email dupliqué.** Un crash du notification-worker entre l'envoi SMTP et le `COMMIT` produit un
   second email au rejeu. Le `Message-ID` dérivé de l'`eventId` permet aux clients mail de repérer le doublon.
6. **Redémarrage lent d'ERPNext.** ERPNext met environ 22 s à redémarrer, plus que la fenêtre de retry
   (environ 14 s). Une coupure ERPNext pendant l'étape Sales Order se termine donc en compensation, pas
   en confirmation retardée.
7. **`FAILED` avec inscription Moodle orpheline.** Si l'inscription Moodle a réussi mais que sa réponse
   s'est perdue, et que Moodle reste indisponible, la désinscription *best-effort* de `_fail` échoue.
   L'apprenant peut alors rester inscrit dans Moodle alors que l'inscription est `FAILED`.
