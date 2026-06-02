#!/usr/bin/env python
"""
erp_core -- the "value & things" core on top of the pacioli ledger.
License: BSD <http://opensource.org/licenses/BSD-3-Clause>

This module is deliberately independent of py4web: every function takes a pydal
``db`` as its first argument, so it can be unit-tested with a bare ``DAL(...)``
and reused unchanged by the py4web controllers.

The design (see PLAN.md):

  *  A *document* (invoice, bill, payment, stock move, journal) is a request to
     move value.  The core validates it, moves stock, emits a *balanced*
     pacioli transaction and records an event.  Pacioli guarantees the
     "balanced" part.
  *  Parties and items are thin metadata mapped onto pacioli account
     namespaces (Assets:AccountsReceivable:<party>, Assets:Inventory:<sku>, ...)
     so per-party / per-item balances come from the ledger's tree roll-up for
     free.
  *  The ledger (pacioli) is the single source of truth for money; the ``stock``
     table is an operational projection that must reconcile to the inventory
     asset accounts.

Inventory is valued with a moving-average cost (the most common method); the
item's ``cost`` field is maintained on every receipt.  FIFO via pacioli's
``@``/``BOOK`` commodity machinery is possible but intentionally not the default
(see PLAN.md, "Costing").
"""

import datetime
import decimal

from pydal.tools.tags import Tags

from . import ledger as pacioli
from . import storage as pacioli_db
from .ledger import Amount, Pacioli

R = decimal.Decimal
ZERO = R("0.00")
DEFAULT_CURRENCY = "USD"

# every table the system creates is taggable and searchable.  TAG_NAME is the
# pydal Tags "namespace": each table T gets a companion table T_tag_tags.
TAG_NAME = "tags"
SYSTEM_TABLES = [
    "account", "txn", "posting", "chk", "price",
    "party", "item", "location", "stock",
    "document", "document_line", "allocation", "event",
]
# the unique-ish human text identifier per table (for name search / lookup)
LABEL_FIELD = {
    "account": "name", "party": "name", "item": "sku", "location": "name",
    "document": "external_id", "price": "commodity", "txn": "info",
}

# base accounts that always exist
ACC_AR = "Assets:AccountsReceivable"
ACC_AP = "Liabilities:AccountsPayable"
ACC_INVENTORY = "Assets:Inventory"
ACC_BANK = "Assets:Bank"
ACC_CASH = "Assets:Cash"
ACC_TAX = "Liabilities:Tax"
ACC_INCOME = "Income:Sales"
ACC_COGS = "Expenses:COGS"
ACC_INV_ADJUST = "Expenses:Inventory-Adjustment"
ACC_OPENING = "Equity:Opening-Balances"

BASE_ACCOUNTS = [
    ACC_AR,
    ACC_AP,
    ACC_INVENTORY,
    ACC_BANK,
    ACC_CASH,
    ACC_TAX,
    ACC_INCOME,
    ACC_COGS,
    ACC_INV_ADJUST,
    ACC_OPENING,
]


