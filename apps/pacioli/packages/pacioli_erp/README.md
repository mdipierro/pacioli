# pacioli_erp

A small, dependency-light **double-entry accounting engine** (named after Luca
Pacioli) with a [ledger-cli](https://ledger-cli.org/)-compatible file format, an
optional relational backend, a single-page file editor, and an API-first **ERP
core** (ledger + inventory + invoicing) built on top of it.

The package is layered so you only pay for what you import:

| Module | Needs | What it gives you |
|---|---|---|
| `pacioli_erp` / `pacioli_erp.ledger` | stdlib only | the double-entry engine + ledger-cli parser/serializer |
| `pacioli_erp.editor` | stdlib only | a single-page web editor for a ledger file |
| `pacioli_erp.storage` | `pydal` | store/read the ledger in any pydal database; file↔db |
| `pacioli_erp.erp` | `pydal` | the ERP posting engine: parties, items, stock, invoices, bills, payments |

`import pacioli_erp` never imports pydal, so the core stays usable without it.

---

## Install

```bash
pip install .                 # core only
pip install ".[db]"           # + pydal backend
pip install ".[web]"          # + py4web (for the ERP app under apps/pacioli)
pip install ".[db,web,dev]"   # everything
```

## Quick start

```python
from pacioli_erp import Pacioli, Transaction, Posting, Amount

p = Pacioli()
p.add_account("Assets:Cash", "De", "USD")
p.add_account("Equity", "Cr", "USD")
p.ledger.append(Transaction("2008-10-20", "opening", postings=[
    Posting("Assets:Cash", amount=Amount(100, "USD")),
    Posting("Equity"),                       # auto-balanced
]))
p.finalize(); p.run()
assert p.accounts["Assets"].wallet["USD"] == 100
assert p.accounts["Equity"].wallet["USD"] == -100
```

Command line (uses the bundled `examples/demo.ledger` by default):

```bash
python -m pacioli_erp -i my.ledger            # report + HTML/Latex output
python -m pacioli_erp -i my.ledger --to-db sqlite://ledger.sqlite
python -m pacioli_erp --db sqlite://ledger.sqlite
python -m pacioli_erp.editor my.ledger --port 8080
```

---

## Public API

### `pacioli_erp` (top level)

Re-exported from `pacioli_erp.ledger` (stdlib-only):

| Name | Description |
|---|---|
| `Pacioli` | the ledger engine (accounts, transactions, run, reports) |
| `Amount` | a `(value, commodity)` pair |
| `Wallet` | a multi-commodity balance |
| `Transaction`, `Posting`, `Check` | ledger items |
| `DataSource`, `FileSource` | pluggable load/save backends |
| `parse_amount(text)`, `parse_date(text)` | ledger-cli parsers |

### `pacioli_erp.ledger`

The engine and the ledger-cli parser/serializer.

- **`Pacioli(strict=False)`** — the main object.
  - `load(source)` — load from a file path, an open stream, or a `DataSource`.
  - `run()` — replay the ledger, compute balances, capital gains (FIFO), checks.
  - `report()` — print a trial balance to stdout.
  - `save(target)` — write the ledger to a path, stream, or `DataSource`.
  - `trial_balance()` — `{account_name: Wallet}`.
  - `dump_html(folder)`, `dump_latex(filename)` — reports.
  - `add_account(name, type=None, asset=None)`, `open_account(...)`, `close_account(...)`.
  - `finalize()` — sort the ledger and compute leaf accounts (call after building it by hand).
  - Attributes: `accounts` (dict), `ledger` (list), `begin_date`, `end_date`,
    `begin_accounts` / `end_accounts` / `diff_accounts` (after `run()`).
- **Value objects:** `Amount`, `Wallet`, `Account`, `Posting`, `Transaction`, `Check`.
- **`FileSource(path_or_stream)`** — a `DataSource` for a flat ledger file.
- **Functions:** `parse_amount`, `parse_date`, `main` (the CLI).

Supported ledger-cli syntax: `/ - .` dates and `=` effective dates; `*`/`!`
states, `(code)`, payees; `; # % | *` comments and `:tag:` / `key: value`
metadata; account names with spaces; commodity prefix/suffix amounts with
thousands separators; `@`/`@@` costs; `= ` balance assertions; virtual postings
`(..)`/`[..]`; the `account`, `commodity`, `include`, `P`, `year`/`Y`, `D`,
`bucket`/`A`, `apply tag`, `comment` directives. Pacioli extensions:
`open`/`close`, `pad`/`balance` checks, `pushtags`/`poptags`, and `BOOK` postings
for FIFO capital gains.

### `pacioli_erp.storage` (requires `pydal`)

Relational backend mirroring the file format (tables `account`, `txn`,
`posting`, `chk`, `price`).

- `connect(uri="sqlite://pacioli.sqlite", folder=None, **kw)` → a DAL with the tables defined.
- `define_tables(db)` — define the ledger schema on an existing DAL.
- `DBSource(db)` — a `DataSource` so `Pacioli.load(DBSource(db))` / `.save(...)` work.
- `file_to_db(filename, db)` / `db_to_file(db, filename)` — convert both ways.
- `pacioli_to_db(p, db)` / `db_to_pacioli(db, p=None)` — in-memory ↔ db.

### `pacioli_erp.erp` (requires `pydal`)

The ERP posting engine. Every document becomes a *balanced* ledger transaction;
parties and items map onto account namespaces (`Assets:AccountsReceivable:<party>`,
`Assets:Inventory:<sku>`). See `../PLAN.md` for the full REST API exposed by the
py4web app in `apps/pacioli`.

- **Setup:** `setup(db)` (= `define_tables(db)` + `init_accounts(db)`).
- **Masters:** `create_party(db, name, kind=...)`, `create_item(db, sku, ...)`.
- **Documents:** `create_invoice(db, party, lines, ...)`,
  `create_bill(db, party, lines, ...)`,
  `create_payment(db, party, amount, direction="in"|"out", allocations=...)`,
  `create_stock_move(db, item, kind, qty, ...)`,
  `post_journal(db, lines, date=, memo=)`.
- **Reports:** `trial_balance(db)`, `account_balance(db, name)`,
  `party_balance(db, party)`, `profit_and_loss(db, begin=, end=)`,
  `balance_sheet(db, date=)`, `stock_levels(db)`, `stock_on_hand(db, item)`,
  `reconcile_inventory(db)`, `build_pacioli(db)`.
- **Errors / constants:** `ErpError`, `DEFAULT_CURRENCY`, `SYSTEM_TABLES`,
  `LABEL_FIELD`, and the `ACC_*` account names (e.g. `ACC_BANK`, `ACC_TAX`, `ACC_COGS`).
- **Tags & search** (every system table is taggable via pydal `Tags`):
  `add_tags(db, table, id, tags)`, `remove_tags(...)`, `get_tags(db, table, id)`,
  `lookup(db, table, ref)` (resolve by id or unique text identifier),
  `search(db, table, text=, tags=, tag_mode=)`, `record_summary(db, table, row)`.
  The `create_*` functions also accept an optional `tags=` list.

Idempotency: every creating call accepts `external_id`; re-sending the same id
returns the existing record. Each write appends to the `event` outbox.

### `pacioli_erp.editor` (stdlib only)

A single-page web editor for a ledger **file** with live validation + trial balance.

- `serve(filename, port=8080, host="127.0.0.1")` — run the editor.
- `analyze(text)` → `{"ok", "error", "balance"}` — parse/run a ledger string.
- `main()` — the CLI (`python -m pacioli_erp.editor`).

---

## Tests

```bash
python -m unittest discover -s pacioli_erp/tests -t .
```

81 tests cover the parser, the engine (balancing, FIFO capital gains), the
serializer round-trip, the pydal backend, the editor, the ERP posting engine,
the py4web app end-to-end, and ledger-cli compatibility against authoritative
journals harvested from the ledger-cli test suite. The pydal/py4web tests skip
automatically when those packages are absent.

## License

BSD-3-Clause.
