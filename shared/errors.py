"""Classification des erreurs d'intégration : elle décide entre retry et DLQ/compensation."""


class IntegrationError(Exception):
    """Erreur lors d'un appel à un système externe ou d'un traitement de message."""


class TransientError(IntegrationError):
    """Erreur temporaire, à retenter : timeout, connexion refusée, HTTP 5xx / 429."""


class PermanentError(IntegrationError):
    """Erreur définitive, inutile de retenter : HTTP 4xx, payload invalide, règle métier impossible."""


class DuplicateInFlight(Exception):
    """Le même événement est déjà en cours de traitement par un autre worker (livraison dupliquée)."""