class ErpError(Exception):
    """Raised on any business-rule violation (unbalanced doc, bad ref, ...)."""


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def define_tables(db, auth_signature=None):
    """Define the pacioli ledger tables and the ERP operational tables.

    ``auth_signature`` (optional) is a pydal signature added to operational
    tables when running inside py4web.  Safe to call more than once."""
    from pydal import Field

    pacioli_db.define_tables(db)

    extra = [auth_signature] if auth_signature else []

    db.define_table(
        "party",
        Field("name", "string", length=255, notnull=True),
        Field("kind", "string", length=16, default="customer"),  # customer|vendor|both
        Field("currency", "string", length=8, default=DEFAULT_CURRENCY),
        Field("ar_account", "string", length=512),
        Field("ap_account", "string", length=512),
        Field("external_ref", "string", length=255),
        Field("email", "string", length=255),
        Field("phone", "string", length=64),
        Field("address", "text"),
        *extra,
        redefine=True,
    )
    db.define_table(
        "item",
        Field("sku", "string", length=64, notnull=True, unique=True),
        Field("name", "string", length=255),
        Field("uom", "string", length=16, default="unit"),
        Field("valuation", "string", length=16, default="average"),  # average|fifo|std
        Field("cost", "decimal(20,6)", default=ZERO),  # current moving-avg unit cost
        Field("price", "decimal(20,6)", default=ZERO),  # default sales price
        Field("inventory_account", "string", length=512),
        Field("income_account", "string", length=512),
        Field("cogs_account", "string", length=512),
        Field("tracked", "boolean", default=True),  # False => service item
        *extra,
        redefine=True,
    )
    db.define_table(
        "location",
        Field("name", "string", length=128, notnull=True, unique=True),
        *extra,
        redefine=True,
    )
    db.define_table(
        "stock",
        Field("item", "reference item", notnull=True),
        Field("location", "reference location"),
        Field("qty", "decimal(20,6)", default=ZERO),
        redefine=True,
    )
    db.define_table(
        "document",
        Field("kind", "string", length=16, notnull=True),  # invoice|bill|payment|journal
        Field("party", "reference party"),
        # name + address snapshotted from the party at creation time, so the
        # document stays accurate even if the party record is later edited.
        Field("party_name", "string", length=255),
        Field("party_address", "text"),
        Field("doc_date", "date"),
        Field("status", "string", length=12, default="posted"),  # draft|posted|void
        Field("currency", "string", length=8, default=DEFAULT_CURRENCY),
        Field("subtotal", "decimal(20,6)", default=ZERO),
        Field("tax_total", "decimal(20,6)", default=ZERO),
        Field("total", "decimal(20,6)", default=ZERO),
        Field("allocated", "decimal(20,6)", default=ZERO),  # for invoices/bills
        Field("memo", "text"),
        Field("external_id", "string", length=128),
        Field("txn", "reference txn"),  # the ledger transaction it posted
        Field("created_on", "datetime"),
        *extra,
        redefine=True,
    )
    db.define_table(
        "document_line",
        Field("document", "reference document", notnull=True),
        Field("item", "reference item"),
        Field("account", "string", length=512),  # used when no item (service/expense)
        Field("description", "string", length=255),
        Field("qty", "decimal(20,6)", default=R("1")),
        Field("unit_price", "decimal(20,6)", default=ZERO),
        Field("tax_rate", "decimal(8,4)", default=ZERO),  # e.g. 0.08 for 8%
        Field("amount", "decimal(20,6)", default=ZERO),
        redefine=True,
    )
    db.define_table(
        "stock_move",
        Field("item", "reference item", notnull=True),
        Field("kind", "string", length=16),  # receipt|issue|adjust|transfer
        Field("qty", "decimal(20,6)"),
        Field("unit_cost", "decimal(20,6)"),
        Field("location", "reference location"),
        Field("to_location", "reference location"),
        Field("document", "reference document"),
        Field("txn", "reference txn"),
        Field("external_id", "string", length=128),
        Field("created_on", "datetime"),
        redefine=True,
    )
    db.define_table(
        "allocation",
        Field("payment", "reference document", notnull=True),
        Field("target", "reference document", notnull=True),
        Field("amount", "decimal(20,6)"),
        redefine=True,
    )
    db.define_table(
        "event",
        Field("kind", "string", length=48),  # invoice.posted, payment.received, ...
        Field("payload", "json", default=lambda: {}),
        Field("created_on", "datetime"),
        redefine=True,
    )
    _init_tags(db)
    return db


# ---------------------------------------------------------------------------
# tagging & search (pydal Tags) — every system table is taggable and records
# are searchable by id, by their text identifier, and by tags.
# ---------------------------------------------------------------------------


def _init_tags(db):
    """Attach a pydal Tags handler to every system table (companion table
    ``<table>_tag_tags``).  Idempotent and cached on the db."""
    if getattr(db, "_erp_tags", None) is None:
        db._erp_tags = {}
    for t in SYSTEM_TABLES:
        if t in db.tables and t not in db._erp_tags:
            companion = "%s_tag_%s" % (t, TAG_NAME)
            existing = db[companion] if companion in db.tables else None
            db._erp_tags[t] = Tags(db[t], TAG_NAME, tag_table=existing)
    return db._erp_tags


def _tags(db, table):
    if getattr(db, "_erp_tags", None) is None or table not in db._erp_tags:
        _init_tags(db)
    if table not in db._erp_tags:
        raise ErpError("Table %r is not taggable" % table)
    return db._erp_tags[table]


def add_tags(db, table, record_id, tags, commit=True):
    """Tag a record in any system table."""
    _tags(db, table).add(record_id, tags if isinstance(tags, list) else [tags])
    if commit:
        db.commit()


def remove_tags(db, table, record_id, tags, commit=True):
    _tags(db, table).remove(record_id, tags if isinstance(tags, list) else [tags])
    if commit:
        db.commit()


def get_tags(db, table, record_id):
    """Tags currently on a record."""
    if table not in SYSTEM_TABLES:
        return []
    return _tags(db, table).get(record_id)


def lookup(db, table, ref):
    """Resolve a single record by integer id or by its unique text identifier
    (account/location name, item sku, party name, document external_id, ...)."""
    tbl = db[table]
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        return tbl[int(ref)]
    field = LABEL_FIELD.get(table)
    if field and field in tbl.fields:
        return db(tbl[field] == ref).select().first()
    return None


