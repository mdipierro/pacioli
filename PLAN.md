# Pacioli ERP core — design & API plan

> An API-first, plain-text-auditable **financial + inventory core**.
> It owns *money and things* (ledger, inventory, invoicing). It deliberately does
> **not** own *people* (CRM, HR, payroll, the storefront). Developers build their
> own logic around the API.

The whole system collapses to one sentence:

> **A document is a request to move value. The core validates it, moves stock,
> emits a *balanced* pacioli ledger transaction, and fires an event.
> Pacioli guarantees the "balanced" part.**

---

## 1. The seam: people vs. value

| Outside the core (people) | Inside the core (value & things) |
|---|---|
| CRM, leads, contacts, addresses | Ledger (double-entry, pacioli) |
| HR, employees, payroll calculation | Inventory (stock levels + valuation) |
| Pricing / promotions / tax rules | Documents: invoice, bill, payment, journal |
| Storefront, POS, the end-user UI | Reports: trial balance, P&L, balance sheet |

The two domains meet at exactly **two narrow places**:

1. **Opaque party references** — the core stores a `party` as id + name + kind; it
   never knows what a "customer" or "employee" *is*.
2. **Journal postings** — anything a people-system needs to record financially
   (e.g. a payroll run) is pushed in as a balanced journal entry or a bill.
   *HR computes the salary; the core records the money.*

---

## 2. Architecture

```
        ┌─────────────────── CORE (value & things) ───────────────────┐
        │  Documents API   →  Posting-rules engine  →  Pacioli ledger  │
        │  (invoice, bill,     (doc → balanced txn)    (source of truth │
        │   payment, journal,                           for money)      │
        │   stock-move)                                                 │
        │        │                                                      │
        │        └→ Inventory (stock levels, moves, moving-avg cost)    │
        │                                                               │
        │  Parties (thin)   Items (thin)   Events/outbox   Reports      │
        └──────────────┬──────────────────────────┬───────────────────┘
                       │ REST/JSON API + webhooks  │
        ┌──────────────┴──────┐          ┌─────────┴──────────┐
        │ Store / POS / shop  │          │ CRM · HR · Payroll │   (people, outside)
        └─────────────────────┘          └────────────────────┘
```

### Code layout

| File | Responsibility |
|---|---|
| `pacioli_erp/ledger.py` | Double-entry engine + ledger-cli parser/serializer (source of truth for money). |
| `pacioli_erp/storage.py` | pydal schema for the ledger (`account`, `txn`, `posting`, `chk`, `price`) + file↔db. |
| `pacioli_erp/erp.py` | **The core.** ERP schema + posting-rules engine + services. py4web-independent (every function takes `db`), so it is unit-testable with a bare `DAL`. |
| `apps/pacioli/` | Thin py4web app: `common.py` (db/auth/session/token), `models.py` (`pacioli_erp.erp.define_tables`), `controllers.py` (REST API + minimal UI), templates. |
| `pacioli_erp/editor.py` | Stand-alone single-page ledger editor (separate, file-based). |

### Keystone idea — parties & items are account namespaces

Rather than a parallel "balances" system, the core leans on pacioli's account
tree so per-party / per-item balances come from the ledger roll-up for free:

| Entity | Ledger account |
|---|---|
| Customer "Bob" | `Assets:AccountsReceivable:Bob` |
| Vendor "WoodCo" | `Liabilities:AccountsPayable:WoodCo` |
| Item `TOY-1` | `Assets:Inventory:TOY-1` |

`GET /api/accounts/Assets:AccountsReceivable` then *is* total AR; the children are
the per-customer balances.

---

## 3. Data model

Operational tables (in `pacioli_erp.erp.define_tables`) on top of the pacioli ledger tables:

```
party(id, name, kind[customer|vendor|both], currency, ar_account, ap_account, external_ref)
item(id, sku, name, uom, valuation, cost, price, tracked,
     inventory_account, income_account, cogs_account)
location(id, name)
stock(item, location, qty)                              -- operational projection
document(id, kind[invoice|bill|payment|journal], party, doc_date, status,
         currency, subtotal, tax_total, total, allocated, memo, external_id, txn)
document_line(document, item?, account?, description, qty, unit_price, tax_rate, amount)
stock_move(item, kind[receipt|issue|adjust|transfer], qty, unit_cost,
           location, to_location, document, txn, external_id)
allocation(payment, target, amount)                     -- payment ↔ invoice/bill
event(id, kind, payload, created_on)                    -- outbox for webhooks
```

