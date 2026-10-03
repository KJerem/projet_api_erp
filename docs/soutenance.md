# Soutenance : réponses aux 25 questions

Chaque réponse suit le même plan :

1. une réponse directe, à donner à l'oral ;
2. **Dans notre code**, les fichiers et fonctions où le mécanisme est implémenté ;
3. **Preuve**, le script qui le démontre sur la vraie stack.

Les scripts Python côté hôte se lancent avec le venv du projet :

- Windows : `.venv/Scripts/python scripts/<script>.py`
- Linux et macOS : `.venv/bin/python scripts/<script>.py`

Rappel du flux nominal :

```
Client ─POST─► Integration API ─(1 transaction PG : clé + place + inscription + outbox)─► 202
                                                  │
                     outbox-dispatcher ◄──────────┘  (publisher confirms)
                            │
                     si.events (topic) ─► registration.orchestrator ─► registration-worker
                            │                                           ERPNext Lead + Customer
                            │                                           Moodle user + inscription
                            │                                           ERPNext Sales Order
                            │                                           CONFIRMED + outbox registration.confirmed
                            ├─► notifications ─► notification-worker ─► SMTP (Mailpit)
                            └─► audit ─────────► audit-worker ────────► audit_log
```

---

## 1. Pourquoi ne pas appeler Moodle directement depuis le frontend ?