def search(db, table, text=None, tags=None, tag_mode="and", limit=200):
    """Search a system table by text identifier (substring, case-insensitive)
    and/or by tags.  Returns pydal Rows."""
    if table not in SYSTEM_TABLES:
        raise ErpError("Unknown table %r" % table)
    tbl = db[table]
    query = tbl.id > 0
    if tags:
        if not isinstance(tags, list):
            tags = [tags]
        query &= _tags(db, table).find(tags, mode=tag_mode)
    if text:
        field = LABEL_FIELD.get(table)
        if field and field in tbl.fields:
            query &= tbl[field].contains(text)
    return db(query).select(orderby=tbl.id, limitby=(0, limit))


def record_summary(db, table, row):
    """A compact, JSON-able view of a record: id, text label, and its tags."""
    field = LABEL_FIELD.get(table)
    label = row[field] if (field and field in row) else None
    return {"id": row.id, "label": label, "tags": get_tags(db, table, row.id)}


def init_accounts(db):
    """Make sure the base chart of accounts exists in the ledger."""
    existing = set(row.name for row in db(db.account).select(db.account.name))
    for name in BASE_ACCOUNTS:
        for sub in _ancestors(name):
            if sub not in existing:
                db.account.insert(name=sub, open_date=pacioli.BEGIN_TIME)
                existing.add(sub)
    db.commit()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ancestors(name):
    items = name.split(":")
    return [":".join(items[: k + 1]) for k in range(len(items))]


def _slug(text):
    out = "".join(c if (c.isalnum()) else "-" for c in str(text))
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def _dec(value):
    if value is None:
        return ZERO
    return R(str(value))


def _ensure_account(db, name, open_date=None):
    """Create the account (and ancestors) in the ledger if missing."""
    open_date = open_date or pacioli.BEGIN_TIME
    existing = set(
        row.name
        for row in db(db.account.name.belongs(_ancestors(name))).select(db.account.name)
    )
    for sub in _ancestors(name):
        if sub not in existing:
            db.account.insert(name=sub, open_date=open_date)
            existing.add(sub)


def _today():
    return datetime.date.today()


def _now():
    return datetime.datetime.now()


def _emit(db, kind, payload):
    db.event.insert(kind=kind, payload=payload, created_on=_now())


def party_ar_account(party):
    return party.ar_account or "%s:%s" % (ACC_AR, _slug(party.name))


def party_ap_account(party):
    return party.ap_account or "%s:%s" % (ACC_AP, _slug(party.name))


def item_inventory_account(item):
    return item.inventory_account or "%s:%s" % (ACC_INVENTORY, _slug(item.sku))


def item_income_account(item):
    return item.income_account or ACC_INCOME


def item_cogs_account(item):
    return item.cogs_account or ACC_COGS


# ---------------------------------------------------------------------------
# ledger posting primitive
# ---------------------------------------------------------------------------


def post_journal(db, lines, date=None, memo="", currency=DEFAULT_CURRENCY, commit=True):
    """Write a balanced ledger transaction directly.

    ``lines`` is a list of dicts: ``{"account": str, "amount": number}`` (a
    signed amount in ``currency``; debits positive, credits negative).  Raises
    :class:`ErpError` if the postings do not net to zero.  Returns the new
    ``txn.id``.
    """
    if date is None:
        date = _today()
    total = sum(_dec(line["amount"]) for line in lines)
    if total != ZERO:
        raise ErpError("Unbalanced journal: nets to %s, not zero" % total)
    if not lines:
        raise ErpError("Empty journal")

    txn_id = db.txn.insert(
        tdate=date, state="*", info=memo or "journal", seq=None
    )
    for order, line in enumerate(lines):
        _ensure_account(db, line["account"], date)
        db.posting.insert(
            txn=txn_id,
            ordering=order,
            account=line["account"],
            amount_value=_dec(line["amount"]),
            amount_commodity=currency,
            comment=line.get("memo"),
        )
    if commit:
        db.commit()
    return txn_id


# ---------------------------------------------------------------------------
# parties & items
# ---------------------------------------------------------------------------


def create_party(
    db, name, kind="customer", currency=DEFAULT_CURRENCY, external_ref=None,
    email=None, phone=None, address=None, tags=None
):
    party_id = db.party.insert(
        name=name, kind=kind, currency=currency, external_ref=external_ref,
        email=email, phone=phone, address=address
    )
    party = db.party[party_id]
    ar, ap = party_ar_account(party), party_ap_account(party)
    party.update_record(ar_account=ar, ap_account=ap)
    _ensure_account(db, ar)
    _ensure_account(db, ap)
    if tags:
        add_tags(db, "party", party_id, tags, commit=False)
    db.commit()
    return party_id