**Invariants the core maintains**

1. Every posted document maps to a *balanced* pacioli transaction — pacioli
   refuses to record anything else.
2. `Σ (stock.qty × item.cost)` for a tracked item == its ledger
   `Assets:Inventory:<sku>` balance (see `GET /api/stock/reconcile`).
3. Posted documents are **immutable**; corrections are reversing entries.

### Costing

Inventory is valued with **moving-average** cost (the most common method), kept on
`item.cost` and updated on every receipt. FIFO via pacioli's `@`/`BOOK` commodity
machinery is supported by the engine and reserved as a future per-item option
(`item.valuation`).

---

## 4. Posting-rules engine (the heart)

Each document type is a template `document → [balanced postings]`:

| Document | Ledger postings | Inventory |
|---|---|---|
| **Journal** | as supplied; must net to 0 | — |
| **Customer invoice** (item line) | Dr `AR:<party>` (total); Cr `Income` (per line); Cr `Liabilities:Tax` — **and** Dr `Expenses:COGS`, Cr `Inventory:<sku>` at moving-avg cost | −qty |
| **Customer invoice** (account/service line) | Dr `AR:<party>`; Cr the line account; Cr tax | — |
| **Vendor bill** (item line) | Dr `Inventory:<sku>`; Cr `AP:<party>` (+ tax). Updates moving-avg cost | +qty |
| **Vendor bill** (account line) | Dr the expense/asset account; Cr `AP:<party>` (+ tax) | — |
| **Payment in** | Dr `Assets:Bank`; Cr `AR:<party>` | — |
| **Payment out** | Dr `AP:<party>`; Cr `Assets:Bank` | — |
| **Stock receipt** (standalone) | Dr `Inventory:<sku>`; Cr `Equity:Opening-Balances` | +qty |
| **Stock issue** (standalone) | Dr `Expenses:COGS`; Cr `Inventory:<sku>` | −qty |
| **Stock adjust** | Dr/Cr `Inventory:<sku>` vs `Expenses:Inventory-Adjustment` | ±qty |
| **Stock transfer** | none (quantity only between locations) | move |

Base chart of accounts seeded at startup: `Assets:Bank`, `Assets:Cash`,
`Assets:AccountsReceivable`, `Assets:Inventory`, `Liabilities:AccountsPayable`,
`Liabilities:Tax`, `Income:Sales`, `Expenses:COGS`, `Expenses:Inventory-Adjustment`,
`Equity:Opening-Balances`.

---

## 5. RESTful API reference

Base path: **`/pacioli/api`** (the app is mounted at `/pacioli`). All requests and
responses are JSON.

### Conventions

- **Auth.** Send `Authorization: Bearer <token>`. Tokens are rows in
  `auth_simple_token`, created by a logged-in user at `/pacioli/tokens`. In
  development mode (`PY4WEB_MODE=development`, `settings.API_REQUIRE_AUTH=False`)
  the API is open for local testing.
- **Money** is a decimal string in the document/party currency (default `USD`).
- **Idempotency.** Any creating call may include `"external_id"`. Re-sending the
  same `external_id` returns the existing record instead of creating a duplicate
  (safe for at-least-once callers / webhook retries).
- **Errors.** `4xx` with `{"error": "message"}`. `400` = bad/again unbalanced
  request, `401` = missing auth, `404` = not found.
- **Events.** Every successful write appends to the `event` outbox; poll
  `GET /api/events?since=<id>`.

### 5.1 Meta

#### `GET /api/health`
No auth. → `{"ok": true, "service": "pacioli-erp", "version", "accounts", "transactions"}`

### 5.2 Ledger & reports

#### `GET /api/accounts`
Trial balance (all non-zero accounts).
→ `{"accounts": [{"account": "Assets:Bank", "amounts": [{"commodity":"USD","value":"220.00"}]}, ...]}`

