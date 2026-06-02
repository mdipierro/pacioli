# Pacioli ERP — API Manual

The Pacioli ERP back office is an **API-first, plain-text-auditable financial +
inventory core**. Every operation is available as a JSON REST endpoint; writes
go through the posting engine (`pacioli_erp.erp`), which turns each document
into a balanced double-entry ledger transaction.

- **Base URL:** `http://<host>:8000/pacioli/api`
  (the app is mounted at `/pacioli`; the REST API lives under `/pacioli/api`).
- **Format:** all requests and responses are JSON unless noted (the invoice PDF
  endpoint returns `application/pdf`).
- **Scope:** the API owns *money and things* — ledger, inventory, invoicing. It
  deliberately does **not** own *people* (CRM, HR, payroll, storefront).

---

## 1. Authentication

Two mechanisms, both resolving to the same logged-in user:

| Mechanism | How |
|-----------|-----|
| **Bearer token** (programmatic) | Send `Authorization: Bearer <token>`. Tokens are created by a logged-in user at **`/pacioli/tokens`** (stored in `auth_simple_token`). |
| **Session cookie** (browser) | Log in at `/pacioli/auth/login`; the session cookie authorizes same-origin API calls (this is what the built-in UI uses). |

### Auth modes

Whether the API enforces auth depends on `settings.API_REQUIRE_AUTH`, derived
from the `PY4WEB_MODE` environment variable:

- **Secured (default):** `PY4WEB_MODE` unset or not `development` →
  `API_REQUIRE_AUTH = True`. Every `/api/*` endpoint (except `/api/health`)
  requires a valid token or session, returning **401** otherwise.
- **Open (development):** `PY4WEB_MODE=development` → the API is open for local
  testing (no credentials needed). The HTML reports are still gated in the UI.

> Note: the `--dev` flag on `py4web run` only enables the auto-reloader; it does
> **not** set development mode. Set `PY4WEB_MODE=development` explicitly to open
> the API.

### Errors

Failures return the matching HTTP status and a JSON body:

```json
{ "error": "human-readable message" }
```

Common codes: `400` (bad input / unbalanced posting), `401` (auth required),
`404` (not found), `501` (PDF export when `fpdf2` is not installed).

---

## 2. Conventions

- **Money & quantities** are returned as **strings** carrying full decimal
  precision (e.g. `"220.000000"`), to avoid float rounding. Inputs accept
  numbers or numeric strings.
- **Dates** are ISO `YYYY-MM-DD`. Omit to default to today.
- **Default currency** is `USD` (`DEFAULT_CURRENCY`).
- **Amount lists** — ledger balances are multi-commodity, returned as a list of
  `{ "value": "...", "commodity": "USD" }`.
- **Accounts** are colon-paths: `Assets:Bank`, `Income:Sales`,
  `Liabilities:AccountsPayable`, etc.
- **Tags** — every system table is taggable; most write endpoints accept a
  `tags: [...]` array and echo tags back.

Default chart-of-accounts anchors: `Assets:Bank`, `Assets:AccountsReceivable`,
`Liabilities:AccountsPayable`, `Liabilities:Tax`, `Income:Sales`.

---

## 3. Endpoint reference

### 3.1 Meta

#### `GET /api/health`
Public (no auth). Service heartbeat + row counts.
```json
{ "ok": true, "service": "pacioli-erp", "version": "0.1.0",
  "accounts": 30, "transactions": 2 }
```

---

### 3.2 Ledger & reports

#### `GET /api/accounts`
Trial balance as a flat list (alias of the report below).
```json
{ "accounts": [ { "account": "Assets:Bank",
                  "amounts": [ { "value": "220", "commodity": "USD" } ] } ] }
```

#### `GET /api/accounts/<name>`
Balance of one account (colon-path, URL-encoded).
```json
{ "account": "Assets:Bank", "amounts": [ { "value": "220", "commodity": "USD" } ] }
```