def create_item(
    db,
    sku,
    name=None,
    cost=ZERO,
    price=ZERO,
    tracked=True,
    income_account=None,
    cogs_account=None,
    inventory_account=None,
    tags=None,
):
    item_id = db.item.insert(
        sku=sku,
        name=name or sku,
        cost=_dec(cost),
        price=_dec(price),
        tracked=tracked,
        income_account=income_account,
        cogs_account=cogs_account,
        inventory_account=inventory_account,
    )
    item = db.item[item_id]
    if tracked:
        _ensure_account(db, item_inventory_account(item))
    _ensure_account(db, item_income_account(item))
    _ensure_account(db, item_cogs_account(item))
    if tags:
        add_tags(db, "item", item_id, tags, commit=False)
    db.commit()
    return item_id


def _get_stock_row(db, item_id, location_id=None):
    q = db.stock.item == item_id
    q &= db.stock.location == location_id
    row = db(q).select().first()
    if not row:
        rid = db.stock.insert(item=item_id, location=location_id, qty=ZERO)
        row = db.stock[rid]
    return row


def stock_on_hand(db, item_id):
    total = db(db.stock.item == item_id).select(
        db.stock.qty.sum()
    ).first()[db.stock.qty.sum()]
    return _dec(total)


# ---------------------------------------------------------------------------
# inventory moves
# ---------------------------------------------------------------------------


def create_stock_move(
    db,
    item_id,
    kind,
    qty,
    unit_cost=None,
    location_id=None,
    to_location_id=None,
    document_id=None,
    date=None,
    external_id=None,
    commit=True,
):
    """Move stock and post the matching ledger entry.

    kind:
      * ``receipt``  -- goods in; Dr Inventory / Cr Equity:Opening (standalone)
                        or, when ``document_id`` is a bill, the bill posts AP.
      * ``issue``    -- goods out at moving-avg cost; Dr COGS / Cr Inventory.
      * ``adjust``   -- Dr/Cr Inventory vs Expenses:Inventory-Adjustment.
      * ``transfer`` -- quantity only between locations, no ledger impact.
    """
    if external_id:
        existing = db(db.stock_move.external_id == external_id).select().first()
        if existing:
            return existing.id
    item = db.item[item_id]
    if not item:
        raise ErpError("Unknown item %r" % item_id)
    qty = _dec(qty)
    date = date or _today()
    inv = item_inventory_account(item)
    txn_id = None

    if kind == "transfer":
        src = _get_stock_row(db, item_id, location_id)
        dst = _get_stock_row(db, item_id, to_location_id)
        src.update_record(qty=_dec(src.qty) - qty)
        dst.update_record(qty=_dec(dst.qty) + qty)
    elif kind == "receipt":
        unit_cost = _dec(unit_cost if unit_cost is not None else item.cost)
        _update_moving_average(db, item, qty, unit_cost)
        row = _get_stock_row(db, item_id, location_id)
        row.update_record(qty=_dec(row.qty) + qty)
        # standalone receipt is an opening/uninvoiced increase in inventory
        if document_id is None:
            value = qty * unit_cost
            if value:
                txn_id = post_journal(
                    db,
                    [
                        {"account": inv, "amount": value},
                        {"account": ACC_OPENING, "amount": -value},
                    ],
                    date=date,
                    memo="Stock receipt %s" % item.sku,
                    commit=False,
                )
    elif kind == "issue":
        unit_cost = _dec(unit_cost if unit_cost is not None else item.cost)
        row = _get_stock_row(db, item_id, location_id)
        row.update_record(qty=_dec(row.qty) - qty)
        value = qty * unit_cost
        if value and document_id is None:
            txn_id = post_journal(
                db,
                [
                    {"account": item_cogs_account(item), "amount": value},
                    {"account": inv, "amount": -value},
                ],
                date=date,
                memo="Stock issue %s" % item.sku,
                commit=False,
            )
    elif kind == "adjust":
        row = _get_stock_row(db, item_id, location_id)
        row.update_record(qty=_dec(row.qty) + qty)
        unit_cost = _dec(unit_cost if unit_cost is not None else item.cost)
        value = qty * unit_cost
        if value:
            txn_id = post_journal(
                db,
                [
                    {"account": inv, "amount": value},
                    {"account": ACC_INV_ADJUST, "amount": -value},
                ],
                date=date,
                memo="Stock adjust %s" % item.sku,
                commit=False,
            )
    else:
        raise ErpError("Unknown stock move kind %r" % kind)

    move_id = db.stock_move.insert(
        item=item_id,
        kind=kind,
        qty=qty,
        unit_cost=unit_cost if kind != "transfer" else None,
        location=location_id,
        to_location=to_location_id,
        document=document_id,
        txn=txn_id,
        external_id=external_id,
        created_on=_now(),
    )
    _emit(db, "stock.moved", {"item": item.sku, "kind": kind, "qty": str(qty)})
    if commit:
        db.commit()
    return move_id


