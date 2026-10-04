# Cahier de recette manuelle — Mini SI OpenTraining

Ce document permet de tester **tout le système à la main**, du lancement de la stack jusqu'au nettoyage,
en saisissant soi-même les données. Chaque étape indique l'action, les données à saisir, le résultat
attendu et où le vérifier. Les scripts automatisés (`scripts/test-*`) font la même chose sans
intervention : ce cahier sert à **voir et comprendre** chaque mécanisme, et à préparer la démo.

Durée indicative : 1 h 30 à 2 h pour l'ensemble (dont ~5 min de premier démarrage).

---

## 0. Conventions

**Outils**

| Outil | Usage dans ce cahier |
|---|---|
| Terminal **Git Bash** (Windows) ou bash | commandes `docker compose`, `curl`, `psql` — à lancer depuis la racine du dépôt |
| **Swagger** http://localhost:8000/docs | saisie des requêtes de l'API (bouton **Try it out**, puis **Execute**) |
| **ERPNext** http://localhost:8080 | vérification des Lead, Customer et Sales Order |
| **Moodle** http://localhost:8081 | vérification des utilisateurs et des inscriptions aux cours |
| **Mailpit** http://localhost:8025 | lecture des emails envoyés aux apprenants |
| **RabbitMQ** http://localhost:15672 | queues, DLQ, publication manuelle de messages |

**Identifiants** (tous dans le fichier `.env`, jamais dans Git)

| Interface | Utilisateur | Mot de passe |
|---|---|---|
| ERPNext | `Administrator` | valeur de `ERPNEXT_ADMIN_PASSWORD` |
| Moodle | valeur de `MOODLE_ADMIN_USER` (`admin`) | valeur de `MOODLE_ADMIN_PASSWORD` |
| RabbitMQ | valeur de `RABBITMQ_USER` (`opentraining`) | valeur de `RABBITMQ_PASSWORD` |

Le compte `guest/guest` de RabbitMQ n'existe pas dans ce projet.

**Suffixe de campagne** : les données ci-dessous portent le suffixe **`r1`** (emails, clés, entreprises)
et **`R1`** (sessions). Moodle et ERPNext conservent leurs données d'une campagne à l'autre : pour
rejouer le cahier, remplace `r1`/`R1` par `r2`/`R2`, etc. Sinon, les utilisateurs Moodle et les Lead
ERPNext existants seront **réutilisés** au lieu d'être créés (comportement normal et idempotent, mais
moins visible).

**Raccourcis utilisés dans les commandes** — à coller dans **chaque nouveau terminal** Git Bash, toujours
ouvert **à la racine du dépôt** (`cd …/projet_api_erp`) : ils ne survivent pas à la fermeture du terminal.

```bash
API=http://localhost:8000
# Requête SQL en lecture sur la base d'intégration :
sqli() { docker compose exec -T integration-db psql -U integration -d integration -c "$1"; }
```

**Accents** : les bodys contenant des accents (Chloé, Gaëlle) se saisissent dans **Swagger**. En `curl`
sous Git Bash, l'argument n'est pas envoyé en UTF-8 et l'API répond `400 MALFORMED_BODY` (« JSON encodé
en UTF-8 attendu ») ; passe alors le body par un fichier UTF-8 (`--data-binary @body.json`). Toutes les
commandes `curl` de ce cahier sont en ASCII.

**Ordre de la saga** (utile pour comprendre les pannes) : à la réception d'une inscription, le worker
appelle d'abord **ERPNext** (Lead, puis Customer), puis **Moodle** (cours, utilisateur, inscription →
`MOODLE_ENROLLED`), puis de nouveau **ERPNext** (Sales Order → `ERP_RECORDED`), et termine par `CONFIRMED`.

Chaque étape se termine par une case à cocher : `- [ ]` → `- [x]` quand le résultat attendu est observé.

---

## 1. Jeux de données

### 1.1 Apprenants

| Réf. | Prénom | Nom | Email | Entreprise | Session | Idempotency-Key | Utilisé en |
|---|---|---|---|---|---|---|---|
| P1 | Alice | Martin | `alice.martin.r1@example.org` | `ACME Formation r1` | `SESSION-PYTHON-01` | `recette-r1-alice` | §5 parcours nominal |
| P2 | Bruno | Petit | `bruno.petit.r1@example.org` | `ACME Formation r1` | `SESSION-PYTHON-01` | `recette-r1-bruno` | §9 outbox |
| P3 | Chloé | Durand | `chloe.durand.r1@example.org` | `Globex r1` | `SESSION-PYTHON-01` | `recette-r1-chloe` | §11 panne Moodle |
| P4 | David | Leroy | `david.leroy.r1@example.org` | `Globex r1` | `SESSION-PYTHON-01` | `recette-r1-david` | §12 panne ERPNext temporaire |
| P5 | Emma | Roux | `emma.roux.r1@example.org` | `Initech r1` | `SESSION-PYTHON-01` | `recette-r1-emma` | §14 crash du worker |
| P6 | Farid | Benali | `farid.benali.r1@example.org` | `Initech r1` | `SESSION-COMPENSATION-R1` | `recette-r1-farid` | §15 compensation |
| P7 | Gaëlle | Moreau | `gaelle.moreau.r1@example.org` | `Umbrella r1` | `SESSION-PYTHON-01` | `recette-r1-gaelle` | §13 DLQ (retries épuisés) |
| P8 | Hugo | Lambert | `hugo.lambert.r1` (invalide) | `Umbrella r1` | `SESSION-PYTHON-01` | `recette-r1-hugo` | §7 erreurs de validation |
| P8' | Hugo | Lambert | `hugo.lambert.r1@example.org` (corrigé) | `Umbrella r1` | `SESSION-PYTHON-01` | `recette-r1-dbdown` | §17 base indisponible |
| P9 | Inès | Garnier | `ines.garnier.r1@example.org` | `Globex r1` | `SESSION-PYTHON-01` | `recette-r1-ines` | §9.3 RabbitMQ arrêté |
| T1 | Trace | Test | `trace.r1@example.org` | `ACME Formation r1` | `SESSION-PYTHON-01` | `recette-r1-trace` | §18.1 correlation ID |
| C1…C20 | Concurrent | N1…N20 | `conc1.r1@example.org` … | `Concurrent Corp r1` | `SESSION-LASTSEAT-R1` | `recette-r1-conc-1` … | §8 concurrence (un seul gagnant) |

