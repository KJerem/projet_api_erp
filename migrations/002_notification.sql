-- Trace des notifications envoyées par le notification-worker (une ligne par événement notifié).
CREATE TABLE notification (
    event_id        UUID         PRIMARY KEY,
    event_type      VARCHAR(100) NOT NULL,
    registration_id UUID,
    recipient       VARCHAR(255) NOT NULL,
    subject         VARCHAR(255) NOT NULL,
    correlation_id  VARCHAR(100),
    sent_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX ix_notification_registration ON notification (registration_id);
