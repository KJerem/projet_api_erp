# Historique du projet — Mini SI OpenTraining

Journal détaillé de la construction du projet, étape par étape : décisions prises, problèmes
rencontrés sur les vrais logiciels, corrections et résultats de tests. Le projet a été mené le
**3 octobre 2026**, en 7 phases (0 à 6), chacune validée avant de passer à la suivante et livrée par un
commit sur la branche `dev`.

| Phase | Contenu | Commit |
|---|---|---|
| — | Initialisation du dépôt | `e04b847` |
| 0 | Infrastructure Docker et bootstrap des vrais logiciels | `3a56319` |
| 1 | Integration API : idempotence, concurrence, outbox transactionnelle | `f9ff62b` |
| 2 | Outbox dispatcher et topologie RabbitMQ | `f4435a2` |
| 3 | Registration worker : saga Moodle + ERPNext, idempotence des consumers | `a79f174` |
| 4 | Résilience : retry, DLQ, compensation, pannes | `e05dc44` |
| 5 | Notification worker, audit worker, catalogue agrégé | `a650847` |
| 6 | Documentation, soutenance, tests unitaires, durcissement | `318ab84` |

---

## Cadrage

**Point de départ** : un dépôt contenant uniquement un `README.md` d'une ligne, sur la branche `dev`.
Environnement constaté : Windows 11, Docker 28.3, Docker Compose 2.39, 16 Go de RAM.

**Décisions prises avant de coder**

| Question | Décision |
|---|---|
| Intégration d'ERPNext | Compose unique : le `pwd.yml` officiel de `frappe/frappe_docker`, image `frappe/erpnext:v16.37.0`, intégré au `docker-compose.yml` racine. Un seul `docker compose up -d` démarre tout. |
| Image Moodle | Image construite par nos soins sur `moodlehq/moodle-php-apache` (image officielle maintenue par Moodle HQ), installation automatique en ligne de commande. L'image Bitnami a été écartée car elle n'est plus maintenue. |
| Bonus (Keycloak, Prometheus…) | Aucun : priorité au socle obligatoire. |
| Rythme de travail | Validation par phase : démo testée à la fin de chaque phase, puis feu vert avant de continuer. |

**Faits vérifiés avant d'écrire le plan**

- Le tag Docker `frappe/erpnext:v16.37.0` existe (publié le 30/09/2026) et le `pwd.yml` officiel l'utilise.
- La dernière version stable de Moodle est la **5.3.0** (build 20261005). Elle exige PHP ≥ 8.3 et
  PostgreSQL ≥ 17, et son dossier web est `public/`.
- L'API Frappe s'authentifie avec l'en-tête `Authorization: token api_key:api_secret` (documentation
  Frappe consultée via Context7).

**Préférence de travail ajoutée en cours de route** : tout script Python lancé sur le poste passe par le
venv du projet (`.venv`), jamais par le Python global.

---

## Phase 0 — Infrastructure et bootstrap (`3a56319`)

**Objectif** : démarrer les vrais logiciels de façon reproductible et vérifier qu'ils répondent par leurs
API officielles.

### Ce qui a été construit

- **`docker-compose.yml` unique** : services ERPNext repris du `pwd.yml` officiel (préfixés `erpnext-`,
  mots de passe sortis dans `.env`), Moodle et sa base `moodle-db` (PostgreSQL 17), base d'intégration
  `integration-db`, RabbitMQ (avec console de management) et Mailpit.
- **Image Moodle** (`infra/moodle/`) :
  - `Dockerfile` : clone de Moodle `v5.3.0`, puis `composer install --no-dev` ;
  - `config.php` : configuration lue dans les variables d'environnement ;
  - `50-opentraining-setup.sh` : installation en ligne de commande au premier démarrage ;
  - `setup_ws.php` : active le protocole REST, crée le service Web Services et génère le token.
- **Bootstraps par les API officielles** (`infra/bootstrap/`) :
  - ERPNext : fin du setup wizard (société OpenTraining, EUR, France), génération des clés API, création
    des articles de formation et de leurs prix ;
  - Moodle : création des cours par Web Services.