def _update_moving_average(db, item, qty_in, unit_cost):
    old_qty = stock_on_hand(db, item.id)
    old_cost = _dec(item.cost)
    new_qty = old_qty + qty_in
    if new_qty > 0:
        new_cost = (old_qty * old_cost + qty_in * unit_cost) / new_qty
        item.update_record(cost=new_cost)


# ---------------------------------------------------------------------------
# invoices (AR) and bills (AP)
# ---------------------------------------------------------------------------


def _line_amounts(lines):
    out = []
    subtotal = ZERO
    tax_total = ZERO
    for ln in lines:
        qty = _dec(ln.get("qty", 1))
        price = _dec(ln.get("unit_price", 0))
        amount = qty * price
        rate = _dec(ln.get("tax_rate", 0))
        tax = (amount * rate).quantize(R("0.000001"))
        out.append(dict(ln, qty=qty, unit_price=price, amount=amount, tax=tax))
        subtotal += amount
        tax_total += tax
    return out, subtotal, tax_total


def create_invoice(db, party_id, lines, date=None, memo="", external_id=None, tags=None, commit=True):
    """Create and post a customer invoice (order-to-cash, AR side).

    Each line is ``{item|account, description?, qty, unit_price, tax_rate?}``.
    Item lines also issue stock and book COGS at moving-average cost.
    """
    if external_id:
        existing = db(
            (db.document.external_id == external_id) & (db.document.kind == "invoice")
        ).select().first()
        if existing:
            return existing.id
    party = db.party[party_id]
    if not party:
        raise ErpError("Unknown party %r" % party_id)
    date = date or _today()
    priced, subtotal, tax_total = _line_amounts(lines)
    total = subtotal + tax_total

    doc_id = db.document.insert(
        kind="invoice",
        party=party_id,
        party_name=party.name,
        party_address=party.address,
        doc_date=date,
        status="posted",
        subtotal=subtotal,
        tax_total=tax_total,
        total=total,
        memo=memo,
        external_id=external_id,
        created_on=_now(),
    )

    postings = []
    ar = party_ar_account(party)
    postings.append({"account": ar, "amount": total})
    revenue_by_account = {}
    for ln in priced:
        if ln.get("item"):
            item = db.item[ln["item"]]
            inc = item_income_account(item)
        else:
            inc = ln.get("account") or ACC_INCOME
        revenue_by_account[inc] = revenue_by_account.get(inc, ZERO) + ln["amount"]
        db.document_line.insert(
            document=doc_id,
            item=ln.get("item"),
            account=ln.get("account"),
            description=ln.get("description"),
            qty=ln["qty"],
            unit_price=ln["unit_price"],
            tax_rate=_dec(ln.get("tax_rate", 0)),
            amount=ln["amount"],
        )
    for acc, amt in revenue_by_account.items():
        postings.append({"account": acc, "amount": -amt})
    if tax_total:
        postings.append({"account": ACC_TAX, "amount": -tax_total})

    txn_id = post_journal(
        db, postings, date=date, memo=memo or "Invoice #%s" % doc_id, commit=False
    )

    # cost of goods sold for tracked item lines
    for ln in priced:
        if ln.get("item"):
            item = db.item[ln["item"]]
            if item.tracked:
                cost = _dec(item.cost) * ln["qty"]
                row = _get_stock_row(db, item.id, None)
                row.update_record(qty=_dec(row.qty) - ln["qty"])
                if cost:
                    cogs_txn = post_journal(
                        db,
                        [
                            {"account": item_cogs_account(item), "amount": cost},
                            {"account": item_inventory_account(item), "amount": -cost},
                        ],
                        date=date,
                        memo="COGS invoice #%s" % doc_id,
                        commit=False,
                    )
                    db.stock_move.insert(
                        item=item.id,
                        kind="issue",
                        qty=ln["qty"],
                        unit_cost=item.cost,
                        document=doc_id,
                        txn=cogs_txn,
                        created_on=_now(),
                    )

    db.document[doc_id].update_record(txn=txn_id)
    if tags:
        add_tags(db, "document", doc_id, tags, commit=False)
    _emit(db, "invoice.posted", {"id": doc_id, "party": party.name, "total": str(total)})
    if commit:
        db.commit()
    return doc_id