Le frontend devrait alors détenir un token Web Services Moodle, qui donne des droits d'administration
(création d'utilisateurs, inscriptions) : n'importe quel visiteur pourrait le lire dans le navigateur.
Ensuite, une inscription ne concerne pas seulement Moodle. Il faut aussi réserver une place, alimenter
ERPNext (Lead, Customer, Sales Order) et notifier l'apprenant. Le navigateur ne peut garantir ni
l'atomicité, ni les retries, ni la compensation entre ces systèmes. Enfin, le frontend serait couplé au
format propre de Moodle (fonctions `core_*`, erreurs renvoyées en HTTP 200), alors que notre API expose un
contrat stable et versionné (`/api/v1`).

**Dans notre code :** les tokens sont lus côté serveur uniquement, dans le volume `integration-secrets` (`shared/secrets.py`). Seul le Hub appelle Moodle (`shared/moodle.py`, `MoodleClient`). La règle métier des places est appliquée dans `integration-api/app/registrations.py` (`create_registration`). La traduction des erreurs Moodle (HTTP 200 + objet `exception`) en `PermanentError` ou `TransientError` est faite dans `MoodleClient.call`.

**Preuve :** `scripts/test-happy-path.py` crée l'inscription par un seul appel à l'API, puis vérifie dans Moodle, par ses Web Services, que l'utilisateur et l'inscription au cours existent.

## 2. Pourquoi RabbitMQ ?

Pour découpler l'acceptation de la demande, qui est rapide, de son traitement dans Moodle et ERPNext, qui est
lent et peut tomber en panne. Les messages sont durables et attendent dans la queue si un worker ou un logiciel
est arrêté. L'ACK manuel garantit qu'un message n'est retiré qu'une fois traité. Les queues de retry à TTL et
les DLQ donnent le backoff et l'isolement des messages non traitables. L'exchange `topic` diffuse un même
événement à plusieurs consumers indépendants (orchestration, notification, audit) sans que le producteur les connaisse.

**Dans notre code :** la topologie est déclarée en code dans `shared/rabbit.py` (`declare_topology`) : exchanges `si.events`, `si.events.dlx` et `si.events.retry`, queues quorum avec `x-delivery-limit` 10. Le consumer générique `shared/consumer.py` gère l'ACK manuel, `prefetch_count=1`, le retry et la DLQ.

**Preuve :** `scripts/test-failure-moodle.py` (Moodle gelé : le message attend puis aboutit) et `scripts/test-notifications.py` (le même événement est reçu par le notifier et par l'audit).

## 3. Pourquoi 202 pour le POST ?

Au moment de la réponse, la demande est acceptée et persistée : place réservée, événement dans l'outbox. Le
travail demandé, c'est-à-dire l'inscription Moodle et la commande ERPNext, n'est pas encore fait, et il peut
même échouer puis être compensé. `201 Created` affirmerait que la ressource finale existe. `202 Accepted`
signifie précisément « accepté pour traitement asynchrone ». Le client suit l'avancement grâce à l'en-tête
`Location` (`GET /api/v1/registrations/{id}` et son historique de statuts).

**Dans notre code :** `integration-api/app/routes/registrations.py` (`status_code=202`, en-tête `Location`). L'API n'appelle aucun système externe dans `create_registration`. À l'inverse, `POST /api/v1/sessions` renvoie `201`, car la session est créée de façon synchrone et complète (`routes/sessions.py`).

**Preuve :** `scripts/test-happy-path.py` (202, puis passage par les états intermédiaires jusqu'à `CONFIRMED`) et `scripts/test-http-codes.py` (202 + `Location`, 201 + `Location`).

## 4. Comment fonctionne votre Idempotency-Key ?

Le client envoie une clé unique par intention d'inscription. Dans **la même transaction** que la réservation,
l'API fait `INSERT … ON CONFLICT DO NOTHING` dans `api_idempotency` (clé primaire sur la clé), avec
l'empreinte SHA-256 du body normalisé. Si l'insertion réussit, c'est une première demande : traitement, puis
mémorisation du code et du body de la réponse avant le COMMIT. Si la clé existe déjà avec la même empreinte, la
réponse mémorisée est rejouée telle quelle, avec `Idempotent-Replayed: true`, sans nouvelle place ni nouvel
événement. Deux requêtes simultanées portant la même clé sont sérialisées par l'index unique : la seconde
attend le COMMIT de la première, puis rejoue sa réponse.

**Dans notre code :** `integration-api/app/registrations.py` : `request_hash` (JSON trié, email en minuscules, espaces retirés) et `create_registration` (étapes 1 et 3). Table `api_idempotency` dans `migrations/001_init.sql`. En-tête de rejeu dans `routes/registrations.py`. Les refus métier (`SESSION_FULL`, `ALREADY_REGISTERED`) sont mémorisés. Le 404 ne l'est pas : l'exception provoque un ROLLBACK, ce qui libère la clé.

**Preuve :** `scripts/test-idempotency.py` (rejeu, body normalisé identique, 10 requêtes simultanées avec la même clé : un seul `registrationId`, une seule place consommée) et `scripts/test-happy-path.py` (rejeu : toujours 1 utilisateur Moodle, 1 Lead, 1 Sales Order, 1 email).

## 5. Même clé, autre body ?

`409 Conflict` avec `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD`. Ce n'est pas un rejeu, puisque
l'intention a changé, mais ce n'est pas non plus une nouvelle demande, puisque le client a réutilisé une clé :
c'est presque toujours un bug client. Rien n'est modifié en base et la réponse d'origine reste associée à la
clé. La comparaison porte sur le body normalisé : un email en majuscules ou des clés JSON dans un autre ordre
ne déclenchent **pas** ce 409.

**Dans notre code :** `integration-api/app/registrations.py`, comparaison `previous["request_hash"] != req_hash`, qui lève une `ApiError(409, …)`.

**Preuve :** `scripts/test-idempotency.py`, contrôles « même clé + body différent -> 409 » et « code IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD ».

## 6. Pourquoi une contrainte UNIQUE ?

Une vérification applicative du type « je lis, puis j'insère si absent » est sujette aux courses entre requêtes
ou workers concurrents. La contrainte UNIQUE est appliquée par PostgreSQL de façon atomique, quel que soit le
nombre de processus. C'est le filet de sécurité qui reste valable même en cas de bug du code. Elle sert aussi de
**mécanisme** : `INSERT … ON CONFLICT` s'appuie dessus pour réserver une clé ou dédupliquer un événement en une
seule instruction.

**Dans notre code :** `migrations/001_init.sql` et `002_notification.sql` :

| Contrainte | Rôle |
|---|---|
| `api_idempotency.idempotency_key` (PK) | Idempotence HTTP |
| `uq_registration_active_student` : index unique partiel sur `(session_id, lower(student_email))` limité aux statuts actifs | Un apprenant ne peut avoir qu'une inscription active par session, mais peut se réinscrire après `COMPENSATED` ou `FAILED` |
| `processed_events (consumer_name, event_id)` (PK) | Déduplication des consumers |
| `outbox_event.event_id` (PK) | Un événement n'existe qu'une fois dans l'outbox |
| `audit_log.event_id` (UNIQUE) | Un événement n'est journalisé qu'une fois |
| `notification.event_id` (PK) | Une notification par événement |
| `external_mapping (entity_type, internal_id, system_name)` (UNIQUE) | Un identifiant externe par entité |

S'y ajoutent les `CHECK (remaining_seats >= 0)` et `remaining_le_capacity` sur `training_session`.

**Preuve :** `scripts/test-http-codes.py` (« même apprenant… » : 409 `ALREADY_REGISTERED`), `scripts/test-concurrency.py` (`remainingSeats` jamais négatif) et `scripts/test-duplicate-message.py` (doublons ignorés grâce à `processed_events`).

## 7. Comment empêchez-vous deux réservations de la dernière place ?

En sérialisant les réservations d'une même session par un verrou de ligne PostgreSQL. Chaque POST, dans sa
transaction, verrouille la ligne `training_session` avec `SELECT … FOR UPDATE`, lit `remaining_seats`, et ne
décrémente que si la valeur est strictement positive. Les autres transactions sur la même session attendent ce
verrou. Quand elles l'obtiennent, elles lisent la valeur **commitée**, soit 0, et répondent `409 SESSION_FULL`.
La contrainte `CHECK (remaining_seats >= 0)` empêcherait de toute façon d'enregistrer une valeur négative.

**Dans notre code :** `integration-api/app/registrations.py`, `create_registration`, étape 2 (`SELECT id, remaining_seats FROM training_session WHERE id = %s FOR UPDATE`, puis `UPDATE … SET remaining_seats = remaining_seats - 1`). Contrainte dans `migrations/001_init.sql`. La libération de la place (`_finalize` dans `workers/registration/processor.py`) prend le même verrou.

**Preuve :** `scripts/test-concurrency.py` : 20 requêtes simultanées (testé aussi avec 50) sur une session de capacité 1. Résultat : exactement 1 réponse 202, les autres en 409 `SESSION_FULL`, aucune 5xx, `remainingSeats = 0`, 1 seule inscription active.

## 8. Que fait `SELECT ... FOR UPDATE` ?

Il lit les lignes sélectionnées **et** pose sur elles un verrou exclusif de ligne, conservé jusqu'à la fin de la
transaction (COMMIT ou ROLLBACK). Une autre transaction qui tente de les verrouiller (`FOR UPDATE`) ou de les
modifier attend. Les lectures simples ne sont pas bloquées, grâce au MVCC. En `READ COMMITTED`, la transaction
qui attendait relit la version commitée de la ligne quand le verrou est libéré. C'est ce qui lui fait voir
`remaining_seats = 0`. La variante `FOR UPDATE SKIP LOCKED` saute les lignes déjà verrouillées au lieu
d'attendre. Elle permet de répartir le travail entre plusieurs processus.

**Dans notre code :**
- `FOR UPDATE` sur la session : `integration-api/app/registrations.py` ;
- `FOR UPDATE` sur l'inscription dans `_confirm` et `_finalize` (états finaux), `_compensate` (passage à `COMPENSATING`) et `on_retry` (`RETRYING`), dans `workers/registration/processor.py`, pour ne jamais écraser un état final ;
- `FOR UPDATE SKIP LOCKED` dans l'outbox dispatcher : `workers/outbox/main.py`, `dispatch_batch`.

**Preuve :** `scripts/test-concurrency.py` pour `FOR UPDATE`. `scripts/test-outbox.py` pour `SKIP LOCKED` : plusieurs dispatchers, « exactement n messages … (ni doublon ni perte) » et « travail réparti entre plusieurs dispatchers ».

## 9. Pourquoi un consumer doit être idempotent ?

Parce que RabbitMQ garantit une livraison *at-least-once* : un même message peut arriver plusieurs fois. C'est
le cas si le worker plante avant l'ACK (redélivrance), si l'outbox republie après un crash entre le *confirm*
du broker et son COMMIT, ou si un message est rejoué manuellement depuis la DLQ. Sans idempotence, chaque
doublon créerait un deuxième compte Moodle, un deuxième Sales Order ou un deuxième email. Le traitement doit
donc pouvoir être appliqué N fois avec le même effet qu'une seule fois.

**Dans notre code :** trois niveaux.
1. **Déduplication par `eventId`** dans `processed_events` : `shared/consumer.py` (`already_processed` avant le traitement, `mark_processed` après). Le worker d'inscription l'écrit dans la même transaction que la confirmation (`_confirm`). Le notifier l'écrit dans la même transaction que la ligne `notification`.
2. **Verrou par inscription** : `pg_try_advisory_lock` dans `_registration_lock` (`workers/registration/processor.py`). Un doublon reçu pendant que l'original est en cours lève `DuplicateInFlight`, et il est acquitté sans effet.
3. **Étapes idempotentes** : chaque création externe passe par `_find_or_create` (mapping local, puis recherche sur un champ unique, puis création), et l'état final est testé avant toute action (`TERMINAL_STATUSES`).

**Preuve :** `scripts/test-duplicate-message.py` (événement republié 2 fois, puis événement neuf publié 3 fois et consommé par 2 workers : une seule exécution de la saga, 1 utilisateur Moodle, 1 Lead, 1 Sales Order) et `scripts/test-notifications.py` (événement `confirmed` republié 2 fois : 1 seul email).

## 10. Que se passe-t-il si le worker plante avant l'ACK ?

Le message n'est pas perdu. Il reste « unacked » côté RabbitMQ. Quand la connexion AMQP du worker se ferme
(processus mort), le broker le remet en queue et le redélivre au prochain consumer, ici le même worker
redémarré par Docker (`restart: unless-stopped`). Comme l'ACK n'est envoyé qu'en **fin** de traitement, tout
ce qui a été fait avant le crash est rejoué. Les étapes idempotentes (question 9) font que le rejeu complète le
travail sans le dupliquer. Si un message faisait planter le worker à chaque fois, le `x-delivery-limit` de 10
des queues quorum l'enverrait en DLQ (après la 11e livraison) au lieu de bloquer la queue indéfiniment.
Vérifié expérimentalement sur RabbitMQ 4.3.6 (version épinglée) : une redélivrance due à la fermeture de la
connexion sans ACK incrémente bien `x-delivery-count`.

**Dans notre code :** `shared/consumer.py` : `auto_ack=False`, `basic_qos(prefetch_count=1)`, ACK dans `_finish` uniquement après `_process`. `shared/rabbit.py` : `DELIVERY_LIMIT = 10`. Politique de redémarrage dans `docker-compose.yml`. Le verrou consultatif par inscription est un verrou **de session** PostgreSQL : il est libéré automatiquement quand la connexion du processus mort se ferme (`_registration_lock`).

**Preuve :** `scripts/test-crash-worker.py` (le worker est tué, `RestartCount` augmente, le message est redélivré, statut final `CONFIRMED`).

## 11. Que se passe-t-il s'il plante après Moodle mais avant l'ACK ?

C'est le cas le plus délicat : l'effet externe a eu lieu, mais rien ne le confirme côté Hub. Au rejeu :
- Lead et Customer sont relus depuis `external_mapping` ;
- l'utilisateur Moodle aussi, car son mapping est enregistré avant l'inscription au cours ;
- le mapping de l'inscription au cours manque, mais le worker demande à Moodle si l'utilisateur est **déjà inscrit** (`core_enrol_get_users_courses`). Il ne réinscrit pas, enregistre le mapping et continue vers le Sales Order.

Résultat : un seul compte Moodle, une seule inscription au cours, un seul Lead et un seul Sales Order.

**Dans notre code :** `workers/registration/processor.py`, `_run_saga`. Le bloc `if self._mapping(reg_id, MOODLE_ENROLMENT) is None:` contient `moodle.is_enrolled(...)` avant `moodle.enrol(...)`. Le hook d'injection `CRASH_AFTER_MOODLE` appelle `os._exit(1)` juste après l'inscription, avant le mapping et l'ACK. Il est désactivé par défaut (`docker-compose.yml`).

**Preuve :** `scripts/test-crash-worker.py` : « aucun deuxième utilisateur Moodle », « inscrit une seule fois au cours », « 1 seul Lead et 1 seul Sales Order », « deux tentatives visibles (crash puis reprise) ».

## 12. Pourquoi l'Outbox ?

Pour éviter le *dual write* : écrire en base **et** publier dans RabbitMQ sont deux opérations sur deux
systèmes, sans transaction commune. Si l'on publie après le COMMIT, un crash entre les deux fait perdre
l'événement : une place est réservée mais aucune inscription n'est traitée. Si l'on publie avant, un ROLLBACK
laisse partir l'événement d'une inscription qui n'existe pas. Avec l'outbox, l'événement est une **ligne de la
même transaction** que le changement d'état : les deux existent ensemble, ou aucun. Un processus séparé, le
dispatcher, publie ensuite ces lignes et les marque publiées.

**Dans notre code :** `shared/events.py`, `insert_outbox`, appelé avec le curseur de la transaction métier dans `integration-api/app/registrations.py` (`registration.accepted`) et dans `workers/registration/processor.py` (`_confirm`, `_finalize`). Le dispatcher est `workers/outbox/main.py` (`dispatch_batch`), et la table est `outbox_event` (`migrations/001_init.sql`, index partiel `ix_outbox_unpublished`).

**Preuve :** `scripts/test-outbox.py`, scénario A : dispatcher arrêté, le POST répond 202 et l'événement attend dans l'outbox (`published_at IS NULL`, `/ready` signale `pendingOutboxEvents`), puis il est publié au redémarrage.

## 13. PostgreSQL commit mais RabbitMQ indisponible ?

Pas de conséquence pour le client : l'API ne parle pas à RabbitMQ pour accepter une inscription, elle répond
`202` puisque le COMMIT a réussi. L'événement reste dans `outbox_event` avec `published_at IS NULL`. Le
dispatcher échoue à publier : l'erreur est comptée dans `publish_attempts`, avec `last_error`, sans que la
ligne soit marquée publiée. Il se reconnecte avec un backoff, puis publie tout ce qui est en attente, dans
l'ordre de `created_at`. `/ready` répond `503` (`rabbitmq: DOWN`) pendant la panne, ce qui la rend visible.

**Dans notre code :** `workers/outbox/main.py` : `dispatch_batch` commite d'abord `published_at` pour les événements déjà confirmés, puis relève l'`AMQPError`. `run` gère la reconnexion. Le backoff de reconnexion est dans `shared/rabbit.py` (`connect`). Le contrôle RabbitMQ de `/ready` est dans `integration-api/app/routes/health.py`.

**Preuve :** `scripts/test-outbox.py`, scénario B : RabbitMQ arrêté, POST toujours 202, `/ready` 503, événement conservé dans l'outbox. Au redémarrage, le dispatcher se reconnecte et l'audit-worker reçoit l'événement.

## 14. Que contient votre DLQ ?

Les messages que le système ne peut pas traiter seul, chacun avec sa cause. Il y en a trois sortes :

1. les messages **illisibles** : JSON invalide, enveloppe incomplète, `schemaVersion` inconnue. Ils sont rejetés par `basic.reject` et routés par le DLX de la queue, avec l'en-tête `x-death` posé par RabbitMQ ;
2. les **erreurs permanentes** ou les **retries épuisés** (4 tentatives) qui n'ont pas pu être compensés, par exemple un échec avant Moodle (l'inscription passe en `FAILED`) ou un événement d'inscription inconnue ;
3. les **compensations impossibles**, quand Moodle est indisponible au moment de la désinscription : statut `COMPENSATING`, en-tête `x-give-up-error`.

Le message conserve son body d'origine. Le consumer ajoute `x-error`, `x-error-kind` (`permanent` ou
`retries exhausted`), `x-attempts`, `x-consumer` et `x-failed-at`. Les DLQ sont des queues **classiques**
durables, sans limite de livraison : on peut les inspecter (peek + requeue) sans risquer de perdre un message.

Un cas compensé avec succès ne va **pas** en DLQ, car l'issue métier est gérée (`on_give_up` renvoie `"handled"`).

**Dans notre code :** `shared/consumer.py` (`_give_up`, `_finish`), `shared/rabbit.py` (`_DLQS` : `registration.dlq`, `notifications.dlq` et `audit.dlq`, liées à `si.events.dlx`) et `workers/registration/processor.py` (`on_give_up`). Inspection : `scripts/inspect-dlq.py [queue]`.

**Preuve :** `scripts/test-dlq.py` : A, JSON invalide en DLQ ; B, inscription inconnue en DLQ sans retry ; C, ERPNext arrêté durablement, 4 tentatives, `FAILED`, place libérée, message en DLQ avec la cause.

## 15. Quelles erreurs sont retryables ?

Celles qui peuvent disparaître d'elles-mêmes (`TransientError`) :
- timeout HTTP (5 s par défaut) et connexion refusée ;
- HTTP `429` et `5xx`, par exemple le 502 de nginx quand le backend ERPNext est arrêté ;
- réponse Moodle non JSON (page de maintenance) et codes d'erreur Moodle liés à sa base (`dmlreadexception`, `dmlwriteexception`, `dbconnectionfailed`, `dbsessionhandlerproblem`) ;
- secret pas encore généré par le bootstrap ;
- erreurs SMTP et réseau du notifier.

Toute exception inattendue, par exemple la base d'intégration indisponible, est aussi traitée comme temporaire.
Le retry suit un backoff de 2 s, 4 s puis 8 s, soit 4 tentatives au total (mesuré : 2,15 / 4,13 / 8,13 s).
Il passe par des queues à TTL : le worker n'est pas bloqué pendant l'attente.

Ne sont **pas** retryables (`PermanentError`) :
- HTTP `4xx` hors 429, par exemple le `417 LinkValidationError` d'ERPNext pour un article inexistant ;
- exception Moodle métier ;
- cours introuvable ;
- inscription inconnue ;
- destinataire SMTP refusé.

**Dans notre code :** `shared/errors.py` (classes), `shared/erpnext.py` (`_request`), `shared/moodle.py` (`call`, `_TRANSIENT_ERRORCODES`), `workers/notifications/main.py` (`handle`) et `shared/consumer.py` (`_process` : `MAX_ATTEMPTS = 4`, `RETRY_DELAYS = (2, 4, 8)` défini dans `shared/rabbit.py`).

**Preuve :** `scripts/test-failure-moodle.py` (timeout, `RETRYING`, reprise), `scripts/test-failure-erp.py` (A : temporaire, retry puis `CONFIRMED` ; B : 417, `attempts = 1`, aucun retry) et `scripts/test-notifications.py` (E : Mailpit arrêté, retry, email délivré).

## 16. Pourquoi ne pas retry un 400 indéfiniment ?

Un 4xx dit que **la requête elle-même** est refusée : données invalides, référence inexistante, règle métier.
La renvoyer à l'identique produira la même réponse, et le temps n'y change rien. Retenter indéfiniment
consommerait des ressources sur le système distant. Avec `prefetch_count=1`, cela **bloquerait aussi la
queue** : les inscriptions valides suivantes attendraient derrière un message empoisonné. Et le problème
serait masqué au lieu d'être signalé. Le bon traitement est d'abandonner tout de suite : compensation si
nécessaire, statut final explicite (`COMPENSATED` ou `FAILED`, place libérée, apprenant notifié), et
message conservé en DLQ comme **trace d'analyse**. Rejouer ce message n'aurait aucun effet : l'inscription
est déjà dans un état final et le worker l'ignore (`handle`, `TERMINAL_STATUSES`). Pour réessayer, le client
refait une demande avec une nouvelle `Idempotency-Key`. Seul un message de compensation bloquée (statut
`COMPENSATING`, Moodle indisponible pendant la désinscription) est utilement rejouable.

**Dans notre code :** `shared/consumer.py`, `except PermanentError` qui appelle directement `_give_up` sans retry. `workers/registration/processor.py`, `_run_saga` : un `PermanentError` sur le Sales Order déclenche immédiatement `_compensate`.

**Preuve :** `scripts/test-failure-erp.py`, B (417 : `attempts = 1`) et `scripts/test-compensation.py`, A (compensation immédiate).

## 17. Comment corrélez-vous les logs ?

Chaque requête HTTP reçoit un correlation ID : celui fourni dans `X-Correlation-ID` s'il est valide, sinon
`corr-<uuid>`. Il est renvoyé au client. Il est stocké dans un `contextvar` et ajouté automatiquement à
**chaque ligne de log JSON** par le formateur. Il est enregistré dans `registration.correlation_id`, copié dans
l'enveloppe de chaque événement et dans les propriétés AMQP. Les workers le restaurent à la réception du
message. Il est transmis en en-tête HTTP à Moodle et ERPNext, et en en-tête `X-Correlation-ID` dans les emails.
Tous les événements d'une inscription, y compris `confirmed` et `compensated`, portent le correlation ID de la
requête d'origine. Pour suivre une inscription de bout en bout :
`docker compose logs | grep corr-xxx`.

**Dans notre code :**
- génération et propagation HTTP : `integration-api/app/main.py` (`correlation_and_access_log`) ;
- injection dans les logs : `shared/logging_setup.py` (`correlation_id_var`, `JsonFormatter`) ;
- enveloppe : `shared/events.py` ;
- propriétés AMQP : `shared/rabbit.py` (`event_properties`) ;
- restauration côté worker : `shared/consumer.py` (`_process`) ;
- appels externes : `shared/erpnext.py` et `shared/moodle.py` (en-tête `X-Correlation-ID`) ;
- email : `workers/notifications/main.py`.

**Preuve :** `scripts/test-http-codes.py` (ID renvoyé tel quel, ou généré s'il est absent), `scripts/test-happy-path.py` (« correlationId conservé ») et `scripts/test-notifications.py`, D (audit complet de l'inscription avec le correlationId).

## 18. Comment gérez-vous les IDs Moodle et ERPNext ?

Notre identifiant de référence est le `registrationId` (UUID) généré par le Hub. Les identifiants des
logiciels sont enregistrés dans `external_mapping`, avec une ligne par `(entity_type, internal_id, system_name)`.
Les entités sont `erp_lead`, `erp_customer`, `erp_sales_order`, `moodle_user`, `moodle_course` et
`moodle_enrolment`. Chaque identifiant est enregistré dès qu'il est obtenu, ce qui rend la saga reprenable
étape par étape. Avant toute création, on cherche aussi l'objet dans le logiciel par une **clé naturelle** :

| Objet | Clé naturelle |
|---|---|
| Lead ERPNext | email |
| Customer ERPNext | nom de l'entreprise |
| Utilisateur Moodle | email, et username = email (unique côté Moodle) |
| Sales Order ERPNext | `po_no` = `registrationId` |

Cette recherche couvre le cas où le mapping n'a pas été enregistré à cause d'un crash. Les identifiants sont
exposés par `GET /api/v1/registrations/{id}` (`moodleUserId`, `courseId`, `erpLeadId`, `erpCustomerId`,
`erpSalesOrderId`) et dans l'événement `registration.confirmed`.

**Dans notre code :** table `external_mapping` (`migrations/001_init.sql`). Dans `workers/registration/processor.py` : `_find_or_create`, `_mapping` et `_save_mapping`. Dans `shared/erpnext.py` : `find_lead`, `find_customer` et `find_sales_order` (filtre `po_no`). Dans `shared/moodle.py` : `find_user_by_email` et `create_user` (username = email). Lecture dans `integration-api/app/registrations.py` (`get_registration`).

**Preuve :** `scripts/test-happy-path.py` (« Moodle : moodleUserId cohérent », Sales Order lié à l'inscription) et `scripts/test-crash-worker.py` (reprise sans doublon grâce aux mappings).

## 19. Pourquoi ne pas accéder directement aux DB des logiciels ?

Le schéma interne de Moodle ou d'ERPNext n'est pas un contrat : il change entre versions sans préavis. Écrire
directement en base contournerait toute la logique applicative : validations, droits, hooks, cache Moodle,
événements, workflow `docstatus` et écritures comptables d'ERPNext, numérotation des documents. On pourrait
produire des données incohérentes que l'interface n'affiche pas, ou qui cassent une mise à jour. Cela
demanderait aussi de distribuer des identifiants de base avec des droits complets. On passe donc uniquement
par les API officielles : REST `/api/resource/{DocType}` pour ERPNext, et un service Web Services Moodle dédié
qui liste les fonctions appelées par le Hub et par le bootstrap. Limite assumée de la démo : le token Moodle
est celui de l'administrateur (`restrictedusers = 0`) et les clés ERPNext celles d'`Administrator`. En
production : utilisateurs techniques dédiés à droits minimaux, `restrictedusers = 1`, et
`core_course_create_courses` réservé au token de bootstrap.

**Dans notre code :** `shared/erpnext.py` (seules les routes `/api/resource/...` sont utilisées) et `shared/moodle.py` (`/webservice/rest/server.php`, liste des fonctions en tête de fichier). Les bootstraps (`infra/bootstrap/bootstrap_erpnext.py`, `bootstrap_moodle.py`) passent eux aussi par les API. Seule la configuration initiale du service Web Services Moodle (`infra/moodle/setup_ws.php`) s'exécute dans le conteneur Moodle, via l'API PHP interne de Moodle. Le Hub n'a aucune connexion vers `moodle-db` ni vers `erpnext-db`.

**Preuve :** pas de test dédié, c'est un choix d'architecture. On peut le vérifier : aucune variable de connexion à `moodle-db` ou `erpnext-db` dans les services du Hub (`docker-compose.yml`). Les tests eux-mêmes vérifient Moodle et ERPNext par leurs API (`scripts/_testlib.py`, classe `Externals`).

## 20. Quelle compensation ?

Si l'apprenant est déjà inscrit dans Moodle et que l'étape ERPNext (Sales Order) échoue définitivement ou après
épuisement des retries, la saga ne peut pas aboutir. Le worker applique alors trois actions :

1. il passe l'inscription en `COMPENSATING` ;
2. il **désinscrit l'apprenant du cours Moodle** (`enrol_manual_unenrol_users`, sans effet si l'apprenant n'est plus inscrit) ;
3. dans une transaction, il **libère la place**, passe en `COMPENSATED` et écrit `registration.compensated` dans l'outbox. Le notifier envoie alors un email d'annulation.

Ce n'est pas un rollback : on ne supprime pas le compte Moodle, ni le Lead ou le Customer ERPNext. Ces données
restent valides et réutilisables pour une inscription future. Seule l'action devenue incohérente, l'accès au
cours, est annulée. Si Moodle est lui-même indisponible pendant la compensation, celle-ci est retentée. Si
elle échoue encore, l'inscription reste en `COMPENSATING` et le message part en DLQ pour intervention manuelle.
C'est une limite assumée. Quand l'échec survient **avant** Moodle, il n'y a rien à compenser : statut `FAILED`,
place libérée, désinscription « best effort » par précaution.

**Dans notre code :** `workers/registration/processor.py` : `_compensate`, `_finalize` et `on_give_up` (choix entre compensation et `FAILED`). La reprise d'une compensation interrompue est dans `handle` (statut `COMPENSATING`). L'échec avant Moodle est géré par `_fail`. L'ordre des étapes est **ERPNext CRM, puis Moodle, puis ERPNext Sales Order**, d'où une seule action à compenser.

**Preuve :** `scripts/test-compensation.py` (A : 417 permanent, compensation immédiate ; B : ERPNext arrêté après Moodle, retries épuisés, compensation ; dans les deux cas, désinscription Moodle vérifiée, place libérée, aucun Sales Order) et `scripts/test-notifications.py`, C (email « Inscription annulée »).

## 21. At-least-once ?

Oui, c'est la garantie de livraison du système. Aucun événement n'est perdu, mais un événement peut être livré
plusieurs fois. Elle repose sur une chaîne de mécanismes, chacun préférant le doublon à la perte :

- l'outbox est commitée avec la donnée ;
- le dispatcher marque `published_at` **après** le *publisher confirm* du broker ;
- les messages sont persistants, dans des queues quorum durables ;
- les consumers font un ACK manuel **après** traitement ;
- les republications en retry ou en DLQ sont confirmées par le broker **avant** l'ACK du message d'origine.

Chaque maillon peut produire un doublon en cas de crash. Les doublons sont absorbés par l'idempotence des
consumers (question 9).

**Dans notre code :** `workers/outbox/main.py` (docstring « Pannes » et `dispatch_batch`), `shared/rabbit.py` (`publish_event` : `delivery_mode` persistant, `mandatory=True`), `shared/consumer.py` (`confirm_delivery()`, `auto_ack=False`, ordre publication puis ACK dans `_finish`).

**Preuve :** `scripts/test-outbox.py` (ni perte ni doublon sur n messages), `scripts/test-crash-worker.py` (redélivrance) et `scripts/test-duplicate-message.py` (doublons absorbés).

## 22. Pourquoi exactly once est difficile ?

Parce qu'il faudrait qu'un effet et son accusé de réception soient atomiques, alors qu'ils vivent dans des
systèmes différents sans transaction commune : PostgreSQL, RabbitMQ, Moodle, ERPNext, SMTP. Quel que soit
l'ordre choisi, un crash entre les deux étapes force à choisir entre perdre (ACK d'abord) et dupliquer (effet
d'abord). Un timeout ne dit pas si l'opération distante a eu lieu. Le message de confirmation peut lui-même se
perdre : c'est le problème des deux généraux. On vise donc un effet **« exactly-once »** obtenu par
**at-least-once + idempotence**. Il reste des fenêtres résiduelles, documentées :

- un `POST` ERPNext qui expire mais que le serveur termine **après** notre nouvelle recherche peut créer un doublon tardif ;
- un crash entre l'envoi SMTP et le COMMIT renvoie l'email. Le `Message-ID` est dérivé de l'`eventId` pour que les clients mail puissent repérer le doublon.

**Dans notre code :** `workers/registration/processor.py` (`_find_or_create`, recherche avant création ; Sales Order retrouvé par `po_no`) et `workers/notifications/main.py` (docstring et `Message-ID`).

**Preuve :** `scripts/test-duplicate-message.py` et `scripts/test-notifications.py`, B (doublons de messages sans effet supplémentaire). Les fenêtres résiduelles ne sont pas reproductibles de façon déterministe.

## 23. Différence idempotence HTTP vs consumer ?

L'**idempotence HTTP** protège contre un **client** qui renvoie sa requête : timeout réseau, double clic,
retry automatique. La clé est **choisie par le client** (`Idempotency-Key`). Le serveur mémorise la
**réponse** et la rejoue, et détecte une clé réutilisée pour une autre demande (409). L'**idempotence
consumer** protège contre le **broker** qui relivre un message (at-least-once, crash, republication). La clé
est **générée par le producteur** (`eventId`). Il n'y a pas de réponse à rejouer : le consumer doit garantir
que l'**effet** n'est produit qu'une fois, y compris dans les systèmes externes. Cela exige plus que la
déduplication : il faut aussi des étapes rejouables (mappings, recherche avant création), car un crash peut
survenir au milieu du traitement.

**Dans notre code :** HTTP dans `integration-api/app/registrations.py` et la table `api_idempotency`. Consumer dans `shared/consumer.py` et la table `processed_events`, plus `workers/registration/processor.py` (`_registration_lock`, `_find_or_create`).

**Preuve :** `scripts/test-idempotency.py` (HTTP), `scripts/test-duplicate-message.py` et `scripts/test-crash-worker.py` (consumer).

## 24. Où se situe votre transaction locale ?

Dans la base d'intégration PostgreSQL, à chaque changement d'état métier. Il y en a quatre principales :

1. **Acceptation** (API) : réservation de la clé, verrou et décrément de la place, insertion de l'inscription et de son historique, événement `registration.accepted` dans l'outbox, mémorisation de la réponse. Tout ou rien.
2. **Confirmation** (worker) : passage à `CONFIRMED`, historique, outbox `registration.confirmed`, insertion dans `processed_events`.
3. **Échec ou compensation** (worker) : libération de la place, passage à `COMPENSATED` ou `FAILED`, outbox `registration.compensated` ou `registration.failed`.
4. **Notification** : ligne `notification` et `processed_events`. Pour l'**audit** : `audit_log` et `processed_events`.

Les appels à Moodle, ERPNext, RabbitMQ et SMTP sont **hors** de ces transactions. Le pool est en autocommit,
et chaque unité de travail ouvre explicitement `conn.transaction()`.

**Dans notre code :** `integration-api/app/registrations.py` (`create_registration`, un seul `with … conn.transaction()`), `workers/registration/processor.py` (`_confirm`, `_finalize`), `workers/notifications/main.py` (`handle`), `workers/audit/main.py` (`handle`) et `shared/db.py` (pool en autocommit).

**Preuve :** `scripts/test-concurrency.py` et `scripts/test-idempotency.py` (cohérence place, inscription et clé), `scripts/test-outbox.py` (événement présent dès le 202) et `scripts/test-db-down.sh` (base arrêtée : 503, rien n'est commité ; la même clé fonctionne après la reprise).

## 25. Quelles opérations ne peuvent pas appartenir à la même transaction ACID ?

Toutes celles qui touchent des systèmes différents :

- le COMMIT PostgreSQL et la publication RabbitMQ (résolu par l'outbox) ;
- l'ACK RabbitMQ et l'écriture en base (résolu par la déduplication et l'ACK après traitement) ;
- l'inscription Moodle et le Sales Order ERPNext (résolu par la saga, avec compensation) ;
- l'envoi SMTP et la trace `notification` (résolu par la déduplication et le `Message-ID`) ;
- la lecture agrégée du catalogue sur PostgreSQL, Moodle et ERPNext, qui n'est pas un instantané cohérent.

Moodle et ERPNext n'exposent que des appels HTTP unitaires, auto-commités de leur côté. Ils ne participent à
aucun protocole de transaction distribuée. On remplace donc l'atomicité globale par la **cohérence à terme** :
des transactions locales, des événements fiables, des étapes idempotentes et des actions compensatoires.

**Dans notre code :** ces frontières sont visibles dans `workers/registration/processor.py`, où chaque appel externe est suivi d'une écriture locale séparée (`_save_mapping`, `_set_status`), ainsi que dans `workers/outbox/main.py` et `workers/notifications/main.py`.

**Preuve :** `scripts/test-compensation.py` (atomicité perdue entre Moodle et ERPNext, rattrapée par la compensation) et `scripts/test-outbox.py` (entre PostgreSQL et RabbitMQ).

---

## Questions pièges probables

**Pourquoi un verrou consultatif (advisory lock) pour le Customer ERPNext ?**
ERPNext n'impose aucune unicité sur `customer_name` : deux créations simultanées de « ACME » produisent « ACME » et « ACME - 1 ». Une contrainte UNIQUE n'est pas possible, puisqu'on ne touche pas à la base ERPNext. Le « chercher puis créer » est donc sérialisé côté Hub par `pg_advisory_xact_lock(hashtextextended('erp_customer:acme', 0))`, partagé par tous les workers. Il est libéré au COMMIT, ou à la fermeture de la connexion si le worker plante. Le même verrou protège la création des Leads et des utilisateurs Moodle. Code : `_find_or_create` dans `workers/registration/processor.py`. Preuve : `scripts/test-concurrent-workers.py` (6 apprenants d'une nouvelle entreprise, 3 workers : 1 seul Customer).

**Pourquoi pas de two-phase commit (2PC / XA) ?**
Moodle et ERPNext ne savent pas faire de *prepare* ni de *commit* différé : leurs API REST valident chaque appel immédiatement. Le 2PC est de toute façon bloquant : un participant qui tombe après le *prepare* garde ses verrous jusqu'au retour du coordinateur. Il couple aussi la disponibilité de tous les systèmes, alors que l'intérêt de l'architecture est d'accepter une inscription même si Moodle est arrêté. La saga avec compensation est le choix standard pour des systèmes autonomes.

**Pourquoi le rejeu renvoie 202 et pas 200 ?**
L'idempotence HTTP impose qu'une requête répétée produise **la même réponse** que la première : même code, même body. Le client n'a pas à distinguer « ma première requête est passée » de « c'est un rejeu », et il gère un seul cas. L'information de rejeu reste disponible dans l'en-tête `Idempotent-Replayed: true`. Le body rejoué garde `"status": "ACCEPTED"` : c'est la réponse à la demande, pas l'état courant, qui s'obtient par `GET` sur `Location`.

**Que se passe-t-il si le dispatcher plante après le confirm du broker mais avant son COMMIT ?**
Le message est dans RabbitMQ, mais `published_at` est encore `NULL` : la transaction n'est pas commitée, et le verrou `FOR UPDATE SKIP LOCKED` est libéré. Au redémarrage, l'événement est republié avec **le même `eventId`**. Les consumers voient deux messages identiques. Le second est écarté par `processed_events`, ou par `DuplicateInFlight` s'il arrive pendant le traitement du premier. C'est l'at-least-once assumé (docstring de `workers/outbox/main.py`). Preuve : `scripts/test-duplicate-message.py` (« la publication par l'outbox est aussi dédupliquée »).

**Pourquoi `SESSION_FULL` est mémorisé mais pas `SESSION_NOT_FOUND` ?**
`SESSION_FULL` est une réponse métier valable à l'instant T. Si on ne la mémorisait pas, un rejeu après la libération d'une place pourrait répondre 202 : la même clé aurait alors deux réponses différentes, ce qui viole l'idempotence. Le 404 traduit plutôt une erreur de référence : la session peut être créée juste après, par exemple par un administrateur. Le code lève une exception, ce qui provoque un ROLLBACK, et la clé reste utilisable.

**Pourquoi les retries passent par des queues à TTL plutôt qu'un `sleep` dans le worker ou un `nack` avec remise en queue ?**
Un `sleep` bloquerait le worker, puisque `prefetch=1`, et toutes les autres inscriptions. Un `nack` avec remise en queue relivrerait immédiatement, sans backoff, et en boucle. Les queues `<queue>.retry.Ns` gardent le message N secondes puis le renvoient dans la queue d'origine par l'exchange par défaut. Pendant ce temps, le worker traite les autres messages. Le retour se fait directement dans la queue du consumer concerné : un retry du notifier n'est donc pas redistribué à l'audit (`shared/rabbit.py`).

**Pourquoi le Lead et le Customer sont-ils créés avant Moodle, et le Sales Order après ?**
Lead et Customer sont des données CRM sans conséquence pour l'apprenant : les créer d'abord ne nécessite aucune compensation si la suite échoue. Le Sales Order est la trace administrative qui « engage » l'inscription. Il vient en dernier pour qu'un seul effet externe, l'inscription Moodle, soit à compenser. Un échec avant Moodle donne `FAILED` sans compensation.

**ERPNext met environ 22 s à redémarrer, alors que vos retries couvrent 14 s : n'est-ce pas un problème ?**
C'est un compromis mesuré et assumé. Après 4 tentatives (0, +2, +4 et +8 s), l'inscription n'est pas perdue : si Moodle est déjà fait, elle est compensée et la place est libérée ; sinon, elle passe en `FAILED`, la place est libérée, l'apprenant est notifié, et le message reste en DLQ comme trace d'analyse (le rejouer serait sans effet : le client refait une demande avec une nouvelle clé). Allonger la série (par exemple 30 s ou 60 s) ne demande que de modifier `RETRY_DELAYS` dans `shared/rabbit.py`. Les queues de retry sont déclarées à partir de cette liste.