#### `GET /api/ledger/transactions`
All ledger transactions with their postings. Optional `?from=YYYY-MM-DD&to=YYYY-MM-DD`.
```json
{ "transactions": [
  { "id": 1, "date": "2026-06-01", "state": "*", "info": "Invoice #1",
    "postings": [ { "account": "Assets:AccountsReceivable:bob", "amount": "220", "commodity": "USD" } ] }
] }
```

#### `POST /api/ledger/journals`
Write a balanced transaction directly. Postings must net to zero (debits
positive, credits negative).
```json
// request
{ "date": "2026-06-01", "memo": "Owner capital",
  "currency": "USD",
  "lines": [ { "account": "Assets:Bank", "amount": 1000 },
             { "account": "Equity:Opening", "amount": -1000 } ] }
// response
{ "txn": 7 }
```

#### `GET /api/reports/trial_balance`
```json
{ "trial_balance": [ { "account": "Assets:Bank",
                       "amounts": [ { "value": "220", "commodity": "USD" } ] } ] }
```

#### `GET /api/reports/profit_and_loss`
Optional `?begin=YYYY-MM-DD&end=YYYY-MM-DD` window.
```json
{ "income":   [ { "account": "Income:Sales", "amounts": [ { "value": "200", "commodity": "USD" } ] } ],
  "expenses": [ { "account": "Expenses:COGS", "amounts": [ { "value": "50", "commodity": "USD" } ] } ],
  "total_income": "200", "total_expenses": "50", "net": "150" }
```

#### `GET /api/reports/balance_sheet`
Optional `?date=YYYY-MM-DD` (as-of). Buckets accounts into assets/liabilities/equity.
```json
{ "assets": [ { "account": "Assets:Bank", "amounts": [ ... ] } ],
  "liabilities": [ ... ], "equity": [ ... ] }
```

---

### 3.3 Parties

A **party** is a customer and/or vendor. Contact fields (`email`, `phone`,
`address`) were added for invoicing; `address` is a free-text multi-line block.

Party object:
```json
{ "id": 3, "name": "Bob", "kind": "customer", "currency": "USD",
  "ar_account": "Assets:AccountsReceivable:bob",
  "ap_account": "Liabilities:AccountsPayable:bob",
  "external_ref": null,
  "email": "bob@example.com", "phone": "+1 555 0100",
  "address": "12 Oak St\nSpringfield, IL 62704",
  "tags": [] }
```

#### `GET /api/parties`
`{ "parties": [ <party>, ... ] }`

#### `POST /api/parties`
Create. `name` required; `kind` is `customer|vendor|both` (default `customer`).
```json
{ "name": "Bob", "kind": "customer", "currency": "USD",
  "email": "bob@example.com", "phone": "+1 555 0100",
  "address": "12 Oak St\nSpringfield, IL 62704",
  "external_ref": "CRM-42", "tags": ["vip"] }
```
Returns `{ "party": <party> }`. Creating a party also provisions its AR/AP
sub-accounts in the chart of accounts.

#### `GET /api/parties/<id>`
Returns the party plus its ledger balance:
```json
{ ...<party>, "balance": { "receivable": [ ... ], "payable": [ ... ] } }
```

#### `PUT /api/parties/<id>`
Update any of: `name`, `kind`, `currency`, `external_ref`, `email`, `phone`,
`address`. Returns the updated party (with `balance`).

---

### 3.4 Items & inventory

Item object:
```json
{ "id": 1, "sku": "TOY-1", "name": "Wooden toy", "uom": "unit",
  "valuation": "average", "cost": "5.000000", "price": "20.000000",
  "tracked": true,
  "inventory_account": "Assets:Inventory:TOY-1",
  "income_account": "Income:Sales",
  "cogs_account": "Expenses:COGS",
  "tags": [] }
```

#### `GET /api/items` → `{ "items": [ <item>, ... ] }`

#### `POST /api/items`
`sku` required. `tracked: false` marks a service item (no stock/COGS).
```json
{ "sku": "TOY-1", "name": "Wooden toy", "cost": "5.00", "price": "20.00",
  "tracked": true, "income_account": "Income:Sales",
  "cogs_account": "Expenses:COGS", "inventory_account": "Assets:Inventory:TOY-1",
  "tags": ["toys"] }
```
Returns `{ "item": <item> }`.