- **`infra/catalog.json`** : source unique des sessions, cours, articles et prix.
- **Secrets** : les tokens sont générés au démarrage dans un volume Docker (`integration-secrets`),
  jamais dans Git. `scripts/init-env.py` génère un `.env` avec des mots de passe aléatoires.
- **`scripts/check-infra.sh`** : test de l'infrastructure par les API officielles.

### Recherches dans le code des logiciels

- Le setup wizard d'ERPNext (`setup_complete`) et la génération de clés (`generate_keys`) sont exposés
  dans l'API (`@frappe.whitelist`). Le bootstrap ERPNext passe donc entièrement par l'API REST.
- L'image moodlehq détecte automatiquement le dossier `public/` de Moodle 5.x.
- Moodle 5.3 a des dépendances Composer obligatoires en production : l'image doit lancer
  `composer install`.

### Problèmes rencontrés et corrections

| Problème | Cause | Correction |
|---|---|---|
| Échec du build Moodle (`Unable to read current working directory`) | Le dossier de travail de l'image (`/var/www/html`) était supprimé avant le clone. | `cd /` avant la suppression. |
| 401 « Encryption key is invalid » au premier bootstrap ERPNext | Frappe génère sa clé de chiffrement paresseusement, au premier secret enregistré. Une concurrence entre ses workers rendait la clé API illisible. | La clé est fixée dès la création du site (`erpnext-create-site`). Le bootstrap vérifie aussi les clés juste après leur génération. |
| `erpnext-create-site` en erreur après ce correctif | Des commentaires `#` dans un bloc YAML replié (`>`) commentaient le reste de la ligne bash. | Commentaire déplacé hors de la commande. |
| Faux échecs du test d'infra | Git Bash réécrivait `/secrets/...` en chemin Windows ; espace non encodé dans l'URL `Item Price`. | `MSYS_NO_PATHCONV=1` et `%20`. |

### Résultat

Depuis une stack vierge (`docker compose down -v` puis `up -d --build`), tout démarre et se configure du
premier coup en **4 min 15**, sans relance des bootstraps. Test d'infrastructure : **8/8**. Consommation
au repos : environ 1,1 Go de RAM.

**Écart validé** : la topologie RabbitMQ sera déclarée en code Python plutôt que dans un fichier
`definitions.json` (conflit avec l'utilisateur défini dans `.env`, et un hash de mot de passe aurait dû
être versionné).

---

## Phase 1 — Integration API (`f9ff62b`)

**Objectif** : l'API d'inscription, avec idempotence, gestion de la dernière place et écriture de
l'événement dans l'outbox.

### Ce qui a été construit

- **API FastAPI** (`integration-api/`) : `POST` et `GET /api/v1/registrations`, `POST` et
  `GET /api/v1/sessions`, `/health`, `/ready`, documentation Swagger sur `/docs`.
- **Une seule transaction PostgreSQL par inscription** (`integration-api/app/registrations.py`) :
  1. enregistre la clé d'idempotence (une seconde requête avec la même clé attend que la première se termine) ;
  2. compare l'empreinte SHA-256 du body normalisé ;
  3. verrouille la session (`SELECT … FOR UPDATE`) ;
  4. retire une place ;
  5. crée l'inscription et l'événement `registration.accepted` dans l'outbox ;
  6. mémorise la réponse pour les rejeux.
- **Garde-fous en base** (`migrations/001_init.sql`) : `CHECK (remaining_seats >= 0)` et un index unique
  qui empêche un apprenant d'avoir deux inscriptions actives sur la même session.
- **Logs JSON** avec `correlationId`, `registrationId` et `eventId`. L'en-tête `X-Correlation-ID` est
  repris de la requête, ou généré s'il est absent.
- **Migrations versionnées**, appliquées par un service `migrate` qui crée aussi les sessions du catalogue.
- **Image unique pour le Hub** (`Dockerfile` racine), exécutée par un utilisateur non-root.

### Problèmes rencontrés et corrections

| Problème | Correction |
|---|---|
| Deux services construisaient la même image en parallèle (« already exists ») | L'image n'est construite que par le service `migrate`, les autres la réutilisent. |
| Après un redémarrage de PostgreSQL, le pool aurait prêté des connexions mortes | Vérification de chaque connexion avant de la prêter (`check_connection`). |