def create_bill(db, party_id, lines, date=None, memo="", external_id=None, tags=None, commit=True):
    """Create and post a vendor bill (procure-to-pay, AP side).

    Item lines increase stock and Dr Inventory (updating moving-avg cost);
    account lines Dr that expense/asset account.  Both Cr AP.
    """
    if external_id:
        existing = db(
            (db.document.external_id == external_id) & (db.document.kind == "bill")
        ).select().first()
        if existing:
            return existing.id
    party = db.party[party_id]
    if not party:
        raise ErpError("Unknown party %r" % party_id)
    date = date or _today()
    priced, subtotal, tax_total = _line_amounts(lines)
    total = subtotal + tax_total

    doc_id = db.document.insert(
        kind="bill",
        party=party_id,
        party_name=party.name,
        party_address=party.address,
        doc_date=date,
        status="posted",
        subtotal=subtotal,
        tax_total=tax_total,
        total=total,
        memo=memo,
        external_id=external_id,
        created_on=_now(),
    )

    postings = []
    ap = party_ap_account(party)
    debit_by_account = {}
    for ln in priced:
        if ln.get("item"):
            item = db.item[ln["item"]]
            acc = item_inventory_account(item) if item.tracked else (
                ln.get("account") or ACC_COGS
            )
        else:
            acc = ln.get("account") or "Expenses:Misc"
        debit_by_account[acc] = debit_by_account.get(acc, ZERO) + ln["amount"]
        db.document_line.insert(
            document=doc_id,
            item=ln.get("item"),
            account=ln.get("account"),
            description=ln.get("description"),
            qty=ln["qty"],
            unit_price=ln["unit_price"],
            tax_rate=_dec(ln.get("tax_rate", 0)),
            amount=ln["amount"],
        )
    for acc, amt in debit_by_account.items():
        postings.append({"account": acc, "amount": amt})
    if tax_total:
        postings.append({"account": ACC_TAX, "amount": tax_total})
    postings.append({"account": ap, "amount": -total})

    txn_id = post_journal(
        db, postings, date=date, memo=memo or "Bill #%s" % doc_id, commit=False
    )

    # receive stock for tracked item lines and update moving-average cost
    for ln in priced:
        if ln.get("item"):
            item = db.item[ln["item"]]
            if item.tracked:
                _update_moving_average(db, item, ln["qty"], ln["unit_price"])
                row = _get_stock_row(db, item.id, None)
                row.update_record(qty=_dec(row.qty) + ln["qty"])
                db.stock_move.insert(
                    item=item.id,
                    kind="receipt",
                    qty=ln["qty"],
                    unit_cost=ln["unit_price"],
                    document=doc_id,
                    txn=txn_id,
                    created_on=_now(),
                )

    db.document[doc_id].update_record(txn=txn_id)
    if tags:
        add_tags(db, "document", doc_id, tags, commit=False)
    _emit(db, "bill.posted", {"id": doc_id, "party": party.name, "total": str(total)})
    if commit:
        db.commit()
    return doc_id


# ---------------------------------------------------------------------------
# payments
# ---------------------------------------------------------------------------


def create_payment(
    db,
    party_id,
    amount,
    direction="in",
    account=ACC_BANK,
    date=None,
    allocations=None,
    memo="",
    external_id=None,
    tags=None,
    commit=True,
):
    """Record a payment.

    direction ``in``  -- customer pays us: Dr bank / Cr AR.
    direction ``out`` -- we pay a vendor:  Dr AP   / Cr bank.
    ``allocations`` is a list of ``{"document": id, "amount": n}`` matching the
    payment to invoices/bills (updates their ``allocated`` total).
    """
    if external_id:
        existing = db(
            (db.document.external_id == external_id) & (db.document.kind == "payment")
        ).select().first()
        if existing:
            return existing.id
    party = db.party[party_id]
    if not party:
        raise ErpError("Unknown party %r" % party_id)
    amount = _dec(amount)
    date = date or _today()

    doc_id = db.document.insert(
        kind="payment",
        party=party_id,
        doc_date=date,
        status="posted",
        total=amount,
        memo=memo,
        external_id=external_id,
        created_on=_now(),
    )

    if direction == "in":
        postings = [
            {"account": account, "amount": amount},
            {"account": party_ar_account(party), "amount": -amount},
        ]
        ev = "payment.received"
    elif direction == "out":
        postings = [
            {"account": party_ap_account(party), "amount": amount},
            {"account": account, "amount": -amount},
        ]
        ev = "payment.sent"
    else:
        raise ErpError("direction must be 'in' or 'out'")

    txn_id = post_journal(
        db, postings, date=date, memo=memo or "Payment #%s" % doc_id, commit=False
    )
    db.document[doc_id].update_record(txn=txn_id)

    for alloc in allocations or []:
        target = db.document[alloc["document"]]
        if not target:
            raise ErpError("Unknown document to allocate %r" % alloc["document"])
        amt = _dec(alloc["amount"])
        db.allocation.insert(payment=doc_id, target=target.id, amount=amt)
        target.update_record(allocated=_dec(target.allocated) + amt)

    if tags:
        add_tags(db, "document", doc_id, tags, commit=False)
    _emit(db, ev, {"id": doc_id, "party": party.name, "amount": str(amount)})
    if commit:
        db.commit()
    return doc_id


