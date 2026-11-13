-- Compliance Review Database Schema
-- One table: build-time-safe sample contracts, raw text only. No clause-presence or violation
-- columns -- that data is the oracle the artifact must produce itself, not read off a table.

CREATE TABLE IF NOT EXISTS contracts (
    contract_id VARCHAR(80) PRIMARY KEY,
    title       VARCHAR(500) UNIQUE NOT NULL,
    full_text   TEXT NOT NULL
);