#### `GET /api/accounts/<name>`
One account's balance. `<name>` is a full colon path, e.g. `Assets:Inventory:TOY-1`.
→ `{"account": name, "amounts": [{"commodity","value"}, ...]}`

#### `GET /api/ledger/transactions?from=YYYY-MM-DD&to=YYYY-MM-DD`
Journal listing with postings.
→ `{"transactions": [{"id","date","state","info","postings":[{"account","amount","commodity"}]}]}`

#### `POST /api/ledger/journals`
Raw balanced journal entry (the seam for payroll, accruals, opening balances, …).
```json
{ "date": "2026-05-31", "memo": "Payroll run",
  "lines": [ {"account": "Expenses:Salaries", "amount": "5000"},
             {"account": "Assets:Bank",        "amount": "-5000"} ] }
```
→ `{"txn": <id>}` · `400` if lines do not net to zero.

#### `GET /api/reports/trial_balance`
→ `{"trial_balance": [ {"account","amounts"} ]}`

#### `GET /api/reports/profit_and_loss?begin=&end=`
→ `{"income":[…], "expenses":[…], "total_income", "total_expenses", "net"}`

#### `GET /api/reports/balance_sheet?date=`
→ `{"assets":[…], "liabilities":[…], "equity":[…]}`

### 5.3 Parties

#### `GET /api/parties` → `{"parties": [ {id,name,kind,currency,ar_account,ap_account,external_ref} ]}`

#### `POST /api/parties`
```json
{ "name": "WoodCo", "kind": "vendor", "currency": "USD", "external_ref": "crm-42" }
```
→ `{"party": {…}}` (AR/AP control accounts are auto-created).

#### `GET /api/parties/<id>`
→ party fields + `"balance": {"receivable":[…], "payable":[…]}`

#### `PUT /api/parties/<id>`  (update `name|kind|currency|external_ref`) → updated party.

### 5.4 Items & stock

#### `GET /api/items` → `{"items": [ {id,sku,name,cost,price,tracked,inventory_account,income_account,cogs_account} ]}`

#### `POST /api/items`
```json
{ "sku": "TOY-1", "name": "Wooden toy", "price": "20", "cost": "0",
  "tracked": true, "income_account": null, "cogs_account": null }
```
→ `{"item": {…}}` (inventory/income/COGS accounts auto-created).

#### `GET /api/items/<id>` → item fields + `"qty"` on hand.
#### `PUT /api/items/<id>` (update `name|price|uom|tracked`) → updated item + qty.

#### `GET /api/stock`
→ `{"stock": [ {id,sku,name,qty,cost,value} ]}`

#### `POST /api/stock/moves`
```json
{ "item": 3, "kind": "receipt", "qty": 100, "unit_cost": "5.00",
  "location": null, "to_location": null, "external_id": "po-9-line-1" }
```
`kind` ∈ `receipt | issue | adjust | transfer`. → `{"move": <id>}`.

#### `GET /api/stock/reconcile`
→ `{"ok": bool, "items": [ {sku, operational_value, ledger_value, match} ]}`

### 5.5 Invoices (AR) and bills (AP)

#### `POST /api/invoices`
```json
{ "party": 2, "date": "2026-05-31", "memo": "Order #1001",
  "external_id": "shop-order-1001",
  "lines": [ {"item": 3, "qty": 10, "unit_price": "20.00", "tax_rate": "0.10"},
             {"account": "Income:Sales", "description": "gift wrap", "qty": 1, "unit_price": "2.00"} ] }
```
Posts revenue + tax + AR, and (for tracked item lines) issues stock + books COGS.
→ `{"invoice": {id,total,…}}`.

#### `GET /api/invoices` → `{"invoices":[…]}` · `GET /api/invoices/<id>` → invoice + `lines`.

#### `POST /api/bills`  — same body shape; item lines receive stock and update
moving-avg cost, all lines Cr `AP:<party>`. → `{"bill": {…}}`.
#### `GET /api/bills` / `GET /api/bills/<id>` as for invoices.

### 5.6 Payments

