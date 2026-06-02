"""
REST API + minimal back-office UI for the pacioli ERP core.

Every /api/* endpoint returns JSON.  Writes go through erp_core, which turns
each document into a balanced pacioli ledger transaction.  See PLAN.md for the
full API reference.

Auth: send ``Authorization: Bearer <token>`` (tokens are rows in
db.auth_simple_token, managed at /pacioli/tokens).  In development mode
(settings.API_REQUIRE_AUTH = False) the API is open for easy local testing.
"""

import datetime
import json
import os

from py4web import HTTP, URL, action, redirect, request, response
from py4web.utils.grid import Grid

from pacioli_erp import erp as erp_core
from pacioli_erp.erp import ErpError

from . import invoice_pdf, settings
from .common import T, auth, db, flash, session

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _authorized():
    # The JSON API keeps its configurable behaviour: open in development for
    # easy local testing, bearer-token/session protected otherwise.  The HTML
    # back-office *pages* are gated separately via the auth.user fixture (only
    # index, api/health and the auth pages are public).
    if not settings.API_REQUIRE_AUTH:
        return True
    return bool(auth.user_id)


def _require_auth():
    if not _authorized():
        _fail(401, "authentication required (Authorization: Bearer <token>)")


def _fail(code, message):
    raise HTTP(
        code,
        json.dumps({"error": message}),
        headers={"Content-Type": "application/json"},
    )


def _body():
    data = request.json
    if data is None:
        try:
            data = json.loads(request.body.read() or b"{}")
        except (ValueError, TypeError):
            data = {}
    return data or {}


def _date(value):
    if not value:
        return None
    return datetime.date.fromisoformat(value)


def _party_dict(row):
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "currency": row.currency,
        "ar_account": row.ar_account,
        "ap_account": row.ap_account,
        "external_ref": row.external_ref,
        "email": row.email,
        "phone": row.phone,
        "address": row.address,
        "tags": erp_core.get_tags(db, "party", row.id),
    }


def _item_dict(row):
    return {
        "id": row.id,
        "sku": row.sku,
        "name": row.name,
        "uom": row.uom,
        "valuation": row.valuation,
        "cost": str(row.cost),
        "price": str(row.price),
        "tracked": row.tracked,
        "inventory_account": erp_core.item_inventory_account(row),
        "income_account": erp_core.item_income_account(row),
        "cogs_account": erp_core.item_cogs_account(row),
        "tags": erp_core.get_tags(db, "item", row.id),
    }


def _doc_dict(row):
    return {
        "id": row.id,
        "kind": row.kind,
        "party": row.party,
        "party_name": row.party_name,
        "party_address": row.party_address,
        "date": str(row.doc_date) if row.doc_date else None,
        "status": row.status,
        "currency": row.currency,
        "subtotal": str(row.subtotal),
        "tax_total": str(row.tax_total),
        "total": str(row.total),
        "allocated": str(row.allocated),
        "memo": row.memo,
        "external_id": row.external_id,
        "txn": row.txn,
        "tags": erp_core.get_tags(db, "document", row.id),
    }


def _tag_list(name="tag"):
    """Repeated query params (ombott's request.query is single-value)."""
    from urllib.parse import parse_qs

    return parse_qs(request.query_string).get(name, [])


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


@action("api/health", method=["GET"])
@action.uses(db)
def api_health():
    return {
        "ok": True,
        "service": "pacioli-erp",
        "version": "0.1.0",
        "accounts": db(db.account).count(),
        "transactions": db(db.txn).count(),
    }


# ---------------------------------------------------------------------------
# ledger & reports
# ---------------------------------------------------------------------------


@action("api/accounts", method=["GET"])
@action.uses(db, session, auth)
def api_accounts():
    _require_auth()
    return {"accounts": erp_core.trial_balance(db)}


@action("api/accounts/<name>", method=["GET"])
@action.uses(db, session, auth)
def api_account(name):
    _require_auth()
    return {"account": name, "amounts": erp_core.account_balance(db, name)}


@action("api/ledger/transactions", method=["GET"])
@action.uses(db, session, auth)
def api_transactions():
    _require_auth()
    q = db.txn.id > 0
    frm = request.query.get("from")
    to = request.query.get("to")
    if frm:
        q &= db.txn.tdate >= _date(frm)
    if to:
        q &= db.txn.tdate <= _date(to)
    out = []
    for t in db(q).select(orderby=db.txn.tdate | db.txn.id):
        postings = [
            {
                "account": p.account,
                "amount": str(p.amount_value),
                "commodity": p.amount_commodity,
            }
            for p in db(db.posting.txn == t.id).select(orderby=db.posting.ordering)
        ]
        out.append(
            {
                "id": t.id,
                "date": str(t.tdate),
                "state": t.state,
                "info": t.info,
                "postings": postings,
            }
        )
    return {"transactions": out}