### Résultats

| Script | Résultat |
|---|---|
| `test-idempotency.py` (tests 2 et 3) | 12/12 : même clé et même body → même `registrationId`, une seule place ; body différent → 409 ; 10 envois simultanés de la même clé → un seul `registrationId` |
| `test-concurrency.py` (test 4) | 20 requêtes sur 1 place → 1 acceptée, 19 refusées, 0 place restante ; puis 5 fois de suite avec 50 requêtes : toujours exactement 1 acceptée |
| `test-http-codes.py` | 16/16 (codes 200, 201, 202, 400, 404, 409, 422 et correlation ID) |
| `test-db-down.sh` | 5/5 : base arrêtée → 503, puis reprise avec la même clé → 202 |

Contrôle en base : une inscription = exactement un événement outbox, aucun surbooking.

**Choix validés** : un rejeu renvoie **le statut d'origine (202) avec l'en-tête `Idempotent-Replayed: true`**
(plutôt que 200, comme recommandé par le brouillon IETF) ; les refus métier (`SESSION_FULL`,
`ALREADY_REGISTERED`) sont mémorisés, pas le 404.

---

## Phase 2 — Outbox dispatcher et RabbitMQ (`f4435a2`)

**Objectif** : publier de façon garantie les événements de l'outbox dans RabbitMQ.

### Ce qui a été construit

- **Topologie déclarée en code** (`shared/rabbit.py`) : exchange `si.events`, exchange des messages
  rejetés `si.events.dlx`, exchange des retries `si.events.retry`, queues métier `registration.orchestrator`,
  `notifications` et `audit` (en quorum queues), une DLQ par queue, et des queues de retry à délai
  (2 s, 4 s, 8 s) qui renvoient directement le message dans sa queue d'origine.
- **Outbox dispatcher** (`workers/outbox/main.py`) : lecture avec `FOR UPDATE SKIP LOCKED` (plusieurs
  dispatchers possibles), messages persistants, attente de la confirmation du broker, marquage comme
  publié seulement après cette confirmation, reconnexion automatique.
- **`scripts/reset-integration.sh`** : remet le Hub à zéro avant une démo, sans toucher à Moodle ni à ERPNext.

### Problème rencontré et correction

En relisant le code, une erreur de conception est apparue : relancer l'erreur RabbitMQ *à l'intérieur*
de la transaction aurait annulé le marquage des événements déjà confirmés, qui auraient été republiés
pour rien. Le code a été restructuré : l'erreur est mémorisée, la transaction est validée, puis l'erreur
est relancée. Les pannes de base et de RabbitMQ sont aussi traitées séparément.

### Résultat — test 10 (`test-outbox.py`) : 16/16

| Scénario | Résultat |
|---|---|
| Dispatcher arrêté | L'événement reste dans l'outbox, puis il est publié au redémarrage. |
| RabbitMQ arrêté | L'API accepte toujours (202) et `/ready` passe en 503 ; l'événement est publié dès le retour de RabbitMQ. |
| 3 dispatchers, 100 inscriptions | Exactement 100 messages, sans doublon ni perte ; charge répartie (50 / 33 / 19). |

Avant la Phase 3, les 115 messages de test accumulés ont été purgés avec `reset-integration.sh`, pour
ne pas créer 115 utilisateurs de test dans Moodle et ERPNext.

---

## Phase 3 — Registration worker (`a79f174`)

**Objectif** : orchestrer l'inscription dans les vrais Moodle et ERPNext, sans doublon même en cas de
redélivrance, de crash ou de workers concurrents.

### Essais préalables sur les vraies instances

Avant d'écrire les clients, chaque appel a été testé à la main :

- **ERPNext** : groupe client `Commercial`, territoire `France`, liste de prix `Standard Selling`. Le prix
  est bien récupéré côté serveur (850 €), la soumission par `PUT docstatus=1` fonctionne, la recherche par
  `po_no` aussi, et un article inexistant renvoie une **erreur 417** (erreur permanente).
