# Tests unitaires (pytest)

Ces tests vérifient la logique pure du Hub **sans Docker, sans réseau, sans base** :

| Fichier | Cible |
|---|---|
| `test_request_hash.py` | `app.registrations.request_hash` : normalisation du body pour l'idempotence |
| `test_schemas.py` | `app.schemas` : validation des requêtes (email, champs inconnus, format sessionId, strip/lower) |
| `test_events.py` | `shared.events.new_event`, `shared.rabbit.retry_queue_name` |
| `test_consumer.py` | `shared.consumer.parse_event` et la décision de `Consumer._process` (ack / retry / dlq / reject) |
| `test_moodle_client.py` | `shared.moodle.MoodleClient` : classification des erreurs (httpx.MockTransport) |
| `test_erpnext_client.py` | `shared.erpnext.ErpNextClient` : classification des erreurs et messages lisibles |
| `test_notifications.py` | `workers.notifications.main.render` : sujets par type d'événement |

Les scénarios d'intégration (vraie stack ERPNext / Moodle / RabbitMQ / PostgreSQL) restent dans `scripts/test-*`.

## Lancer

Depuis la racine du dépôt, avec le venv du projet (jamais le Python global) :

```bash
.venv/Scripts/python -m pip install -r requirements.txt -r requirements-dev.txt   # Windows
.venv/Scripts/python -m pytest tests -q

.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt       # Linux/macOS
.venv/bin/python -m pytest tests -q
```

`tests/conftest.py` ajoute la racine et `integration-api/` au `sys.path` et définit des valeurs
factices pour `INTEGRATION_DB_PASSWORD` et `RABBITMQ_PASSWORD` (exigées par `shared/config.py`).