@action("api/ledger/journals", method=["POST"])
@action.uses(db, session, auth)
def api_journal():
    _require_auth()
    data = _body()
    try:
        txn_id = erp_core.post_journal(
            db,
            data["lines"],
            date=_date(data.get("date")),
            memo=data.get("memo", ""),
            currency=data.get("currency", erp_core.DEFAULT_CURRENCY),
        )
    except (ErpError, KeyError) as ex:
        _fail(400, str(ex))
    return {"txn": txn_id}


@action("api/reports/trial_balance", method=["GET"])
@action.uses(db, session, auth)
def api_trial_balance():
    _require_auth()
    return {"trial_balance": erp_core.trial_balance(db)}


@action("api/reports/profit_and_loss", method=["GET"])
@action.uses(db, session, auth)
def api_pl():
    _require_auth()
    return erp_core.profit_and_loss(
        db, begin=_date(request.query.get("begin")), end=_date(request.query.get("end"))
    )


@action("api/reports/balance_sheet", method=["GET"])
@action.uses(db, session, auth)
def api_bs():
    _require_auth()
    return erp_core.balance_sheet(db, date=_date(request.query.get("date")))


# ---------------------------------------------------------------------------
# parties
# ---------------------------------------------------------------------------


@action("api/parties", method=["GET", "POST"])
@action.uses(db, session, auth)
def api_parties():
    _require_auth()
    if request.method == "POST":
        data = _body()
        try:
            pid = erp_core.create_party(
                db,
                data["name"],
                kind=data.get("kind", "customer"),
                currency=data.get("currency", erp_core.DEFAULT_CURRENCY),
                external_ref=data.get("external_ref"),
                email=data.get("email"),
                phone=data.get("phone"),
                address=data.get("address"),
                tags=data.get("tags"),
            )
        except (ErpError, KeyError) as ex:
            _fail(400, str(ex))
        return {"party": _party_dict(db.party[pid])}
    return {"parties": [_party_dict(r) for r in db(db.party).select(orderby=db.party.name)]}


@action("api/parties/<pid:int>", method=["GET", "PUT"])
@action.uses(db, session, auth)
def api_party(pid):
    _require_auth()
    party = db.party[pid]
    if not party:
        _fail(404, "party not found")
    if request.method == "PUT":
        data = _body()
        party.update_record(
            **{k: v for k, v in data.items()
               if k in ("name", "kind", "currency", "external_ref", "email", "phone", "address")}
        )
        db.commit()
    out = _party_dict(db.party[pid])
    out["balance"] = erp_core.party_balance(db, pid)
    return out


# ---------------------------------------------------------------------------
# items & stock
# ---------------------------------------------------------------------------


@action("api/items", method=["GET", "POST"])
@action.uses(db, session, auth)
def api_items():
    _require_auth()
    if request.method == "POST":
        data = _body()
        try:
            iid = erp_core.create_item(
                db,
                data["sku"],
                name=data.get("name"),
                cost=data.get("cost", 0),
                price=data.get("price", 0),
                tracked=data.get("tracked", True),
                income_account=data.get("income_account"),
                cogs_account=data.get("cogs_account"),
                inventory_account=data.get("inventory_account"),
                tags=data.get("tags"),
            )
        except (ErpError, KeyError) as ex:
            _fail(400, str(ex))
        return {"item": _item_dict(db.item[iid])}
    return {"items": [_item_dict(r) for r in db(db.item).select(orderby=db.item.sku)]}


@action("api/items/<iid:int>", method=["GET", "PUT"])
@action.uses(db, session, auth)
def api_item(iid):
    _require_auth()
    item = db.item[iid]
    if not item:
        _fail(404, "item not found")
    if request.method == "PUT":
        data = _body()
        item.update_record(
            **{k: v for k, v in data.items() if k in ("name", "price", "uom", "tracked")}
        )
        db.commit()
        item = db.item[iid]
    out = _item_dict(item)
    out["qty"] = str(erp_core.stock_on_hand(db, iid))
    return out