#### `POST /api/payments`
```json
{ "party": 2, "amount": "220.00", "direction": "in", "account": "Assets:Bank",
  "date": "2026-05-31", "external_id": "stripe-ch_123",
  "allocations": [ {"document": 7, "amount": "220.00"} ] }
```
`direction` ∈ `in` (customer pays us → Dr bank / Cr AR) | `out` (we pay vendor →
Dr AP / Cr bank). `allocations` match the payment to invoices/bills.
→ `{"payment": {…}}`.

#### `GET /api/payments` → `{"payments":[…]}`.

### 5.7 Events

#### `GET /api/events?since=<id>`
→ `{"events": [ {id, kind, payload, created_on} ]}`.
Kinds: `invoice.posted`, `bill.posted`, `payment.received`, `payment.sent`,
`stock.moved`.

### 5.8 Tags & search

Every system table (`account`, `txn`, `posting`, `chk`, `price`, `party`,
`item`, `location`, `stock`, `document`, `document_line`, `allocation`, `event`)
is taggable via a pydal `Tags` companion table, and every record is searchable by
**id**, by its **text identifier** (account/location `name`, item `sku`, party
`name`, document `external_id`, …), and by **tags**. Create calls
(`/parties`, `/items`, `/invoices`, `/bills`, `/payments`) accept an optional
`"tags": [...]`, and those records echo their `tags` in responses.

#### `GET /api/search/<table>?q=<text>&tag=<a>&tag=<b>&mode=and|or`
Search a table by text-identifier substring and/or tags (`mode` combines tags,
default `and`).
→ `{"table","count","results":[{"id","label","tags":[...]}]}` · `404` on unknown table.

#### `GET /api/tags/<table>/<id>` → `{"table","id","tags":[...]}`
#### `POST /api/tags/<table>/<id>`  body `{"add":[...], "remove":[...]}`
Add/remove tags on a record. → the updated `{"tags":[...]}`.

---

## 6. Worked example — the store the developer builds

A storefront, on a sale, makes three independent idempotent calls:

```
POST /pacioli/api/bills      { party: vendor, lines:[{item, qty:100, unit_price:5}] }   # stock in
POST /pacioli/api/invoices   { party: customer, lines:[{item, qty:10, unit_price:20, tax_rate:0.10}],
                           external_id:"order-1001" }                               # sale
POST /pacioli/api/payments   { party: customer, amount:220, direction:"in",
                           allocations:[{document: <invoice id>, amount:220}],
                           external_id:"stripe-ch_..." }                            # payment
```

Result, all reconciling in the ledger: Inventory 500→450, COGS 50, Revenue 200,
Tax 20, AR 220→0, Bank 220. Gross profit (`GET /reports/profit_and_loss`) = 150.
(End-to-end test: `pacioli_erp/tests/test_app_wsgi.py`; engine tests: `pacioli_erp/tests/test_erp_core.py`.)

---

## 7. Running it

```bash
uv run --with py4web --with fpdf2 py4web run apps -A pacioli --dev      # http://127.0.0.1:8000/pacioli/
```

(`uv` pulls py4web — and pydal, which ships with it — into an ephemeral
environment; the local `pacioli`/`erp_core` modules resolve via the project-root
path inserted in `apps/pacioli/common.py`. A classic `pip install pydal py4web` in a
venv works too.)

Back office (read-only dashboards + token management) at `/pacioli/`; REST API at
`/pacioli/api`. Tests: `.venv/bin/python -m unittest discover -s pacioli_erp/tests -t .`.

---

## 8. Out of scope (deliberately) & roadmap

**Not in the core:** CRM, HR/payroll calculation, the storefront and its
end-users, tax *determination* (the caller supplies `tax_rate`; the core only
records tax), pricing/promotion logic.

**Roadmap toward "credibly an ERP":** multi-currency & FX; period close +
immutable periods; FIFO/standard costing options; goods-received-not-invoiced
(GRIR) clearing to fully separate receipt from bill; approval workflows; webhook
delivery worker draining the `event` outbox; multi-entity consolidation. The line
where "ERP-lite" becomes "ERP" is when both order-to-cash **and** procure-to-pay
close the loop to the ledger — which this core already does in its minimal form.