- **Découverte importante** : ERPNext **accepte deux Customer de même nom**. Il renomme le second
  (`ACME - 1`) au lieu de renvoyer une erreur. Il n'y a donc aucune protection contre les doublons de
  son côté.
- **Moodle** : un nom d'utilisateur en double renvoie `invalidparameter`, l'inscription à un cours est
  idempotente, et **`enrol_manual_unenrol_users` refuse le paramètre `roleid` en 5.3** (seuls `userid`
  et `courseid` sont acceptés). Moodle renvoie ses erreurs avec un code HTTP 200 et un objet `exception`.

### Ce qui a été construit

- **Clients des API officielles** : `shared/erpnext.py` (Lead, Customer, Sales Order) et
  `shared/moodle.py` (Web Services). Chaque appel a un timeout. Erreurs **temporaires** : timeout,
  connexion refusée, 5xx, 429. Erreurs **permanentes** : 4xx et erreurs métier Moodle.
- **Consumer générique** (`shared/consumer.py`) : acquittement seulement en fin de traitement,
  déduplication par `processed_events`, retry et DLQ. Le traitement tourne dans un thread séparé pour que
  la connexion RabbitMQ reste vivante pendant les appels longs.
- **Orchestration de l'inscription** (`workers/registration/processor.py`), dans l'ordre :
  1. Lead puis Customer dans ERPNext ;
  2. utilisateur Moodle (nom d'utilisateur = email, champ unique), puis inscription au cours ;
  3. Sales Order (`po_no` = identifiant d'inscription), puis soumission ;
  4. statut `CONFIRMED` et événement `registration.confirmed`, dans la même transaction.
- **Idempotence de chaque étape** : on consulte d'abord la table `external_mapping`, puis on cherche
  dans le système externe sur un champ unique avant toute création.
- **Contre les doublons ERPNext** : la recherche et la création sont protégées par un verrou PostgreSQL
  partagé par tous les workers (`pg_advisory_xact_lock`). Un second verrou par inscription empêche deux
  workers de traiter la même en parallèle.
- **Mode `CRASH_AFTER_MOODLE=true`** : le worker s'arrête brutalement juste après l'inscription Moodle.
- **Secrets** : rendus lisibles uniquement par l'utilisateur non-root du Hub (mode 0440, uid 10001).

### Résultats

| Script | Résultat |
|---|---|
| `test-happy-path.py` (tests 1, 2, 12) | 17/17 : 1 utilisateur Moodle inscrit, 1 Lead, 1 Customer, 1 Sales Order soumis à 850 € ; un rejeu ne crée rien de plus ; transitions visibles par `GET` |
| `test-duplicate-message.py` (test 5) | 9/9 : événement republié → aucun effet ; événement livré 4 fois à 2 workers → un seul traitement |
| `test-crash-worker.py` (test 9) | 8/8 : crash après Moodle, redémarrage par Docker, fin en `CONFIRMED` sans doublon |
| `test-concurrent-workers.py` | 4/4 : 6 apprenants d'une même entreprise, 3 workers → 1 seul Customer |
| Test de charge | 117 inscriptions, 117 `CONFIRMED`, aucune en DLQ |

Correction de test pendant la régression : `test-outbox.py` comptait des messages désormais consommés
par le worker ; le worker est mis en pause pendant la mesure.

---

## Phase 4 — Résilience (`e05dc44`)

**Objectif** : tests 6, 7, 8 et 11 (pannes Moodle et ERPNext, DLQ, compensation).

### Mesures préalables

- ERPNext met **22 s** à redémarrer, Moodle **10 s**. Or la fenêtre de retry (2 + 4 + 8 s) dure 14 s.
- Pour des tests reproductibles : les **pannes récupérables** sont simulées par `docker pause` (un vrai
  timeout réseau), les **pannes durables** par `docker stop` (une vraie erreur 502).

### Ce qui a été construit ou modifié

- **DLQ en queues classiques** : en quorum queue, un message mort inspecté trop souvent aurait pu être
  supprimé silencieusement.
- `reset-integration.sh` supprime désormais les queues (au lieu de les vider) pour appliquer les
  changements de configuration.
- **`scripts/inspect-dlq.py`** : affiche le contenu de la DLQ sans le consommer (motif, tentatives, erreur).
- **Hook `ERP_STEP_DELAY_SECONDS`** (désactivé par défaut) : pause entre Moodle et le Sales Order, le temps
  de couper ERPNext pendant le test.
- Messages d'erreur ERPNext lisibles (texte au lieu de la page HTML 502 de nginx).

### Résultats

| Script | Test | Résultat |
|---|---|---|
| `test-failure-moodle.py` / `.sh` | 6 | 7/7 : Moodle gelé → `RETRYING` visible → `CONFIRMED` après le retour, sans doublon |
| `test-failure-erp.py` / `.sh` | 7 | 6/6 : timeout → retries puis `CONFIRMED` ; erreur 417 → **aucun retry**, compensation immédiate |
| `test-dlq.py` | 8 | 10/10 : JSON invalide → DLQ ; inscription inconnue → DLQ sans retry ; ERPNext arrêté → 4 tentatives, `FAILED`, place libérée, DLQ |
| `test-compensation.py` | 11 | 14/14 : erreur ERP permanente, puis temporaire jusqu'à épuisement → désinscription Moodle, place libérée, `COMPENSATED`, aucun Sales Order |

**Backoff mesuré** dans l'historique : 2,15 s, puis 4,13 s, puis 8,13 s.

Régression complète des Phases 1 à 4 : 13 scripts, **130 vérifications, 0 échec**.

---

## Phase 5 — Notifications, audit et catalogue (`a650847`)

**Objectif** : email à l'apprenant, journal d'audit et catalogue agrégé.

### Ce qui a été construit

- **Retries pour tous les workers** : chaque queue a désormais sa propre série de retry
  (`<queue>.retry.2s/4s/8s`), et `registration.failed` est aussi envoyé aux notifications.
- **Notification worker** : email par SMTP à Mailpit pour une inscription confirmée, annulée ou en échec,
  tracé dans la table `notification` (migration 002), avec le correlation ID.
- **Audit worker** : tous les événements du SI sont enregistrés dans `audit_log` (un événement n'y est
  jamais écrit deux fois).
- **`GET /api/v1/catalog`** : interroge en parallèle PostgreSQL (places), Moodle (nom du cours, un seul
  appel) et ERPNext (prix, un seul appel). Réponse en 138 ms. Si une source est indisponible : **503**
  par défaut, ou 200 dégradé avec `?partial=true` et l'en-tête `X-Catalog-Degraded`.
- L'API de recherche de Mailpit a été vérifiée dans sa documentation (Context7) avant d'écrire les tests.

### Résultats

| Script | Résultat |
|---|---|
| `test-happy-path.py` (test 1 complet) | 19/19, email compris |
| `test-notifications.py` | 15/15 : confirmation, doublon → 1 seul email, annulation, audit, Mailpit arrêté → email délivré à son retour |
| `test-catalog.py` | 9/9 : cours, prix et places corrects ; Moodle gelé → 503 en 5,0 s ; mode partiel → 200 dégradé |

### Corrections de tests pendant la régression

Aucun bug dans le code applicatif ; quatre hypothèses de tests ne tenaient plus :

- `test-outbox` lisait la queue `audit`, désormais consommée : il vérifie maintenant la table `audit_log`
  (preuve du trajet complet outbox → RabbitMQ → consumer).
- Les compteurs de l'API de management RabbitMQ sont échantillonnés toutes les 5 s : remplacés par
  `rabbitmqctl`, qui donne la valeur exacte.
- `test-dlq` exigeait une erreur 502 précise, alors que nginx renvoie un 502 ou laisse expirer le timeout
  selon l'état du réseau Docker. Les deux sont des erreurs temporaires.
- `test-outbox` laissait 30 inscriptions en attente qui ralentissaient le test suivant : il attend
  maintenant qu'elles soient traitées.

---

## Phase 6 — Documentation, soutenance et validation finale (`318ab84`)

**Objectif** : livrables écrits, tests unitaires et validation depuis une stack vierge.

### Méthode

Le travail a été réparti entre 4 rédacteurs en parallèle (fichiers distincts), puis un vérificateur
indépendant a confronté chaque affirmation de la documentation au code réel, et fait une revue de sécurité.

### Livrables

- **`README.md`** : procédure de démarrage, les 12 tests et leurs scripts, les **15 critères de démo du
  §38** avec la façon de les montrer, justification des codes HTTP, passage à une v2 des événements,
  sécurité, limites connues.
- **`docs/architecture.md`** et **`docs/mermaid.md`** (9 diagrammes, validés avec mermaid-cli).
- **`docs/api.md`** : référence des endpoints et des événements.
- **`docs/soutenance.md`** : les 25 questions, chacune avec la réponse, le code concerné et le test qui
  le prouve, plus des questions pièges probables.
- **`tests/`** : 109 tests unitaires pytest, sans Docker.

### Erreurs trouvées par la vérification et corrigées (13)

Les plus importantes :

- les tests pytest échouaient à l'import si l'on suivait le README → `requirements-dev.txt` inclut
  désormais `requirements.txt` ;
- deux réponses de soutenance affirmaient qu'un message rejoué depuis la DLQ relancerait l'inscription,
  ce que le code ne fait pas (une inscription terminée est ignorée) ;
- la réponse sur les droits Moodle était contredite par `setup_ws.php` ;
- transitions d'état manquantes dans le diagramme, formulations trop larges dans l'architecture.

### Durcissement de la configuration

- Fonction Web Services Moodle inutilisée retirée (`core_enrol_get_enrolled_users`).
- Images épinglées sur les versions réellement testées : RabbitMQ 4.3.6, PostgreSQL 17.11,
  Mailpit v1.31.4, Python 3.12.15.
- `check-infra.sh` utilise le Python du venv et lit ses ports dans `.env`.

### Vérification expérimentale

Le comportement de la limite de 10 livraisons sur les queues dépend de la version de RabbitMQ. Il a été
mesuré sur une queue jetable : sur RabbitMQ 4.3.6, **un message redélivré après un crash du worker compte
bien dans cette limite**. Le message part en DLQ après la 11e livraison. La documentation a été corrigée
en conséquence.

### Validation finale depuis une stack vierge

En suivant le README à la lettre, après `docker compose down -v` :

- les services d'initialisation se terminent en **5 min 14 s**, du premier coup ;
- `check-infra.sh` : **8/8** ;
- **les 15 scripts de bout en bout passent** : **158 vérifications**, 0 échec ;
- pytest : **109 passed** ;
- aucun mot de passe dans les 89 fichiers commités ; `.env` et `.venv` sont ignorés par Git.

---

## Après la livraison

### Interface RabbitMQ blanche dans Chrome

**Symptôme** : la console RabbitMQ (http://localhost:15672) affiche une page blanche dans Chrome, mais
fonctionne dans un autre navigateur.

**Diagnostic** :

- Les logs transmis concernaient le port 5672 (connexions normales des workers), pas l'interface web.
- Côté serveur, tout fonctionne : plugin de management actif, port 15672 à l'écoute, page et scripts
  servis en HTTP 200.
- Le problème vient donc de l'état local de Chrome pour `localhost:15672`. Cause la plus probable : le
  cache, les cookies ou le stockage local laissés par un RabbitMQ d'une version antérieure (TP précédent).
  Une extension (bloqueur de scripts) peut aussi en être la cause.

**Diagnostic confirmé** par les erreurs de la console Chrome (`check_version is not defined`,
`Cannot read properties of undefined (reading 'enabled')` dans `main.js` ligne 11). Le `main.js` servi par
RabbitMQ 4.3.6 définit `check_version` et sa ligne 11 ne lit pas `.enabled` : Chrome exécutait donc une
**ancienne copie de `main.js` en cache** (RabbitMQ 3.x d'un TP précédent, même `localhost:15672`), mélangée
à la nouvelle page. RabbitMQ 4.3.6 envoie `Cache-Control: max-age=0, must-revalidate` : le problème ne
revient plus une fois cette copie supprimée.

**Correctif** (une seule fois) : F12, clic droit sur le bouton Recharger → « Vider le cache et effectuer
une actualisation forcée » ; sinon F12 → Application → Storage → « Clear site data ». Aucune
modification du projet nécessaire.

### Cahier de recette manuelle

**Demande** : un document de tests avec jeux de données, permettant de tester tout le système en saisissant
soi-même les données, du lancement jusqu'à la fin.

**Livrable** : `docs/recette-manuelle.md`, avec :

- des jeux de données nommés (11 apprenants, 20 apprenants pour la concurrence, 2 sessions à créer, dont
  une liée à un article volontairement absent d'ERPNext pour provoquer la compensation) et une fiche de
  suivi des identifiants ;
- 20 sections, du démarrage de la stack au nettoyage : saisie (Swagger, Git Bash, interfaces web),
  résultat attendu, où le vérifier, case à cocher ;
- un tableau de correspondance avec les 12 tests obligatoires, les 15 critères de démo et les codes HTTP.

**Vérification** : le cahier a été exécuté à la lettre sur la stack réelle par un agent indépendant
(suffixe de campagne `rv`), pendant qu'un second agent en vérifiait la couverture et la clarté. 23 étapes
sur 27 étaient conformes du premier coup ; les 4 écarts et les 15 remarques portaient sur la rédaction
(nom de conteneur variable au §14, relevé des places placé après le POST au §15, attente d'ERPNext avant le
crash test, absence d'étape pour le code 500, transitions invisibles au §5.2…). Tous ont été corrigés, et
une démo « en direct » des transitions a été ajoutée (hook `ERP_STEP_DELAY_SECONDS`).

### Nettoyage de la campagne de vérification « rv »

Suppression ciblée, à partir des identifiants enregistrés dans `external_mapping`, chaque objet étant
revérifié avant suppression :

- ERPNext (API REST) : 10 Sales Orders (annulés puis supprimés), 5 Customers, 11 Leads ; le Customer
  `Concurrent Corp`, partagé avec des tests antérieurs, a été conservé ;
- Moodle (API PHP officielle `delete_user`, en administration) : 11 utilisateurs, seulement si leur email
  correspondait à celui attendu ;
- Mailpit (API) : 12 emails ;
- Integration Hub : base d'intégration et queues remises à zéro (`reset-integration.sh`).

Contrôle final : plus aucune donnée `rv` dans aucun système.

### Format d'erreur des exceptions du framework

**Constat** (pendant la vérification du cahier) : un body envoyé avec des accents non encodés en UTF-8
renvoyait `400 {"detail": "There was an error parsing the body"}`, hors du format commun de l'API. Le même
défaut touchait la route inconnue (`404 {"detail": "Not Found"}`) et la méthode non autorisée
(`405 {"detail": "Method Not Allowed"}`) : toutes ces erreurs sont levées par FastAPI/Starlette avant nos
routes, et aucun gestionnaire ne les interceptait.

**Correction** : un gestionnaire unique dans `integration-api/app/errors.py` (`http_error`) les ramène au
format `{"error", "message"}` : `400 MALFORMED_BODY`, `404 NOT_FOUND`, `405 METHOD_NOT_ALLOWED` (en-tête
`Allow` conservé). Un JSON mal formé mais bien encodé reste un `422 VALIDATION_ERROR`.

**Tests** : 6 tests unitaires (`tests/test_api_errors.py`, 115/115 pytest), 3 vérifications de bout en bout
ajoutées à `scripts/test-http-codes.py`, vérification sur l'API en marche ; `docs/api.md` et le cahier de
recette mis à jour.

---

## Limites connues (documentées dans le README)

- Les tokens Moodle et ERPNext sont ceux des comptes administrateurs ; en production, il faudrait des
  comptes techniques à droits minimaux.
- L'API n'a pas d'authentification (Keycloak est le bonus optionnel du sujet).
- Si ERPNext traite une création après notre timeout, un doublon tardif reste théoriquement possible.
- Si Moodle reste indisponible pendant une compensation, l'inscription reste en `COMPENSATING` et demande
  une intervention manuelle.
- Si le notification worker plante entre l'envoi de l'email et l'écriture en base, l'email peut être
  envoyé deux fois (SMTP n'est pas transactionnel).
