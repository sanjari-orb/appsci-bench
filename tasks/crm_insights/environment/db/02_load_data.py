#!/usr/bin/env python3
"""Load crm_insights fixture JSONL files into the Postgres database.

Run after 01_schema.sql has created the tables. Table/column mapping mirrors the real Salesforce
object/field names (see RESOURCES.md) -- just lower_snake_cased and with 'crm_'
prefixed on the two Postgres reserved words (user, order).
"""
import json
import os

TABLES = [
    ("case", "crm_case", [
        ("Id", "id"), ("Priority", "priority"), ("Subject", "subject"),
        ("Description", "description"), ("Status", "status"), ("ContactId", "contact_id"),
        ("CreatedDate", "created_date"), ("ClosedDate", "closed_date"),
        ("OrderItemId__c", "order_item_id"), ("IssueId__c", "issue_id"),
        ("AccountId", "account_id"), ("OwnerId", "owner_id"),
    ]),
    ("account", "crm_account", [
        ("Id", "id"), ("FirstName", "first_name"), ("LastName", "last_name"),
        ("PersonEmail", "person_email"), ("Phone", "phone"), ("RecordTypeId", "record_type_id"),
        ("ShippingCity", "shipping_city"), ("ShippingState", "shipping_state"),
    ]),
    ("user", "crm_user", [
        ("Id", "id"), ("FirstName", "first_name"), ("LastName", "last_name"),
        ("Email", "email"), ("Phone", "phone"), ("Username", "username"), ("Alias", "alias"),
        ("LanguageLocaleKey", "language_locale_key"), ("EmailEncodingKey", "email_encoding_key"),
        ("TimeZoneSidKey", "time_zone_sid_key"), ("LocaleSidKey", "locale_sid_key"),
    ]),
    ("casehistory", "crm_case_history", [
        ("Id", "id"), ("CaseId__c", "case_id"), ("OldValue__c", "old_value"),
        ("NewValue__c", "new_value"), ("CreatedDate", "created_date"), ("Field__c", "field"),
    ]),
    ("order", "crm_order", [
        ("Id", "id"), ("AccountId", "account_id"), ("Status", "status"),
        ("EffectiveDate", "effective_date"), ("Pricebook2Id", "pricebook2_id"),
    ]),
    ("orderitem", "crm_order_item", [
        ("Id", "id"), ("OrderId", "order_id"), ("Product2Id", "product2_id"),
        ("Quantity", "quantity"), ("UnitPrice", "unit_price"),
        ("PriceBookEntryId", "price_book_entry_id"),
    ]),
    ("issue", "crm_issue", [("Id", "id"), ("Name", "name")]),
    ("product2", "crm_product2", [
        ("Id", "id"), ("Name", "name"), ("Description", "description"),
        ("IsActive", "is_active"), ("External_ID__c", "external_id"),
    ]),
]


def load_table(cursor, fixture_file, table_name, field_map):
    src_cols = [c[0] for c in field_map]
    dst_cols = [c[1] for c in field_map]
    placeholders = ", ".join(["%s"] * len(dst_cols))
    sql = (f"INSERT INTO {table_name} ({', '.join(dst_cols)}) VALUES ({placeholders}) "
           "ON CONFLICT DO NOTHING")
    with open(fixture_file) as f:
        for line in f:
            rec = json.loads(line)
            cursor.execute(sql, tuple(rec.get(c) for c in src_cols))


if __name__ == "__main__":
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "crm_insights"),
        user=os.environ.get("POSTGRES_USER", "sandbox"),
        password=os.environ.get("POSTGRES_PASSWORD", "sandbox"),
    )
    try:
        with conn.cursor() as cursor:
            for fixture_name, table_name, field_map in TABLES:
                load_table(cursor, f"/fixture/{fixture_name}.jsonl", table_name, field_map)
        conn.commit()
        print("Data loaded successfully")
    finally:
        conn.close()
