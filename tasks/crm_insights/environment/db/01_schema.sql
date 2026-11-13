CREATE TABLE IF NOT EXISTS crm_case (
    id            VARCHAR(20) PRIMARY KEY,
    priority      VARCHAR(20),
    subject       TEXT,
    description   TEXT,
    status        VARCHAR(30),
    contact_id    VARCHAR(20),
    created_date  TIMESTAMPTZ,
    closed_date   TIMESTAMPTZ,
    order_item_id VARCHAR(20),
    issue_id      VARCHAR(20),
    account_id    VARCHAR(20),
    owner_id      VARCHAR(20)
);

CREATE TABLE IF NOT EXISTS crm_account (
    id             VARCHAR(20) PRIMARY KEY,
    first_name     VARCHAR(100),
    last_name      VARCHAR(100),
    person_email   VARCHAR(200),
    phone          VARCHAR(50),
    record_type_id VARCHAR(20),
    shipping_city  VARCHAR(100),
    shipping_state VARCHAR(10)
);

CREATE TABLE IF NOT EXISTS crm_user (
    id                  VARCHAR(20) PRIMARY KEY,
    first_name          VARCHAR(100),
    last_name           VARCHAR(100),
    email               VARCHAR(200),
    phone               VARCHAR(50),
    username            VARCHAR(200),
    alias               VARCHAR(50),
    language_locale_key VARCHAR(20),
    email_encoding_key  VARCHAR(20),
    time_zone_sid_key   VARCHAR(50),
    locale_sid_key      VARCHAR(20)
);

CREATE TABLE IF NOT EXISTS crm_case_history (
    id           VARCHAR(20) PRIMARY KEY,
    case_id      VARCHAR(20),
    old_value    TEXT,
    new_value    TEXT,
    created_date TIMESTAMPTZ,
    field        VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS crm_order (
    id             VARCHAR(20) PRIMARY KEY,
    account_id     VARCHAR(20),
    status         VARCHAR(30),
    effective_date DATE,
    pricebook2_id  VARCHAR(20)
);

CREATE TABLE IF NOT EXISTS crm_order_item (
    id                VARCHAR(20) PRIMARY KEY,
    order_id          VARCHAR(20),
    product2_id       VARCHAR(20),
    quantity          VARCHAR(20),
    unit_price        VARCHAR(20),
    price_book_entry_id VARCHAR(20)
);

CREATE TABLE IF NOT EXISTS crm_issue (
    id   VARCHAR(20) PRIMARY KEY,
    name TEXT
);

CREATE TABLE IF NOT EXISTS crm_product2 (
    id            VARCHAR(20) PRIMARY KEY,
    name          TEXT,
    description   TEXT,
    is_active     INTEGER,
    external_id   VARCHAR(100)
);
