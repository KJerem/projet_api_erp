"""Configuration lue exclusivement depuis l'environnement (aucun secret dans le code)."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"variable d'environnement manquante : {name}")
    return value


@dataclass(frozen=True)
class Settings:
    service_name: str
    log_level: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_pool_max: int
    rabbitmq_host: str
    rabbitmq_port: int
    rabbitmq_user: str
    rabbitmq_password: str
    http_timeout_seconds: float
    secrets_dir: Path
    catalog_file: Path

    @property
    def db_dsn(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password} connect_timeout=5"
        )

    @property
    def rabbitmq_url(self) -> str:
        return (
            f"amqp://{quote(self.rabbitmq_user, safe='')}:{quote(self.rabbitmq_password, safe='')}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/%2F"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings(
        service_name=os.environ.get("SERVICE_NAME", "integration-hub"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        db_host=os.environ.get("INTEGRATION_DB_HOST", "integration-db"),
        db_port=int(os.environ.get("INTEGRATION_DB_PORT_INTERNAL", "5432")),
        db_name=os.environ.get("INTEGRATION_DB_NAME", "integration"),
        db_user=os.environ.get("INTEGRATION_DB_USER", "integration"),
        db_password=_require("INTEGRATION_DB_PASSWORD"),
        db_pool_max=int(os.environ.get("DB_POOL_MAX", "10")),
        rabbitmq_host=os.environ.get("RABBITMQ_HOST", "rabbitmq"),
        rabbitmq_port=int(os.environ.get("RABBITMQ_PORT_INTERNAL", "5672")),
        rabbitmq_user=os.environ.get("RABBITMQ_USER", "opentraining"),
        rabbitmq_password=_require("RABBITMQ_PASSWORD"),
        http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "5")),
        secrets_dir=Path(os.environ.get("SECRETS_DIR", "/secrets")),
        catalog_file=Path(os.environ.get("CATALOG_FILE", "/app/config/catalog.json")),
    )
