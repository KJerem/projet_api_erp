-- Schéma de la base d'intégration (coordination du SI — ne remplace ni Moodle ni ERPNext).

CREATE TABLE training_session (
    id                      VARCHAR(100) PRIMARY KEY,
    moodle_course_shortname VARCHAR(100) NOT NULL,
    erp_item_code           VARCHAR(140) NOT NULL,
    capacity                INTEGER      NOT NULL CHECK (capacity >= 0),
    -- Garde-fou ultime : même en cas de bug applicatif, la base refuse une capacité négative.
    remaining_seats         INTEGER      NOT NULL CHECK (remaining_seats >= 0),
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT remaining_le_capacity CHECK (remaining_seats <= capacity)
);

CREATE TABLE registration (
    id                 UUID         PRIMARY KEY,
    session_id         VARCHAR(100) NOT NULL REFERENCES training_session (id),
    student_email      VARCHAR(255) NOT NULL,
    student_first_name VARCHAR(100) NOT NULL,
    student_last_name  VARCHAR(100) NOT NULL,
    company_name       VARCHAR(140) NOT NULL,
    status             VARCHAR(30)  NOT NULL CHECK (status IN (
                           'ACCEPTED', 'PROCESSING', 'MOODLE_ENROLLED', 'ERP_RECORDED', 'CONFIRMED',
                           'RETRYING', 'COMPENSATING', 'COMPENSATED', 'FAILED')),
    correlation_id     VARCHAR(100) NOT NULL,
    attempts           INTEGER      NOT NULL DEFAULT 0,
    last_error         TEXT,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Un apprenant ne peut avoir qu'une inscription active par session (filet de sécurité UNIQUE).
CREATE UNIQUE INDEX uq_registration_active_student
    ON registration (session_id, lower(student_email))
    WHERE status NOT IN ('COMPENSATED', 'FAILED');

CREATE TABLE registration_status_history (
    id              BIGSERIAL    PRIMARY KEY,
    registration_id UUID         NOT NULL REFERENCES registration (id),
    status          VARCHAR(30)  NOT NULL,
    detail          TEXT,
    correlation_id  VARCHAR(100),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX ix_status_history_registration ON registration_status_history (registration_id, id);

-- Idempotence HTTP. registration_id est NULL pour les refus métier mémorisés (ex. SESSION_FULL).
CREATE TABLE api_idempotency (
    idempotency_key VARCHAR(255) PRIMARY KEY,
    request_hash    VARCHAR(64)  NOT NULL,
    registration_id UUID REFERENCES registration (id),
    response_status INTEGER,
    response_body   JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Transactional Outbox : écrit dans la même transaction que le changement d'état métier.
CREATE TABLE outbox_event (
    event_id         UUID         PRIMARY KEY,
    event_type       VARCHAR(100) NOT NULL,
    routing_key      VARCHAR(100) NOT NULL,
    aggregate_type   VARCHAR(50)  NOT NULL,
    aggregate_id     VARCHAR(100) NOT NULL,
    correlation_id   VARCHAR(100) NOT NULL,
    payload          JSONB        NOT NULL,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    published_at     TIMESTAMPTZ,
    publish_attempts INTEGER      NOT NULL DEFAULT 0,
    last_error       TEXT
);
CREATE INDEX ix_outbox_unpublished ON outbox_event (created_at) WHERE published_at IS NULL;

-- Déduplication des consumers (livraison at-least-once).
CREATE TABLE processed_events (
    consumer_name VARCHAR(100) NOT NULL,
    event_id      UUID         NOT NULL,
    processed_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_id)
);

-- Correspondance des identifiants internes <-> Moodle / ERPNext.
CREATE TABLE external_mapping (
    entity_type VARCHAR(50)  NOT NULL,
    internal_id UUID         NOT NULL,
    system_name VARCHAR(50)  NOT NULL,
    external_id VARCHAR(255) NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (entity_type, internal_id, system_name)
);

-- Journal d'audit alimenté par l'audit-worker.
CREATE TABLE audit_log (
    id             BIGSERIAL    PRIMARY KEY,
    event_id       UUID         NOT NULL UNIQUE,
    event_type     VARCHAR(100) NOT NULL,
    correlation_id VARCHAR(100),
    payload        JSONB        NOT NULL,
    received_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