# ---------------------------------------------------------------------------
# voiding (reverse a posted document or stock move)
# ---------------------------------------------------------------------------


def _document_txn_ids(db, doc):
    """Every distinct ledger txn a document produced: its own ``txn`` plus any
    auxiliary txns booked through its stock moves (e.g. an invoice's COGS)."""
    ids = []
    if doc.txn:
        ids.append(doc.txn)
    for mv in db(db.stock_move.document == doc.id).select():
        if mv.txn and mv.txn not in ids:
            ids.append(mv.txn)
    return ids


def _reverse_txn(db, txn_id, date, memo):
    """Post a journal that negates every posting of ``txn_id`` (keeps the
    ledger balanced because the original was balanced)."""
    postings = [
        {"account": p.account, "amount": -_dec(p.amount_value)}
        for p in db(db.posting.txn == txn_id).select(orderby=db.posting.ordering)
    ]
    if postings:
        return post_journal(db, postings, date=date, memo=memo, commit=False)
    return None


def void_document(db, doc_id, date=None, commit=True):
    """Reverse a posted invoice / bill / payment.

    Posts a reversing journal for every ledger transaction the document
    created, restores the stock quantities its moves changed, unwinds any
    payment allocations, and marks the document ``status='void'``.  The ledger
    is kept perfectly balanced (every reversing journal nets to zero).

    Note: moving-average unit costs are *not* recomputed on void — re-establish
    them with a fresh stock receipt if a voided bill had changed them.
    """
    doc = db.document[doc_id]
    if not doc:
        raise ErpError("Unknown document %r" % doc_id)
    if doc.status == "void":
        raise ErpError("Document #%s is already void" % doc_id)
    date = date or _today()

    for tid in _document_txn_ids(db, doc):
        _reverse_txn(db, tid, date, "Void %s #%s" % (doc.kind, doc_id))

    # undo the quantity effect of each stock move this document made
    for mv in db(db.stock_move.document == doc_id).select():
        q = _dec(mv.qty)
        if mv.kind == "issue":
            row = _get_stock_row(db, mv.item, mv.location)
            row.update_record(qty=_dec(row.qty) + q)
        elif mv.kind in ("receipt", "adjust"):
            row = _get_stock_row(db, mv.item, mv.location)
            row.update_record(qty=_dec(row.qty) - q)
        elif mv.kind == "transfer":
            src = _get_stock_row(db, mv.item, mv.location)
            dst = _get_stock_row(db, mv.item, mv.to_location)
            src.update_record(qty=_dec(src.qty) + q)
            dst.update_record(qty=_dec(dst.qty) - q)

    # unwind allocations made *by* this payment ...
    for alloc in db(db.allocation.payment == doc_id).select():
        target = db.document[alloc.target]
        if target:
            target.update_record(allocated=_dec(target.allocated) - _dec(alloc.amount))
        alloc.delete_record()
    # ... and any allocations made *against* this invoice/bill by other payments
    for alloc in db(db.allocation.target == doc_id).select():
        alloc.delete_record()

    doc.update_record(status="void", allocated=ZERO)
    _emit(db, "document.voided", {"id": doc_id, "kind": doc.kind})
    if commit:
        db.commit()
    return doc_id


def void_stock_move(db, move_id, date=None, commit=True):
    """Reverse a standalone stock move (one not tied to a document): negate its
    ledger txn and undo its quantity change.  Use :func:`void_document` for
    moves that belong to an invoice or bill."""
    mv = db.stock_move[move_id]
    if not mv:
        raise ErpError("Unknown stock move %r" % move_id)
    if mv.document:
        raise ErpError("Move #%s belongs to document #%s; void that instead"
                       % (move_id, mv.document))
    date = date or _today()
    if mv.txn:
        _reverse_txn(db, mv.txn, date, "Void stock move #%s" % move_id)
    q = _dec(mv.qty)
    if mv.kind == "issue":
        _get_stock_row(db, mv.item, mv.location).update_record(
            qty=_dec(_get_stock_row(db, mv.item, mv.location).qty) + q)
    elif mv.kind in ("receipt", "adjust"):
        _get_stock_row(db, mv.item, mv.location).update_record(
            qty=_dec(_get_stock_row(db, mv.item, mv.location).qty) - q)
    elif mv.kind == "transfer":
        src = _get_stock_row(db, mv.item, mv.location)
        dst = _get_stock_row(db, mv.item, mv.to_location)
        src.update_record(qty=_dec(src.qty) + q)
        dst.update_record(qty=_dec(dst.qty) - q)
    mv.delete_record()
    if commit:
        db.commit()
    return move_id