#### `GET /api/items/<id>`
Item plus on-hand quantity: `{ ...<item>, "qty": "100.000000" }`.

#### `PUT /api/items/<id>`
Update any of: `name`, `price`, `uom`, `tracked`.

#### `GET /api/stock`
On-hand levels and valuation (qty × moving-average cost):
```json
{ "stock": [ { "id": 1, "sku": "TOY-1", "name": "Wooden toy",
               "qty": "100.000000", "cost": "5.000000", "value": "500.00" } ] }
```

#### `POST /api/stock/moves`
Record a stock movement (posts to the ledger at moving-average cost).
```json
{ "item": 1, "kind": "receipt", "qty": "100", "unit_cost": "5.00",
  "location": null, "to_location": null, "date": "2026-06-01",
  "external_id": "MOVE-1" }
```
`kind` is `receipt|issue|adjust|transfer`. Returns `{ "move": <id> }`.

#### `GET /api/stock/reconcile`
Compares operational stock value with the ledger inventory accounts:
```json
{ "ok": true,
  "items": [ { "sku": "TOY-1", "operational_value": "500.00",
               "ledger_value": "500.00", "match": true } ] }
```

---

### 3.5 Invoices, bills, payments

These are **documents** (`kind` = `invoice|bill|payment`). Posting an invoice
books AR + revenue (+ COGS for tracked items); a bill books AP + inventory/
expense; a payment moves cash and settles AR/AP. Posted documents are
immutable — to change one, **void** it (UI) and re-create.

Document object:
```json
{ "id": 5, "kind": "invoice", "party": 3,
  "party_name": "Bob", "party_address": "12 Oak St\nSpringfield, IL 62704",
  "date": "2026-06-01", "status": "posted", "currency": "USD",
  "subtotal": "200.000000", "tax_total": "20.000000", "total": "220.000000",
  "allocated": "0.000000", "memo": "", "external_id": null, "txn": 1, "tags": [] }
```

> `party_name` and `party_address` are **snapshotted from the party at creation
> time**, so the document stays accurate even if the party is later edited.

**Line format** (invoices & bills) — each line is item-based *or* account-based:
```json
{ "item": 1, "description": "Wooden toy", "qty": 10,
  "unit_price": "20.00", "tax_rate": "0.10" }
// or, for a service / expense line:
{ "account": "Expenses:Consulting", "description": "Audit", "qty": 1, "unit_price": "500" }
```

#### `GET /api/invoices` → `{ "invoices": [ <document>, ... ] }`  (newest first)
#### `POST /api/invoices`
```json
{ "party": 3, "date": "2026-06-01", "memo": "Order #99",
  "external_id": "INV-99", "tags": ["q2"],
  "lines": [ { "item": 1, "qty": 10, "unit_price": "20.00", "tax_rate": "0.10" } ] }
```
Returns `{ "invoice": <document> }`. (`external_id` is idempotent — re-posting
the same id returns the existing invoice.)

#### `GET /api/bills` · `POST /api/bills`
Same shape as invoices; returns `{ "bills": [...] }` / `{ "bill": <document> }`.

#### `GET /api/invoices/<id>` · `GET /api/bills/<id>`
Document detail **with its lines**:
```json
{ ...<document>,
  "lines": [ { "item": 1, "account": null, "description": "Wooden toy",
               "qty": "10.000000", "unit_price": "20.000000",
               "tax_rate": "0.1000", "amount": "200.000000" } ] }
```

#### `GET /api/invoices/<id>/pdf`
Download the invoice as a **PDF** (`Content-Type: application/pdf`,
`Content-Disposition: attachment; filename="invoice-<id>.pdf"`). Multi-page
invoices paginate with a repeating business header/footer and table headings.
The business identity (name, logo, address, contact) comes from
`apps/pacioli/config.toml`; the bill-to comes from the invoice's snapshot.

Requires the `fpdf2` package — returns **501** with a hint if it is missing.

```bash
curl -H "Authorization: Bearer $TOKEN" \
     -o invoice-5.pdf http://localhost:8000/pacioli/api/invoices/5/pdf
```

