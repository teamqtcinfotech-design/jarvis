-- JARVIS integration tables. Run ONLY on the TaskPanel database (never the main software DB).
-- Additive: creates new tables, alters nothing that exists. Take a TaskPanel DB backup first.

CREATE TABLE IF NOT EXISTS taskpanel_integration_grants (
    id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    client_id    VARCHAR(64)  NOT NULL,
    grant_hash   CHAR(64)     NOT NULL,
    staff_key    VARCHAR(64)  NOT NULL,
    label        VARCHAR(120) NOT NULL DEFAULT '',
    scopes       VARCHAR(255) NOT NULL,
    expires_at   DATETIME     NULL,
    revoked_at   DATETIME     NULL,
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_grant_hash (grant_hash),
    KEY idx_grant_staff (staff_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS taskpanel_integration_nonces (
    client_id  VARCHAR(64) NOT NULL,
    nonce      VARCHAR(64) NOT NULL,
    expires_at DATETIME    NOT NULL,
    PRIMARY KEY (client_id, nonce),
    KEY idx_nonce_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS taskpanel_integration_commands (
    id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    client_id    VARCHAR(64) NOT NULL,
    command_id   CHAR(36)    NOT NULL,
    actor_key    VARCHAR(64) NOT NULL,
    type         VARCHAR(40) NOT NULL,
    payload_hash CHAR(64)    NOT NULL,
    state        VARCHAR(16) NOT NULL,          -- processing | succeeded | rejected
    http_status  SMALLINT    NOT NULL DEFAULT 0,
    result       LONGTEXT    NULL,
    task_id      BIGINT UNSIGNED NULL,
    created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_client_command (client_id, command_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS taskpanel_sync_entities (
    task_id       BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    uuid          CHAR(36)    NOT NULL,
    sync_revision INT UNSIGNED NOT NULL DEFAULT 0,
    last_hash     CHAR(64)    NOT NULL DEFAULT '',
    updated_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_entity_uuid (uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS taskpanel_sync_feed (
    seq           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    event_id      CHAR(36)    NOT NULL,
    environment   VARCHAR(20) NOT NULL,
    task_id       BIGINT UNSIGNED NOT NULL,
    task_uuid     CHAR(36)    NOT NULL,
    sync_revision INT UNSIGNED NOT NULL,
    event_type    VARCHAR(40) NOT NULL,
    payload       LONGTEXT    NOT NULL,
    created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_feed_event (event_id),
    KEY idx_feed_task (task_id),
    KEY idx_feed_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS taskpanel_sync_state (
    name       VARCHAR(64)  NOT NULL PRIMARY KEY,
    value      VARCHAR(255) NOT NULL,
    updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