@action("api/stock", method=["GET"])
@action.uses(db, session, auth)
def api_stock():
    _require_auth()
    return {"stock": erp_core.stock_levels(db)}


@action("api/stock/moves", method=["POST"])
@action.uses(db, session, auth)
def api_stock_move():
    _require_auth()
    data = _body()
    try:
        mid = erp_core.create_stock_move(
            db,
            data["item"],
            data["kind"],
            data["qty"],
            unit_cost=data.get("unit_cost"),
            location_id=data.get("location"),
            to_location_id=data.get("to_location"),
            date=_date(data.get("date")),
            external_id=data.get("external_id"),
        )
    except (ErpError, KeyError) as ex:
        _fail(400, str(ex))
    return {"move": mid}


@action("api/stock/reconcile", method=["GET"])
@action.uses(db, session, auth)
def api_reconcile():
    _require_auth()
    return erp_core.reconcile_inventory(db)


# ---------------------------------------------------------------------------
# invoices, bills, payments
# ---------------------------------------------------------------------------


def _create_doc(kind, fn):
    data = _body()
    try:
        doc_id = fn(
            db,
            data["party"],
            data["lines"],
            date=_date(data.get("date")),
            memo=data.get("memo", ""),
            external_id=data.get("external_id"),
            tags=data.get("tags"),
        )
    except (ErpError, KeyError) as ex:
        _fail(400, str(ex))
    return {kind: _doc_dict(db.document[doc_id])}


@action("api/invoices", method=["GET", "POST"])
@action.uses(db, session, auth)
def api_invoices():
    _require_auth()
    if request.method == "POST":
        return _create_doc("invoice", erp_core.create_invoice)
    rows = db(db.document.kind == "invoice").select(orderby=~db.document.id)
    return {"invoices": [_doc_dict(r) for r in rows]}


@action("api/bills", method=["GET", "POST"])
@action.uses(db, session, auth)
def api_bills():
    _require_auth()
    if request.method == "POST":
        return _create_doc("bill", erp_core.create_bill)
    rows = db(db.document.kind == "bill").select(orderby=~db.document.id)
    return {"bills": [_doc_dict(r) for r in rows]}


@action("api/invoices/<did:int>", method=["GET"])
@action("api/bills/<did:int>", method=["GET"])
@action.uses(db, session, auth)
def api_document(did):
    _require_auth()
    doc = db.document[did]
    if not doc:
        _fail(404, "document not found")
    out = _doc_dict(doc)
    out["lines"] = [
        {
            "item": l.item,
            "account": l.account,
            "description": l.description,
            "qty": str(l.qty),
            "unit_price": str(l.unit_price),
            "tax_rate": str(l.tax_rate),
            "amount": str(l.amount),
        }
        for l in db(db.document_line.document == did).select()
    ]
    return out


def _line_description(line):
    """Human label for a document line: explicit description, else the item
    name/sku, else the posting account."""
    if line.description:
        return line.description
    if line.item:
        item = db.item[line.item]
        if item:
            return item.name or item.sku
    return line.account or ""


def _invoice_for_pdf(doc):
    """Assemble the plain dict the invoice_pdf renderer expects."""
    lines = [
        {
            "description": _line_description(l),
            "qty": str(l.qty),
            "unit_price": str(l.unit_price),
            "tax_rate": str(l.tax_rate),
            "amount": str(l.amount),
        }
        for l in db(db.document_line.document == doc.id).select(orderby=db.document_line.id)
    ]
    balance_due = (doc.total or erp_core.ZERO) - (doc.allocated or erp_core.ZERO)
    return {
        "number": doc.id,
        "date": str(doc.doc_date) if doc.doc_date else "",
        "status": doc.status,
        "currency": doc.currency,
        "bill_to_name": doc.party_name or (db.party[doc.party].name if doc.party else ""),
        "bill_to_address": doc.party_address or "",
        "memo": doc.memo or "",
        "lines": lines,
        "subtotal": str(doc.subtotal),
        "tax_total": str(doc.tax_total),
        "total": str(doc.total),
        "allocated": str(doc.allocated),
        "balance_due": str(balance_due),
    }


@action("api/invoices/<did:int>/pdf", method=["GET"])
@action.uses(db, session, auth)
def api_invoice_pdf(did):
    _require_auth()
    doc = db.document[did]
    if not doc or doc.kind != "invoice":
        _fail(404, "invoice not found")
    try:
        pdf_bytes = invoice_pdf.render_invoice_pdf(_invoice_for_pdf(doc))
    except invoice_pdf.PdfDependencyError as ex:
        _fail(501, str(ex))
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = (
        'attachment; filename="invoice-%s.pdf"' % did
    )
    return pdf_bytes


