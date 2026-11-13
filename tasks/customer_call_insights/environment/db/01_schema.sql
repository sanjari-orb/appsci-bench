-- Customer-Call Insights schema. One table: the recorded calls, raw transcript only.

CREATE TABLE IF NOT EXISTS calls (
    call_id      VARCHAR(80)  PRIMARY KEY,
    account_id   VARCHAR(80)  NOT NULL,
    account_name VARCHAR(200) NOT NULL,
    occurred_at  TIMESTAMPTZ  NOT NULL,
    transcript   TEXT         NOT NULL
);