#### `GET /api/payments` → `{ "payments": [ <document>, ... ] }`
#### `POST /api/payments`
Record a payment and optionally allocate it to invoices/bills.
```json
{ "party": 3, "amount": "220.00", "direction": "in",
  "account": "Assets:Bank", "date": "2026-06-01", "memo": "",
  "external_id": "PAY-1",
  "allocations": [ { "document": 5, "amount": "220.00" } ],
  "tags": [] }
```
`direction` is `in` (customer pays you) or `out` (you pay a vendor). Returns
`{ "payment": <document> }`.

---

### 3.6 Events

#### `GET /api/events`
Append-only event log (e.g. `invoice.posted`, `bill.posted`,
`payment.received`). Optional `?since=<id>` to fetch only newer events; capped
at 500.
```json
{ "events": [ { "id": 12, "kind": "invoice.posted",
                "payload": { "id": 5, "party": "Bob", "total": "220.000000" },
                "created_on": "2026-06-01 10:00:00" } ] }
```

---

### 3.7 Search & tags

System tables: `account, txn, posting, chk, price, party, item, location,
stock, document, document_line, allocation, event`.

#### `GET /api/search/<table>`
Search by text identifier and/or tags.
- `?q=<text>` — match the table's text identifier (party/account/location name,
  item sku, document external_id, …).
- `?tag=a&tag=b` — repeatable; `?mode=and|or` combines them (default `and`).
```json
{ "table": "item", "count": 1,
  "results": [ { "id": 1, "label": "TOY-1", "tags": ["toys"] } ] }
```

#### `GET /api/tags/<table>/<id>`
Current tags on a record: `{ "table": "item", "id": 1, "tags": ["toys"] }`.

#### `POST /api/tags/<table>/<id>`
Add/remove tags: `{ "add": ["clearance"], "remove": ["promo"] }`. Returns the
updated tag list.

---

## 4. Configuration — `apps/pacioli/config.toml`

Business identity used for the invoice PDF header/footer. `logo` is resolved
relative to the app folder (`apps/pacioli`); leave empty to omit the image.

```toml
[business]
name    = "Pacioli ERP, Inc."
logo    = "static/media/pacioli.png"
address = """
123 Ledger Lane, Suite 100
Springfield, IL 62704
United States
"""
email   = "billing@example.com"
phone   = "+1 (555) 010-2030"
website = "https://example.com"
tax_id  = ""          # shown in the footer when set
```

---

## 5. Back-office UI (HTML routes)

Not part of the JSON API, but served by the same app. All require a logged-in
user except **home** (`/pacioli/index`) and the auth pages.

| Route | Purpose |
|-------|---------|
| `/pacioli/index` | Vue single-page back office: reports (trial balance, P&L, balance sheet), search. **Public landing**; report views require login. |
| `/pacioli/parties`, `/pacioli/items` | Master-data grids (create/edit/delete). |
| `/pacioli/stock`, `/pacioli/invoices`, `/pacioli/bills`, `/pacioli/payments` | Transaction list grids + create forms; invoices have a **PDF** download button per row. |
| `/pacioli/events` | Event log grid. |
| `/pacioli/tokens` | Create/revoke your API bearer tokens. |
| `/pacioli/docs`, `/pacioli/docs/<name>` | **Public** documentation pages (this manual, architecture, overview), rendered from the repo markdown. Linked from the sidebar; available without login. |
| `/pacioli/auth/*` | Login, register, profile (py4web Auth). |

---

## 6. Quick start

```bash
# 1. run the app (fpdf2 enables PDF export)
uv run --with py4web --with fpdf2 py4web run apps -A pacioli --dev
#    -> http://127.0.0.1:8000/pacioli/

# 2. open /pacioli/tokens (after logging in) to mint a bearer token
TOKEN=...

# 3. call the API
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/pacioli/api/health
curl -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name":"Bob","kind":"customer","email":"bob@example.com"}' \
     http://127.0.0.1:8000/pacioli/api/parties
curl -H "Authorization: Bearer $TOKEN" \
     -o invoice-5.pdf http://127.0.0.1:8000/pacioli/api/invoices/5/pdf
```
