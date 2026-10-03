from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from shared.config import Settings


def create_pool(settings: Settings, min_size: int = 1) -> ConnectionPool:
    """Pool de connexions en autocommit : chaque unité de travail ouvre explicitement `conn.transaction()`."""
    return ConnectionPool(
        settings.db_dsn,
        min_size=min_size,
        max_size=settings.db_pool_max,
        kwargs={"row_factory": dict_row, "autocommit": True},
        # Vérifie chaque connexion avant de la prêter : reprise propre après un redémarrage de PostgreSQL.
        check=ConnectionPool.check_connection,
        timeout=10,
        open=False,
        name=settings.service_name,
    )
