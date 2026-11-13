# CRM Insights — schema reference

The database (`crm_db`) mirrors a real Salesforce object schema (Case/Account/User/...),
lower_snake_cased for Postgres, with the two Postgres reserved words (`user`, `order`) prefixed
`crm_`. Column names map 1:1 to the original Salesforce field names below.

## crm_case (Salesforce `Case`)
| column | Salesforce field | notes |
|---|---|---|
| id | Id | |
| priority | Priority | High / Medium / Low |
| subject | Subject | |
| description | Description | |
| status | Status | Closed / Working / Waiting on Customer |
| contact_id | ContactId | |
| created_date | CreatedDate | when the case was opened |
| closed_date | ClosedDate | null if not yet closed |
| order_item_id | OrderItemId\_\_c | join to crm_order_item -> crm_product2, for "which product" questions |
| issue_id | IssueId\_\_c | join to crm_issue |
| account_id | AccountId | join to crm_account |
| owner_id | OwnerId | **current** owner — join to crm_user; see crm_case_history for prior owners |

## crm_account (Salesforce `Account`, person-account style)
first_name, last_name, person_email, phone, record_type_id, shipping_city, **shipping_state**
(2-letter state code — this is what "region"/"state" questions mean).

## crm_user (Salesforce `User`) — the support agents
id, first_name, last_name, email, phone, username, alias, plus locale fields.

## crm_case_history (Salesforce `CaseHistory__c`) — the audit trail
One row per tracked field change on a case. `field = 'Owner Assignment'` rows are what a case
transfer looks like: `old_value` = the agent transferring it away, `new_value` = the new owner.
A case with more than one `'Owner Assignment'` row has been transferred; the first such row (by
`created_date`) names the case's original/first-assigned agent. `field = 'Case Creation'` and
`field = 'Case Closed'` rows mark those lifecycle events.

## crm_order / crm_order_item (Salesforce `Order` / `OrderItem`)
An order belongs to an account; an order item belongs to an order and references a product
(`product2_id`). A case's `order_item_id` is how you get from a case to the product it's about.

## crm_issue (Salesforce `Issue__c`)
A small fixed catalog (10 rows) of named problem categories a case can be tagged with
(`crm_case.issue_id`).

## crm_product2 (Salesforce `Product2`)
The product catalog (name, description, active flag).

## What this document does not cover
The exact quarter/season windows used in questions, the Case Routing tie-break order, and the
Handle Time / Transfer Count first-assigned-agent filter are operational definitions the answers
depend on but are not written here — ask the persona posing the question; that's how a real analyst
would confirm an ambiguous convention too.