@action("api/payments", method=["GET", "POST"])
@action.uses(db, session, auth)
def api_payments():
    _require_auth()
    if request.method == "POST":
        data = _body()
        try:
            pid = erp_core.create_payment(
                db,
                data["party"],
                data["amount"],
                direction=data.get("direction", "in"),
                account=data.get("account", erp_core.ACC_BANK),
                date=_date(data.get("date")),
                allocations=data.get("allocations"),
                memo=data.get("memo", ""),
                external_id=data.get("external_id"),
                tags=data.get("tags"),
            )
        except (ErpError, KeyError) as ex:
            _fail(400, str(ex))
        return {"payment": _doc_dict(db.document[pid])}
    rows = db(db.document.kind == "payment").select(orderby=~db.document.id)
    return {"payments": [_doc_dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


@action("api/events", method=["GET"])
@action.uses(db, session, auth)
def api_events():
    _require_auth()
    since = int(request.query.get("since") or 0)
    rows = db(db.event.id > since).select(orderby=db.event.id, limitby=(0, 500))
    return {
        "events": [
            {
                "id": r.id,
                "kind": r.kind,
                "payload": r.payload,
                "created_on": str(r.created_on),
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# search & tags (any system table)
# ---------------------------------------------------------------------------


@action("api/search/<table>", method=["GET"])
@action.uses(db, session, auth)
def api_search(table):
    """Search a system table by text identifier (?q=) and/or tags (?tag=a&tag=b).

    ?mode=and|or selects tag combination (default and).  Returns id + label +
    tags for each match."""
    _require_auth()
    if table not in erp_core.SYSTEM_TABLES:
        _fail(404, "unknown table %r" % table)
    text = request.query.get("q")
    tags = _tag_list("tag")
    mode = request.query.get("mode") or "and"
    try:
        rows = erp_core.search(db, table, text=text, tags=tags or None, tag_mode=mode)
    except ErpError as ex:
        _fail(400, str(ex))
    return {
        "table": table,
        "count": len(rows),
        "results": [erp_core.record_summary(db, table, r) for r in rows],
    }


@action("api/tags/<table>/<rid:int>", method=["GET", "POST"])
@action.uses(db, session, auth)
def api_tags(table, rid):
    """GET the tags on a record, or POST {add:[...], remove:[...]} to change them."""
    _require_auth()
    if table not in erp_core.SYSTEM_TABLES:
        _fail(404, "unknown table %r" % table)
    if not db[table][rid]:
        _fail(404, "record not found")
    if request.method == "POST":
        data = _body()
        try:
            if data.get("remove"):
                erp_core.remove_tags(db, table, rid, data["remove"], commit=False)
            if data.get("add"):
                erp_core.add_tags(db, table, rid, data["add"], commit=False)
            db.commit()
        except ErpError as ex:
            _fail(400, str(ex))
    return {"table": table, "id": rid, "tags": erp_core.get_tags(db, table, rid)}


# ---------------------------------------------------------------------------
# back-office UI
#
# Public: index (reports SPA), api/health, auth/*.  Everything else requires a
# logged-in user (auth.user).  Master-data tables (parties, items) get native
# Grid create/edit/delete; transactional tables (invoices, bills, payments,
# stock) get a Grid list plus create forms wired to erp_core and a Void action
# (erp_core.void_document / void_stock_move) so the ledger stays balanced.
# ---------------------------------------------------------------------------


@action("index")
@action.uses("index.html", db, session, auth)
def index():
    return dict(
        api_base=URL("api"),
        require_auth=settings.API_REQUIRE_AUTH,
        active="",
        user=auth.get_user(),
    )


# Public documentation pages — rendered from the repo's markdown files so they
# are available (and styled) without logging in.
_REPO_ROOT = os.path.dirname(os.path.dirname(settings.APP_FOLDER))
DOCS = [
    ("api", "API.md", "API manual"),
    ("plan", "PLAN.md", "Architecture"),
    ("readme", "README.md", "Overview"),
]


@action("docs")
@action("docs/<name>")
@action.uses("docs.html", session, db, auth)
def docs(name="api"):
    match = next((d for d in DOCS if d[0] == name), None)
    if not match:
        redirect(URL("docs", "api"))
    _, filename, title = match
    try:
        with open(os.path.join(_REPO_ROOT, filename), encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        content = "# Not available\n\n`%s` could not be read." % filename
    return dict(
        content=content,
        doc_name=name,
        title=title,
        docs=[(k, label) for k, _f, label in DOCS],
        user=auth.get_user(),
    )


def _today_str():
    return datetime.date.today().isoformat()


def _void_doc_button(next_page):
    """A Grid post-action button that voids a document via erp_core."""

    def make(row):
        if row.status == "void":
            return None
        return dict(
            text="Void",
            url=URL("doc_void", row.id, vars=dict(next=next_page)),
            icon="fa-ban",
            kind="grid-delete-button",
        )

    return make


def _invoice_pdf_button(row):
    """A Grid post-action button that downloads the invoice as a PDF.

    Uses the same pill style as the built-in Details button (grid-details-button)
    so it reads as a button, not a bare link."""
    return dict(
        text="PDF",
        url=URL("api/invoices", row.id, "pdf"),
        icon="fa-file-pdf",
        kind="grid-details-button",
    )


def _void_move_button(row):
    if row.document:
        return None  # belongs to a doc; void the document instead
    return dict(
        text="Void", url=URL("move_void", row.id), icon="fa-ban", kind="grid-delete-button"
    )


# ---- master data: native Grid CRUD ----------------------------------------


@action("parties")
@action("parties/<path:path>")
@action.uses("grid.html", session, db, auth.user)
def parties(path=None):
    grid = Grid(
        db.party.id > 0,
        orderby=db.party.name,
        create=True,
        editable=True,
        deletable=True,
        details=True,
    )
    return dict(grid=grid, active="parties", title="Parties", user=auth.get_user())


@action("items")
@action("items/<path:path>")
@action.uses("grid.html", session, db, auth.user)
def items(path=None):
    grid = Grid(
        db.item.id > 0,
        orderby=db.item.sku,
        create=True,
        editable=True,
        deletable=True,
        details=True,
    )
    return dict(grid=grid, active="items", title="Items", user=auth.get_user())


# ---- inventory: moves list + create + void --------------------------------


@action("stock")
@action("stock/<path:path>")
@action.uses("grid.html", session, db, auth.user)
def stock(path=None):
    grid = Grid(
        db.stock_move.id > 0,
        orderby=~db.stock_move.id,
        create=False,
        editable=False,
        deletable=False,
        details=True,
        post_action_buttons=[_void_move_button],
    )
    return dict(
        grid=grid,
        active="stock",
        title="Inventory moves",
        new_url=URL("stock_new"),
        new_label="New stock move",
        hint="Each move posts to the ledger at moving-average cost. Void reverses it.",
        user=auth.get_user(),
    )


@action("stock_new", method=["GET", "POST"])
@action.uses("stock_form.html", session, db, auth.user)
def stock_new():
    if request.method == "POST":
        f = request.forms
        try:
            if not f.get("item"):
                raise ErpError("Select an item")
            uc = f.get("unit_cost")
            erp_core.create_stock_move(
                db,
                int(f.get("item")),
                f.get("kind") or "receipt",
                f.get("qty") or "0",
                unit_cost=(uc if uc else None),
                date=_date(f.get("date")),
            )
            flash.set("Stock move posted")
            redirect(URL("stock"))
        except (ErpError, ValueError) as ex:
            flash.set(str(ex), _class="error")
    return dict(
        active="stock",
        title="New stock move",
        items=db(db.item).select(orderby=db.item.sku),
        today=_today_str(),
        user=auth.get_user(),
    )


@action("move_void/<move_id:int>")
@action.uses(session, db, auth.user)
def move_void(move_id):
    try:
        erp_core.void_stock_move(db, move_id)
        flash.set("Stock move voided")
    except ErpError as ex:
        flash.set(str(ex), _class="error")
    redirect(URL("stock"))


# ---- documents: invoices, bills, payments ---------------------------------


def _doc_grid(path, kind, next_page):
    buttons = [_void_doc_button(next_page)]
    if kind == "invoice":
        buttons.insert(0, _invoice_pdf_button)
    return Grid(
        db.document.kind == kind,
        orderby=~db.document.id,
        create=False,
        editable=False,
        deletable=False,
        details=True,
        post_action_buttons=buttons,
    )


@action("invoices")
@action("invoices/<path:path>")
@action.uses("grid.html", session, db, auth.user)
def invoices(path=None):
    return dict(
        grid=_doc_grid(path, "invoice", "invoices"),
        active="invoices",
        title="Invoices",
        new_url=URL("invoice_new"),
        new_label="New invoice",
        hint="Posted invoices book AR, revenue and COGS. To change one, Void and re-create.",
        user=auth.get_user(),
    )


@action("bills")
@action("bills/<path:path>")
@action.uses("grid.html", session, db, auth.user)
def bills(path=None):
    return dict(
        grid=_doc_grid(path, "bill", "bills"),
        active="bills",
        title="Bills",
        new_url=URL("bill_new"),
        new_label="New bill",
        hint="Posted bills book AP and receive stock. To change one, Void and re-create.",
        user=auth.get_user(),
    )


@action("payments")
@action("payments/<path:path>")
@action.uses("grid.html", session, db, auth.user)
def payments(path=None):
    return dict(
        grid=_doc_grid(path, "payment", "payments"),
        active="payments",
        title="Payments",
        new_url=URL("payment_new"),
        new_label="New payment",
        hint="Payments move cash and settle AR/AP. Void reverses the entry and any allocation.",
        user=auth.get_user(),
    )


def _doc_new(kind, fn):
    if request.method == "POST":
        f = request.forms
        try:
            if not f.get("party"):
                raise ErpError("Select a party")
            lines = json.loads(f.get("lines_json") or "[]")
            fn(
                db,
                int(f.get("party")),
                lines,
                date=_date(f.get("date")),
                memo=f.get("memo") or "",
                external_id=f.get("external_id") or None,
            )
            flash.set("%s posted" % kind.title())
            redirect(URL(kind + "s"))
        except (ErpError, ValueError, KeyError) as ex:
            flash.set(str(ex), _class="error")
    return dict(
        kind=kind,
        active=kind + "s",
        title="New %s" % kind,
        parties=db(db.party).select(orderby=db.party.name),
        items=db(db.item).select(orderby=db.item.sku),
        today=_today_str(),
        user=auth.get_user(),
    )


@action("invoice_new", method=["GET", "POST"])
@action.uses("doc_form.html", session, db, auth.user)
def invoice_new():
    return _doc_new("invoice", erp_core.create_invoice)


@action("bill_new", method=["GET", "POST"])
@action.uses("doc_form.html", session, db, auth.user)
def bill_new():
    return _doc_new("bill", erp_core.create_bill)


@action("payment_new", method=["GET", "POST"])
@action.uses("payment_form.html", session, db, auth.user)
def payment_new():
    if request.method == "POST":
        f = request.forms
        try:
            if not f.get("party"):
                raise ErpError("Select a party")
            erp_core.create_payment(
                db,
                int(f.get("party")),
                f.get("amount") or "0",
                direction=f.get("direction") or "in",
                account=f.get("account") or erp_core.ACC_BANK,
                date=_date(f.get("date")),
                memo=f.get("memo") or "",
                external_id=f.get("external_id") or None,
            )
            flash.set("Payment recorded")
            redirect(URL("payments"))
        except (ErpError, ValueError) as ex:
            flash.set(str(ex), _class="error")
    return dict(
        active="payments",
        title="New payment",
        parties=db(db.party).select(orderby=db.party.name),
        default_account=erp_core.ACC_BANK,
        today=_today_str(),
        user=auth.get_user(),
    )


@action("doc_void/<doc_id:int>")
@action.uses(session, db, auth.user)
def doc_void(doc_id):
    nxt = request.query.get("next") or "index"
    try:
        erp_core.void_document(db, doc_id)
        flash.set("Document #%s voided" % doc_id)
    except ErpError as ex:
        flash.set(str(ex), _class="error")
    redirect(URL(nxt))


# ---- events (read-only audit log) -----------------------------------------


@action("events")
@action("events/<path:path>")
@action.uses("grid.html", session, db, auth.user)
def events(path=None):
    grid = Grid(
        db.event.id > 0,
        orderby=~db.event.id,
        create=False,
        editable=False,
        deletable=False,
        details=True,
    )
    return dict(grid=grid, active="events", title="Events", user=auth.get_user())


# ---- API tokens ------------------------------------------------------------


@action("tokens")
@action("tokens/<path:path>")
@action.uses("grid.html", session, db, auth.user)
def tokens(path=None):
    db.auth_simple_token.user_id.default = auth.user_id
    grid = Grid(
        db.auth_simple_token.user_id == auth.user_id,
        create=True,
        deletable=True,
        editable=False,
    )
    return dict(grid=grid, active="", title="API tokens", user=auth.get_user())