# ---------------------------------------------------------------------------
# reporting (replay the ledger through pacioli)
# ---------------------------------------------------------------------------


def build_pacioli(db, begin=None, end=None):
    p = pacioli_db.db_to_pacioli(db)
    p.finalize()
    if begin:
        p.begin_date = begin if isinstance(begin, datetime.date) else pacioli.parse_date(begin)
    if end:
        p.end_date = end if isinstance(end, datetime.date) else pacioli.parse_date(end)
    p.run()
    return p


def _wallet_to_list(wallet):
    return [
        {"commodity": c, "value": str(v)} for c, v in wallet if v != 0
    ]


def trial_balance(db):
    p = build_pacioli(db)
    out = []
    for name in sorted(p.accounts):
        amounts = _wallet_to_list(p.accounts[name].wallet)
        if amounts:
            out.append({"account": name, "amounts": amounts})
    return out


def account_balance(db, name):
    p = build_pacioli(db)
    if name not in p.accounts:
        return []
    return _wallet_to_list(p.accounts[name].wallet)


def party_balance(db, party_id):
    party = db.party[party_id]
    if not party:
        raise ErpError("Unknown party %r" % party_id)
    p = build_pacioli(db)
    ar = party_ar_account(party)
    ap = party_ap_account(party)
    return {
        "receivable": _wallet_to_list(p.accounts[ar].wallet) if ar in p.accounts else [],
        "payable": _wallet_to_list(p.accounts[ap].wallet) if ap in p.accounts else [],
    }


def profit_and_loss(db, begin=None, end=None):
    p = build_pacioli(db, begin=begin, end=end)
    diff = p.diff_accounts
    out = {"income": [], "expenses": [], "net": []}
    income = _dec(0)
    expenses = _dec(0)
    for name in sorted(diff):
        top = name.split(":")[0]
        if top == "Income" and name != "Income":
            amounts = _wallet_to_list(diff[name].wallet)
            if amounts:
                out["income"].append({"account": name, "amounts": amounts})
        elif top == "Expenses" and name != "Expenses":
            amounts = _wallet_to_list(diff[name].wallet)
            if amounts:
                out["expenses"].append({"account": name, "amounts": amounts})
    income = -_dec(diff["Income"].wallet[DEFAULT_CURRENCY])
    expenses = _dec(diff["Expenses"].wallet[DEFAULT_CURRENCY])
    out["net"] = str(income - expenses)
    out["total_income"] = str(income)
    out["total_expenses"] = str(expenses)
    return out


def balance_sheet(db, date=None):
    p = build_pacioli(db, end=date)
    out = {"assets": [], "liabilities": [], "equity": []}
    for name in sorted(p.accounts):
        top = name.split(":")[0]
        bucket = {
            "Assets": "assets",
            "Liabilities": "liabilities",
            "Equity": "equity",
        }.get(top)
        if bucket and name != top:
            amounts = _wallet_to_list(p.accounts[name].wallet)
            if amounts:
                out[bucket].append({"account": name, "amounts": amounts})
    return out


def stock_levels(db):
    out = []
    for item in db(db.item).select(orderby=db.item.sku):
        qty = stock_on_hand(db, item.id)
        out.append(
            {
                "id": item.id,
                "sku": item.sku,
                "name": item.name,
                "qty": str(qty),
                "cost": str(_dec(item.cost)),
                "value": str((qty * _dec(item.cost)).quantize(R("0.01"))),
            }
        )
    return out


def reconcile_inventory(db):
    """Compare operational stock value (qty*cost) with the ledger inventory
    asset accounts.  Returns a per-item list and an overall ``ok`` flag."""
    p = build_pacioli(db)
    rows = []
    ok = True
    for item in db(db.item).select(orderby=db.item.sku):
        if not item.tracked:
            continue
        qty = stock_on_hand(db, item.id)
        op_value = (qty * _dec(item.cost)).quantize(R("0.01"))
        acc = item_inventory_account(item)
        ledger_value = _dec(p.accounts[acc].wallet[DEFAULT_CURRENCY]) if acc in p.accounts else ZERO
        ledger_value = ledger_value.quantize(R("0.01"))
        match = op_value == ledger_value
        ok = ok and match
        rows.append(
            {
                "sku": item.sku,
                "operational_value": str(op_value),
                "ledger_value": str(ledger_value),
                "match": match,
            }
        )
    return {"ok": ok, "items": rows}


def setup(db):
    """Convenience: define tables + base chart of accounts."""
    define_tables(db)
    init_accounts(db)
    return db