**Clés de secours** : si une étape de panne (§11, §12.1) dépasse la fenêtre de retry, refais-la avec un
**nouvel apprenant** (ex. `chloe.durand.r1b@example.org`, clé `recette-r1-chloe-2`). Réutiliser le même
email avec une nouvelle clé donnerait `409 ALREADY_REGISTERED` si la première inscription est toujours active.

Modèle de body JSON pour `POST /api/v1/registrations` (exemple P1) :

```json
{
  "student": { "email": "alice.martin.r1@example.org", "firstName": "Alice", "lastName": "Martin" },
  "company": { "name": "ACME Formation r1" },
  "sessionId": "SESSION-PYTHON-01"
}
```

### 1.2 Sessions

| Session | Capacité | Cours Moodle | Article ERPNext | Origine |
|---|---|---|---|---|
| `SESSION-PYTHON-01` | 50 | `PYTHON-ADV` (« Python avancé ») | `FORMATION-PYTHON` (850 EUR) | créée au démarrage (`infra/catalog.json`) |
| `SESSION-CONCURRENCY` | 1 | `CONCURRENCY` | `FORMATION-CONCURRENCY` (300 EUR) | créée au démarrage |
| `SESSION-LASTSEAT-R1` | **1** | `CONCURRENCY` | `FORMATION-CONCURRENCY` | créée par toi en §4 |
| `SESSION-COMPENSATION-R1` | 5 | `PYTHON-ADV` | **`ARTICLE-INEXISTANT`** (absent d'ERPNext, volontairement) | créée par toi en §4 |

### 1.3 Fiche de suivi

Note au fur et à mesure les identifiants renvoyés par l'API, ils resservent plus loin :

| Réf. | registrationId | moodleUserId | erpCustomerId | erpSalesOrderId | Statut final |
|---|---|---|---|---|---|
| P1 | | | | | |
| P2 | | | | | |
| P3 | | | | | |
| P4 | | | | | |
| P5 | | | | | |
| P6 | | | | | |
| P7 | | | | | |
| P8' | | | | | |
| P9 | | | | | |
| T1 | | | | | |
| Cx (gagnant §8) | | | | | |

---

## 2. Lancement du système

### 2.1 Préparer l'environnement (première fois uniquement)

```bash
python -m venv .venv                # si `python` ouvre le Microsoft Store : py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt     # Linux/macOS : .venv/bin/python
.venv/Scripts/python scripts/init-env.py                        # crée .env avec des mots de passe aléatoires
```

- [ ] `.env` existe et contient des mots de passe (pas de `change-me`).

### 2.2 Démarrer la stack

```bash
docker compose up -d --build
```

Le premier démarrage dure **environ 5 minutes** (création du site ERPNext et installation de Moodle).
Suivre l'avancement :

```bash
docker compose ps -a
docker compose logs -f erpnext-bootstrap moodle-bootstrap     # Ctrl+C pour quitter
```

**Résultat attendu** :

- `erpnext-configurator`, `erpnext-create-site`, `erpnext-bootstrap`, `moodle-bootstrap` et `migrate` sont
  en **`Exited (0)`** ;
- `moodle`, `moodle-db`, `integration-db`, `rabbitmq`, `mailpit`, `erpnext-db` et `integration-api` sont
  **`(healthy)`** ; les autres services sont **`Up`** ;
- les derniers logs affichent « bootstrap ERPNext terminé » et « bootstrap Moodle terminé ».

- [ ] Tous les services sont dans l'état attendu.

### 2.3 Vérifier l'infrastructure

```bash
bash scripts/check-infra.sh
```

**Attendu** : `Résultat : 8 PASS / 0 FAIL` (ERPNext, Moodle, RabbitMQ, PostgreSQL et Mailpit répondent
par leurs API officielles, avec les tokens générés au démarrage).

- [ ] 8 PASS.

### 2.4 Se connecter aux interfaces

Ouvre chaque interface (§0) et connecte-toi.

- [ ] ERPNext : bureau affiché, société **OpenTraining**.
- [ ] Moodle : site « OpenTraining LMS », cours **Python avancé** et **Concurrence et systèmes distribués**
  présents (*Mes cours* ou *Administration du site → Cours → Gestion des cours*).
- [ ] RabbitMQ : onglet **Queues** listant `registration.orchestrator`, `notifications`, `audit`, les trois
  `*.dlq` et les queues `*.retry.2s/4s/8s`.
- [ ] Mailpit : boîte de réception affichée.
- [ ] Swagger : page « OpenTraining Integration API ».

> Page blanche sur RabbitMQ dans Chrome ? C'est le cache du navigateur (ancienne version de RabbitMQ
> d'un TP précédent) : F12, clic droit sur le bouton Recharger → « Vider le cache et effectuer une
> actualisation forcée ».

### 2.5 Partir d'un état propre

```bash
bash scripts/reset-integration.sh -y
```

Supprime les queues RabbitMQ (recréées vides par les services au redémarrage) et recrée la base
d'intégration. Moodle et ERPNext ne sont pas touchés.

**Attendu** : en fin de sortie, `SESSION-CONCURRENCY | 1/1 places` et `SESSION-PYTHON-01 | 50/50 places`.

- [ ] Reset effectué.

---

## 3. Consultations initiales

Dans Swagger :

| Étape | Requête | Attendu |
|---|---|---|
| 3.1 | `GET /health` | `200` `{"status": "UP"}` |
| 3.2 | `GET /ready` | `200` `{"status": "UP", "checks": {"postgres": "UP", "rabbitmq": "UP"}, "pendingOutboxEvents": 0}` |
| 3.3 | `GET /api/v1/sessions` | `200`, 2 sessions : `SESSION-CONCURRENCY` (1/1) et `SESSION-PYTHON-01` (50/50), `activeRegistrations` à 0 |
| 3.4 | `GET /api/v1/catalog` | `200`, pour `SESSION-PYTHON-01` : `courseName` « Python avancé », `remainingSeats` 50, `price` 850, `currency` « EUR » |

Le catalogue (3.4) est une **agrégation synchrone** : le nom du cours vient de Moodle, les places de
PostgreSQL, le prix d'ERPNext. Recoupe-le :

- [ ] ERPNext → *Stock* → *Item Price* : `FORMATION-PYTHON` = 850 (liste *Standard Selling*).
- [ ] Moodle : le cours `PYTHON-ADV` s'appelle « Python avancé ».
- [ ] Les quatre requêtes renvoient le résultat attendu.

---

## 4. Création des sessions de recette

Dans Swagger, `POST /api/v1/sessions`, body :

```json
{ "sessionId": "SESSION-LASTSEAT-R1", "capacity": 1,
  "moodleCourseShortname": "CONCURRENCY", "erpItemCode": "FORMATION-CONCURRENCY" }
```

**Attendu** : `201 Created`, en-tête `location: /api/v1/sessions/SESSION-LASTSEAT-R1`, body avec
`remainingSeats: 1`.

Puis :

```json
{ "sessionId": "SESSION-COMPENSATION-R1", "capacity": 5,
  "moodleCourseShortname": "PYTHON-ADV", "erpItemCode": "ARTICLE-INEXISTANT" }
```

**Attendu** : `201`.

Renvoie **une seconde fois** le premier body (`SESSION-LASTSEAT-R1`).

**Attendu** : `409` `{"error": "SESSION_ALREADY_EXISTS", ...}`.

- [ ] 201, 201, puis 409.

---

## 5. Parcours nominal — Tests 1 et 12 (P1 Alice)

### 5.1 Demander l'inscription

`POST /api/v1/registrations`, champ **Idempotency-Key** : `recette-r1-alice`, body P1 (§1.1).

**Attendu** : `202 Accepted`, body `{"registrationId": "<uuid>", "status": "ACCEPTED"}`, en-tête
`location: /api/v1/registrations/<uuid>`. Le 202 signifie « place réservée, traitement en cours » :
Moodle et ERPNext ne sont pas encore appelés.

- [ ] 202 reçu, `registrationId` noté dans la fiche de suivi.

### 5.2 Suivre le statut (test 12)

`GET /api/v1/registrations/{registration_id}` avec l'identifiant noté.

**Attendu** : le traitement ne dure que 1 à 3 secondes ; le temps de coller l'identifiant dans Swagger,
le premier GET montre souvent déjà **`CONFIRMED`**. Les transitions se lisent alors dans le champ
`history`. La réponse finale contient :

- `moodleUserId`, `courseId` (2), `erpLeadId` (`CRM-LEAD-…`), `erpCustomerId` (`ACME Formation r1`),
  `erpSalesOrderId` (`SAL-ORD-…`), `attempts: 1`, `lastError: null` ;
- `history` : `ACCEPTED` → `PROCESSING` → `MOODLE_ENROLLED` → `ERP_RECORDED` → `CONFIRMED`.

- [ ] Statut `CONFIRMED`, historique complet, identifiants notés.

**Voir les transitions en direct** (facultatif, recommandé pour la démo) : le hook de test
`ERP_STEP_DELAY_SECONDS` met le worker en pause 10 s entre l'inscription Moodle et le Sales Order.

```bash
ERP_STEP_DELAY_SECONDS=10 docker compose up -d registration-worker
ID=$(curl -s -X POST $API/api/v1/registrations -H "Content-Type: application/json" \
  -H "Idempotency-Key: recette-r1-live" \
  -d '{"student":{"email":"live.r1@example.org","firstName":"Live","lastName":"Demo"},"company":{"name":"ACME Formation r1"},"sessionId":"SESSION-PYTHON-01"}' \
  | sed 's/.*"registrationId":"\([^"]*\)".*/\1/')
for i in $(seq 1 20); do curl -s $API/api/v1/registrations/$ID | grep -o '"status":"[A-Z_]*"' | head -1; sleep 1; done
docker compose up -d registration-worker      # retour au mode normal (obligatoire)
```

**Attendu** : `ACCEPTED`/`PROCESSING`, puis `MOODLE_ENROLLED` pendant ~10 s, puis `CONFIRMED`.

### 5.3 Vérifier dans Moodle

- *Administration du site → Utilisateurs → Parcourir la liste des utilisateurs* : l'utilisateur
  `alice.martin.r1@example.org` existe (identifiant = email).
- Cours *Python avancé* → *Participants* (http://localhost:8081/user/index.php?id=2) : Alice est inscrite
  comme *Étudiant*.

- [ ] Utilisateur créé et inscrit au cours.

### 5.4 Vérifier dans ERPNext

- *CRM → Lead* : un Lead « Alice Martin », email `alice.martin.r1@example.org`.
- *Selling → Customer* : un Customer `ACME Formation r1` (groupe *Commercial*, territoire *France*).
- *Selling → Sales Order* : la commande `erpSalesOrderId`, statut **To Deliver and Bill** (soumise),
  client `ACME Formation r1`, article `FORMATION-PYTHON`, total **850 EUR**, champ *Customer's Purchase
  Order* = le `registrationId` (c'est ce lien qui permet de retrouver la commande sans doublon).

- [ ] Lead, Customer et Sales Order présents et cohérents.

### 5.5 Vérifier l'email et l'audit

- Mailpit : un email « **Inscription confirmée — SESSION-PYTHON-01** » adressé à Alice, qui contient la
  référence d'inscription et le numéro de Sales Order.
- Journal d'audit :

```bash
sqli "SELECT event_type, correlation_id, received_at FROM audit_log ORDER BY id"
```

**Attendu** : `registration.accepted` puis `registration.confirmed`, avec le même `correlation_id`.

- [ ] Email reçu, deux événements audités.

### 5.6 Places restantes

`GET /api/v1/sessions/SESSION-PYTHON-01` → **`remainingSeats: 49`**, `activeRegistrations: 1`.

- [ ] 49 places.

---

## 6. Idempotence HTTP — Tests 2 et 3

### 6.1 Même clé, même body (test 2)

Renvoie exactement la requête du §5.1 : Idempotency-Key `recette-r1-alice`, body P1.

**Attendu** : `202`, **même `registrationId`** qu'en 5.1, en-tête **`idempotent-replayed: true`**. Le
body contient toujours `"status": "ACCEPTED"` : c'est la réponse d'origine rejouée, l'état courant se lit
par le GET.

Vérifie qu'**aucun effet n'a été produit** :

- [ ] `GET /api/v1/sessions/SESSION-PYTHON-01` : toujours **49** places.
- [ ] Moodle : toujours **un seul** utilisateur `alice.martin.r1@example.org`.
- [ ] ERPNext : toujours un seul Lead, un seul Customer et un seul Sales Order pour Alice.
- [ ] Mailpit : toujours **un seul** email pour Alice.

### 6.2 Body écrit autrement (normalisation)

Même clé `recette-r1-alice`, body équivalent mais présenté différemment :

```json
{
  "sessionId": "SESSION-PYTHON-01",
  "company": { "name": "  ACME Formation r1  " },
  "student": { "lastName": "Martin", "firstName": "Alice", "email": "ALICE.MARTIN.R1@Example.org" }
}
```

**Attendu** : `202` rejoué avec le même `registrationId` (ordre des clés, espaces et casse de l'email
n'entrent pas dans l'empreinte).

- [ ] Rejeu confirmé.

### 6.3 Même clé, body différent (test 3)

Même clé `recette-r1-alice`, body P1 avec `"lastName": "Dupont"`.

**Attendu** : `409` `{"error": "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", "message": "Cette
Idempotency-Key a déjà été utilisée avec un contenu différent."}`. Rien n'est créé.

- [ ] 409 reçu.

---

## 7. Contrat HTTP et erreurs de validation

| Étape | Saisie (Swagger) | Attendu |
|---|---|---|
| 7.1 | `POST /registrations`, Idempotency-Key **vide**, body P1 | `400` `IDEMPOTENCY_KEY_MISSING` |
| 7.2 | clé `recette-r1-hugo`, body P8 (email `hugo.lambert.r1`) | `422` `VALIDATION_ERROR`, `details` pointe `student.email` |
| 7.3 | clé `recette-r1-hugo-2`, body P8 corrigé (`hugo.lambert.r1@example.org`) + un champ `"promo": "X"` à la racine | `422` `VALIDATION_ERROR` (champ inconnu refusé) |
| 7.4 | clé `recette-r1-hugo-3`, body valide de Hugo avec `"sessionId": "SESSION-INCONNUE"` | `404` `SESSION_NOT_FOUND` |
| 7.5 | clé `recette-r1-alice-bis` (nouvelle clé), body P1 | `409` `ALREADY_REGISTERED`, avec le `registrationId` d'Alice |
| 7.6 | `GET /registrations/00000000-0000-0000-0000-000000000000` | `404` `REGISTRATION_NOT_FOUND` |
| 7.7 | `GET /registrations/pas-un-uuid` | `422` `VALIDATION_ERROR` |
| 7.8 | `GET /api/v1/inexistant` (navigateur ou curl) | `404` `{"error": "NOT_FOUND", "message": "Ressource inconnue : GET /api/v1/inexistant"}` — même format que les erreurs métier |
| 7.9 | *(non déclenchable par une saisie)* | `500` `INTERNAL_ERROR` : réservé aux erreurs imprévues (bug). Body `{"error": "INTERNAL_ERROR", "message": "Erreur interne inattendue.", "correlationId": "corr-…"}`, sans détail interne ; le correlation ID permet de retrouver la trace dans les logs. Code : middleware de `integration-api/app/main.py`, `internal_error_response` dans `app/errors.py`. Une dépendance en panne donne `503`, pas `500` (§16, §17). |

Correlation ID (en terminal, Swagger ne permet pas d'ajouter cet en-tête) :

```bash
curl -si $API/health -H "X-Correlation-ID: corr-recette-r1" | grep -i x-correlation-id
curl -si $API/health | grep -i x-correlation-id
```

**Attendu** : la première renvoie `x-correlation-id: corr-recette-r1`, la seconde un identifiant généré
`corr-<uuid>`.

- [ ] Les 7 cas et le correlation ID se comportent comme attendu.

---

## 8. Concurrence sur la dernière place — Test 4

`SESSION-LASTSEAT-R1` n'a qu'**une** place. On envoie **20 demandes simultanées**, avec 20 apprenants et
20 clés différents (C1 à C20). Dans Git Bash :

```bash
( for i in $(seq 1 20); do
    curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/api/v1/registrations \
      -H "Content-Type: application/json" -H "Idempotency-Key: recette-r1-conc-$i" \
      -d "{\"student\":{\"email\":\"conc$i.r1@example.org\",\"firstName\":\"Concurrent\",\"lastName\":\"N$i\"},\"company\":{\"name\":\"Concurrent Corp r1\"},\"sessionId\":\"SESSION-LASTSEAT-R1\"}" &
  done; wait ) | sort | uniq -c
```

**Attendu** :

```
      1 202
     19 409
```

Puis `GET /api/v1/sessions/SESSION-LASTSEAT-R1` → `remainingSeats: 0` (**jamais -1**),
`activeRegistrations: 1`. Les 19 refus portent l'erreur `SESSION_FULL` :

```bash
sqli "SELECT response_status, response_body->>'error' AS erreur, count(*) FROM api_idempotency
      WHERE idempotency_key LIKE 'recette-r1-conc-%' GROUP BY 1, 2"
```

**Attendu** : `202 | (vide) | 1` et `409 | SESSION_FULL | 19`.

Pourquoi ça tient : chaque demande verrouille la ligne de la session (`SELECT … FOR UPDATE`) avant de
décrémenter, et la base refuse de toute façon une capacité négative (`CHECK`).

- [ ] 1 acceptée, 19 refusées, 0 place restante.

Variante plus forte (script) : `.venv/Scripts/python scripts/test-concurrency.py --requests 50`.

---

## 9. Outbox — Test 10 (P2 Bruno)

### 9.1 Dispatcher arrêté

```bash
docker compose stop outbox-dispatcher
```

`POST /registrations`, clé `recette-r1-bruno`, body P2.

**Attendu** :

- `202` immédiat : l'API ne dépend pas de RabbitMQ ;
- `GET /ready` → `pendingOutboxEvents: 1` ;
- `GET /registrations/{id}` de Bruno → reste **`ACCEPTED`** (aucun worker n'a reçu l'événement) ;
- en base, l'événement attend :

```bash
sqli "SELECT o.event_type, o.published_at FROM outbox_event o JOIN registration r ON o.aggregate_id = r.id::text
      WHERE r.student_email = 'bruno.petit.r1@example.org' ORDER BY o.created_at"
```

→ une ligne `registration.accepted | (vide)`.

- [ ] Événement en attente dans l'outbox, inscription bloquée en `ACCEPTED`.

### 9.2 Redémarrage du dispatcher

```bash
docker compose start outbox-dispatcher
```

**Attendu** (quelques secondes) : la même requête SQL montre `registration.accepted | <date>` (publié),
puis une seconde ligne `registration.confirmed | <date>` écrite par le worker ; `GET /ready` →
`pendingOutboxEvents: 0`, et Bruno passe à **`CONFIRMED`**. Dans ERPNext, Bruno **réutilise** le Customer `ACME Formation r1` créé pour
Alice (pas de second Customer).

- [ ] Événement publié, Bruno `CONFIRMED`, Customer réutilisé.

### 9.3 Variante : RabbitMQ arrêté

```bash
docker compose stop rabbitmq
```

`GET /ready` → `503`, `checks.rabbitmq: "DOWN"`.

Pendant l'arrêt, `POST /registrations`, clé `recette-r1-ines`, body P9 (Inès) :

**Attendu** : `202` (l'API n'a pas besoin du broker), `GET /ready` → `pendingOutboxEvents: 1`, Inès reste
`ACCEPTED`. Remets RabbitMQ en route :

```bash
docker compose start rabbitmq
```

**Attendu** : `GET /ready` repasse à `200` après **20 à 30 s** (les workers et le dispatcher, coupés de
RabbitMQ, sont relancés automatiquement par Docker — `restart: unless-stopped` — puis se reconnectent) ;
`pendingOutboxEvents: 0` et Inès passe à **`CONFIRMED`**. L'événement n'a pas été perdu.

- [ ] `/ready` 503, inscription d'Inès en attente, puis 200 et Inès `CONFIRMED`.

---

## 10. Message RabbitMQ dupliqué — Test 5

On republie à la main l'événement d'Alice, déjà traité.

1. Récupère l'événement exact écrit dans l'outbox :

```bash
docker compose exec -T integration-db psql -U integration -d integration -At -c \
  "SELECT payload FROM outbox_event o JOIN registration r ON o.aggregate_id = r.id::text
   WHERE r.student_email = 'alice.martin.r1@example.org' AND o.event_type = 'registration.accepted'"
```

   L'option `-At` affiche le JSON brut, sans en-tête : copie toute la ligne, de `{` à `}`.

2. RabbitMQ → *Exchanges* → **`si.events`** → *Publish message* :
   - *Routing key* : `registration.accepted`
   - *Payload* : le JSON copié
   - clique **Publish message** — **deux fois**.

**Attendu** :

```bash
docker compose logs --since 2m registration-worker | grep "doublon"
```

→ deux lignes « événement déjà traité : ACK sans nouvel effet (doublon) ». Aucun effet : historique
d'Alice inchangé (5 entrées), toujours un seul utilisateur Moodle, un seul Sales Order, un seul email.

Pourquoi : l'`eventId` est déjà dans `processed_events` pour ce consumer, le message est acquitté sans
refaire le traitement.

- [ ] Doublons acquittés sans effet.

---

## 11. Moodle indisponible — Test 6 (P3 Chloé)

Le gel du conteneur provoque un vrai **timeout** réseau (5 s par appel). La fenêtre de retry est d'environ
30 secondes : prépare les deux commandes avant de commencer.

```bash
docker compose pause moodle
```

`POST /registrations`, clé `recette-r1-chloe`, body P3. Puis GET de Chloé toutes les 2-3 secondes.

**Attendu** : après ~5 s, statut **`RETRYING`**, `attempts` qui augmente (1, puis 2), `lastError`
« Moodle timeout (core_course_get_courses_by_field) ». **Dès que tu vois `RETRYING`**, remets Moodle :

```bash
docker compose unpause moodle
```

**Attendu** : à la tentative suivante (2, 4 ou 8 s plus tard), Chloé passe à **`CONFIRMED`**,
`lastError: null`. L'historique montre la tentative échouée (`RETRYING` — « tentative 1 échouée, nouvel
essai dans 2s : Moodle timeout … ») puis la tentative suivante réussie ; il y a autant de `RETRYING` que de
tentatives échouées avant le dégel. Le message n'a jamais été perdu. Le Lead et le Customer ERPNext de
Chloé existent déjà : la saga appelle ERPNext avant Moodle.

Deux cas particuliers :

- si la première erreur affichée est `ERPNext timeout (POST /api/resource/Lead)`, c'est ERPNext qui
  n'était pas prêt (démarrage récent ou sortie de veille du PC), pas Moodle. Dégèle Moodle, laisse Chloé
  se terminer, puis refais l'étape avec un nouvel apprenant (`chloe.durand.r1b@example.org`, clé
  `recette-r1-chloe-2`) ;
- si tu dépasses ~30 s avant de dégeler, les 4 tentatives s'épuisent : Chloé passe en `FAILED` et sa
  place est libérée (comportement du §13). Refais l'étape de la même façon.

- [ ] `RETRYING` visible, puis `CONFIRMED` après le retour de Moodle.

---

## 12. ERPNext indisponible — Test 7

### 12.1 Erreur temporaire (P4 David)

```bash
docker compose pause erpnext-backend
```

`POST /registrations`, clé `recette-r1-david`, body P4. GET de David toutes les 2-3 s.

**Attendu** : `RETRYING`, `lastError` « ERPNext timeout (GET /api/resource/Lead) ». Puis :

```bash
docker compose unpause erpnext-backend
```

→ David passe à **`CONFIRMED`**. Une erreur temporaire est **retentée**.

Comme au §11, dégèle **dès que tu vois `RETRYING`** : au-delà de ~30 s, David passe en `FAILED` (§13).
Refais alors l'étape avec `david.leroy.r1b@example.org` et la clé `recette-r1-david-2`.

- [ ] Retry puis `CONFIRMED`.

### 12.2 Erreur permanente

Reprend le cas P6 du §15 : l'article `ARTICLE-INEXISTANT` provoque un **HTTP 417** d'ERPNext. Ce refus est
définitif : **aucun retry** (`attempts: 1`, pas de `RETRYING` dans l'historique). C'est la différence
entre erreur temporaire et permanente.

- [ ] Vérifié au §15.

---

## 13. DLQ — Test 8

### 13.1 Message illisible

RabbitMQ → *Exchanges* → `si.events` → *Publish message* : routing key `registration.accepted`, payload
`ceci n'est pas du JSON (recette r1)`.

**Attendu** : le message est rejeté sans retry et arrive dans **`registration.dlq`**.

```bash
.venv/Scripts/python scripts/inspect-dlq.py
```

→ une entrée « payload non JSON … », motif `rejected`. Le même message arrive aussi dans **`audit.dlq`**
(la queue `audit` reçoit tous les événements et le rejette elle aussi) :
`.venv/Scripts/python scripts/inspect-dlq.py audit.dlq` montre la même entrée. Préfère `inspect-dlq.py` au
compteur de la console RabbitMQ, qui peut afficher 0 pendant quelques secondes (statistiques échantillonnées).

- [ ] Message illisible en DLQ.

### 13.2 Retries épuisés (P7 Gaëlle)

ERPNext arrêté durablement (pas seulement gelé) :

```bash
docker compose stop erpnext-backend
```

`POST /registrations`, clé `recette-r1-gaelle`, body P7. Suis Gaëlle par GET pendant ~30 s.

**Attendu** :

- historique `ACCEPTED → PROCESSING → RETRYING → PROCESSING → RETRYING → PROCESSING → RETRYING →
  PROCESSING → FAILED` : 4 tentatives, espacées de 2 s, 4 s puis 8 s ;
- statut **`FAILED`**, `attempts: 4`, `lastError` « ERPNext HTTP 502 … » ou « ERPNext timeout … » ;
- **place libérée** (`GET /sessions/SESSION-PYTHON-01` : le compteur est revenu à sa valeur d'avant) ;
- email « **Inscription non aboutie** » dans Mailpit ;
- `inspect-dlq.py` : une entrée `registration.accepted` de Gaëlle, motif `retries exhausted`,
  `tentatives: 4`.
- Gaëlle n'a **aucun compte Moodle** : la saga a échoué dès la recherche du Lead ERPNext, avant d'atteindre
  Moodle.

Remets ERPNext en route, puis **attends qu'il réponde** avant de continuer (le §14 en a besoin) :

```bash
docker compose start erpnext-backend
until curl -fs http://localhost:8080/api/method/ping >/dev/null; do sleep 2; done; echo "ERPNext prêt"
```

- [ ] `FAILED`, place libérée, message en DLQ avec sa cause ; ERPNext de nouveau prêt.

---

## 14. Crash du worker — Test 9 (P5 Emma)

Prérequis : ERPNext et Moodle répondent (`bash scripts/check-infra.sh` → 8 PASS).

On redémarre le worker avec un mode qui le fait **planter juste après l'inscription Moodle** : le Lead et
le Customer ERPNext existent déjà, mais l'inscription Moodle n'est pas encore enregistrée localement
(mapping `moodle_enrolment`) et le message RabbitMQ n'est pas acquitté.

```bash
CRASH_AFTER_MOODLE=true docker compose up -d registration-worker
```

`POST /registrations`, clé `recette-r1-emma`, body P5. Attends ~10 s.

**Attendu** :

```bash
docker compose logs registration-worker | grep "arrêt brutal"
docker inspect -f '{{.RestartCount}}' $(docker compose ps -q registration-worker)
```

→ la ligne « CRASH_AFTER_MOODLE : arrêt brutal après l'inscription Moodle, avant mapping et ACK » et un
`RestartCount` ≥ 1 (Docker a relancé le worker).
RabbitMQ a redélivré le message non acquitté, et Emma finit en **`CONFIRMED`**.

- historique : **deux** `PROCESSING` (le crash, puis la reprise) ;
- Moodle : **un seul** utilisateur `emma.roux.r1@example.org`, inscrit **une seule fois** ;
- ERPNext : un seul Lead et un seul Sales Order pour Emma.

Remets le worker en mode normal :

```bash
docker compose up -d registration-worker
```

- [ ] Crash, redémarrage, `CONFIRMED` sans doublon ; worker remis en mode normal.

---

## 15. Compensation — Test 11 (P6 Farid)

1. **Note d'abord les places** : `GET /api/v1/sessions/SESSION-COMPENSATION-R1` → `remainingSeats: 5`.
2. Puis `POST /registrations`, clé `recette-r1-farid`, body P6 (session `SESSION-COMPENSATION-R1`). Un GET
   de la session juste après peut montrer `4` le temps du traitement.

**Attendu** (quelques secondes) :

- historique `ACCEPTED → PROCESSING → MOODLE_ENROLLED → COMPENSATING → COMPENSATED` : Farid a bien été
  inscrit dans Moodle, puis ERPNext a refusé le Sales Order ;
- `attempts: 1` (erreur permanente, aucun retry), `lastError` contient « **HTTP 417** » et
  « ARTICLE-INEXISTANT » ;
- Moodle : l'utilisateur Farid **existe toujours**, mais il **n'est plus inscrit** au cours *Python avancé*
  (désinscription de compensation) ;
- ERPNext : **aucun** Sales Order pour Farid ;
- `GET /sessions/SESSION-COMPENSATION-R1` → `remainingSeats: 5` (place libérée) ;
- Mailpit : email « **Inscription annulée** » ;
- audit : `registration.accepted` puis `registration.compensated`, à vérifier avec l'identifiant de Farid :

```bash
sqli "SELECT event_type, received_at FROM audit_log
      WHERE payload->'payload'->>'registrationId' = '<registrationId de Farid>' ORDER BY id"
```

- [ ] Compensation complète.

---

## 16. Catalogue en mode dégradé

```bash
docker compose pause moodle
```

| Requête | Attendu |
|---|---|
| `GET /api/v1/catalog` | `503` `CATALOG_DEPENDENCY_UNAVAILABLE`, `sources.moodle` « Moodle timeout … », réponse en ~5 s (timeout borné) |
| `GET /api/v1/catalog?partial=true` | `200` (lui aussi après ~5 s, le temps du timeout Moodle), en-tête `x-catalog-degraded: moodle`, `courseName: null` partout ; pour `SESSION-PYTHON-01`, `price: 850` et `remainingSeats` présents. `SESSION-COMPENSATION-R1` a toujours `price: null`, même hors panne : son article n'existe pas dans ERPNext (voulu) |

```bash
docker compose unpause moodle
```

- [ ] 503 en mode strict, 200 dégradé en mode partiel.

---

## 17. Base d'intégration indisponible (503)

```bash
docker compose stop integration-db
```

| Requête | Attendu |
|---|---|
| `POST /registrations` (clé `recette-r1-dbdown`, body P8') | `503` `SERVICE_UNAVAILABLE`, en-tête `retry-after: 5`. La réponse arrive après **~10 s** (l'API attend une connexion de son pool avant d'abandonner) : Swagger n'est pas figé |
| `GET /ready` | `503`, `checks.postgres: "DOWN"` |
| `GET /health` | `200` (le processus vit, seule la base manque) |

```bash
docker compose start integration-db
until docker compose exec -T integration-db pg_isready -q; do sleep 1; done; echo "PostgreSQL prêt"
```

Attends que `GET /ready` renvoie `200`, puis rejoue le même POST, **même clé** : `202`. Un client qui reçoit un 503 peut réessayer sans risque de
doublon.

- [ ] 503, puis 202 avec la même clé après reprise.

---

## 18. Observabilité et bilan

### 18.1 Suivre une inscription de bout en bout par son correlation ID

```bash
curl -s -X POST $API/api/v1/registrations -H "Content-Type: application/json" \
  -H "Idempotency-Key: recette-r1-trace" -H "X-Correlation-ID: corr-recette-r1-trace" \
  -d '{"student":{"email":"trace.r1@example.org","firstName":"Trace","lastName":"Test"},"company":{"name":"ACME Formation r1"},"sessionId":"SESSION-PYTHON-01"}'
sleep 5
docker compose logs integration-api outbox-dispatcher registration-worker notification-worker audit-worker \
  | grep corr-recette-r1-trace
```

**Attendu** : des lignes JSON de **chaque** service (API, dispatcher, worker, notifier, audit) portant
`"correlationId": "corr-recette-r1-trace"`.

- [ ] Correlation ID présent dans les logs de tous les services.

### 18.2 Bilan en base

```bash
sqli "SELECT status, count(*) FROM registration GROUP BY status ORDER BY status"
sqli "SELECT id, capacity, remaining_seats FROM training_session ORDER BY id"
```

**Attendu** : uniquement des statuts finaux (`CONFIRMED`, `COMPENSATED`, `FAILED`) ; pour chaque session,
`capacity - remaining_seats` = nombre d'inscriptions `CONFIRMED` de la session.

Décompte attendu si aucune étape n'a été refaite : `SESSION-PYTHON-01` compte **8** inscriptions
`CONFIRMED` (Alice, Bruno, Inès, Chloé, David, Emma, Hugo P8', Trace) → **42 places restantes** ; ajoute
1 par étape refaite avec une clé de secours et 1 si tu as fait la démo « en direct » du §5.2.
`SESSION-LASTSEAT-R1` : 1 `CONFIRMED`, 0 place. `SESSION-COMPENSATION-R1` : 0 `CONFIRMED`, 5 places.
Gaëlle (`FAILED`) et Farid (`COMPENSATED`) ont rendu leur place. Si tu as lancé la variante
`test-concurrency.py` du §8, une session `SESSION-CONCURRENCY-xxxx` supplémentaire apparaît (normal).

- [ ] Aucune inscription bloquée, compteurs cohérents.

---

## 19. Fin de recette et nettoyage

| Commande | Effet |
|---|---|
| `bash scripts/reset-integration.sh -y` | Remet le Hub à zéro (queues + base d'intégration) ; Moodle et ERPNext gardent leurs données |
| `docker compose down` | Arrête tout ; **conserve** les données (redémarrage rapide) |
| `docker compose down -v` | Arrête tout et **supprime toutes les données** (prochain démarrage : ~5 min, nouveaux tokens) |

---

## 20. Récapitulatif — correspondance avec le sujet

| Test obligatoire | Section | Critère de démo (§38) | Section |
|---|---|---|---|
| 1 Happy path | §5 | 1 stack démarrée | §2 |
| 2 Idempotency-Key répétée | §6.1 | 2 POST inscription / 3 202 | §5.1 |
| 3 Clé réutilisée, autre body | §6.3 | 4 statut asynchrone | §5.2 |
| 4 Concurrence | §8 | 5-6 utilisateur et inscription Moodle | §5.3 |
| 5 Message dupliqué | §10 | 7 client / document ERPNext | §5.4 |
| 6 Moodle indisponible | §11 | 8 notification Mailpit | §5.5 |
| 7 ERPNext indisponible | §12 | 9 même clé sans doublon | §6.1 |
| 8 DLQ | §13 | 10 concurrence sans surbooking | §8 |
| 9 Crash worker | §14 | 11 message dupliqué | §10 |
| 10 Outbox | §9 | 12 panne Moodle avec retry | §11 |
| 11 Compensation | §15 | 13 message en DLQ | §13 |
| 12 Status API | §5.2 | 14 redémarrage worker | §14 |
| | | 15 compensation | §15 |

| Code HTTP | Où le voir | Autres exigences | Où le voir |
|---|---|---|---|
| 200 OK | §3, §5.2 | Correlation ID | §7, §18.1 |
| 201 Created | §4 | Catalogue agrégé | §3.4, §16 |
| 202 Accepted | §5.1, §6.1 | `/health` | §3.1, §17 |
| 400 Bad Request | §7.1 | `/ready` | §3.2, §9, §17 |
| 404 Not Found | §7.4, §7.6, §7.8 | Logs JSON corrélés | §18.1 |
| 409 Conflict | §4, §6.3, §7.5, §8 | Audit des événements | §5.5, §15 |
| 422 Unprocessable Entity | §7.2, §7.3, §7.7 | Swagger / OpenAPI | §2.4, §3 |
| 500 Internal Server Error | §7.9 (non déclenchable par saisie) | | |
| 503 Service Unavailable | §9.3, §16, §17 | | |
