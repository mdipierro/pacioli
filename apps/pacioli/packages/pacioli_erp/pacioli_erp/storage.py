#!/usr/bin/env python
"""
pydal storage backend for Pacioli.
License: BSD <http://opensource.org/licenses/BSD-3-Clause>

This module mirrors the ledger file format into a relational schema using
web2py's DAL (https://github.com/web2py/pydal) so that the very same ledger can
be stored in, and read back from, any database that pydal supports (sqlite,
postgres, mysql, ...).

The schema is intentionally a faithful, lossless image of the file syntax
understood by pacioli.py:

    account  -- one row per declared account (the `open`/`close`/`account` directive)
    txn      -- one row per transaction (date, state, code, payee/description, tags)
    posting  -- one row per posting inside a transaction
    chk      -- one row per `balance`/`pad` check
    price    -- one row per `P` price directive

Two helper functions move data both ways:

    file_to_db(filename, db)   read a ledger file and write it to the database
    db_to_file(db, filename)   read the database and write a ledger file

and :class:`DBSource` plugs the database into ``Pacioli.load`` / ``Pacioli.save``
through the same DataSource interface used by files.
"""

import datetime

from pydal import DAL, Field

from . import ledger as pacioli
from .ledger import (
    Amount,
    Check,
    DataSource,
    Pacioli,
    Posting,
    Transaction,
    parse_amount,
)

R = pacioli.R
END_TIME = pacioli.END_TIME


def connect(uri="sqlite://pacioli.sqlite", folder=None, **kwargs):
    """Open a DAL connection and make sure the pacioli tables exist."""
    db = DAL(uri, folder=folder, **kwargs)
    define_tables(db)
    return db


def define_tables(db):
    """Define the pacioli schema on an existing DAL instance.

    Returns the same ``db`` so it can be chained.  Safe to call more than once
    (``redefine=True``)."""
    db.define_table(
        "account",
        Field("name", "string", length=512, notnull=True, unique=True),
        Field("type", "string", length=8),  # debit/credit hint, optional
        Field("assets", "string", length=512),  # space separated commodities
        Field("open_date", "date"),
        Field("close_date", "date"),
        redefine=True,
    )
    db.define_table(
        "txn",
        Field("tdate", "date", notnull=True),
        Field("edate", "date"),  # effective date
        Field("state", "string", length=1),  # '*' cleared, '!' pending, '' none
        Field("code", "string", length=64),
        Field("info", "text"),  # payee / description
        Field("tags", "string", length=512),  # space separated
        Field("seq", "integer"),  # original ordering / line number
        redefine=True,
    )
    db.define_table(
        "posting",
        Field("txn", "reference txn", notnull=True),
        Field("ordering", "integer"),
        Field("account", "string", length=512, notnull=True),
        Field("amount_value", "decimal(20,6)"),
        Field("amount_commodity", "string", length=32),
        Field("at_value", "decimal(20,6)"),  # per-unit cost
        Field("at_commodity", "string", length=32),
        Field("assertion_value", "decimal(20,6)"),
        Field("assertion_commodity", "string", length=32),
        Field("virtual", "string", length=1),  # '(' or '[' or null
        Field("book", "boolean", default=False),
        Field("comment", "text"),
        redefine=True,
    )
    db.define_table(
        "chk",
        Field("cdate", "date", notnull=True),
        Field("account", "string", length=512, notnull=True),
        Field("amount_value", "decimal(20,6)"),
        Field("amount_commodity", "string", length=32),
        Field("balance_with", "string", length=512),
        Field("seq", "integer"),
        redefine=True,
    )
    db.define_table(
        "price",
        Field("pdate", "date", notnull=True),
        Field("commodity", "string", length=32),
        Field("value", "decimal(20,6)"),
        Field("base", "string", length=32),
        redefine=True,
    )
    return db


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    return value


def _amount(value, commodity):
    if value is None:
        return None
    return Amount(R(str(value)), commodity or "")


# ---------------------------------------------------------------------------
# pacioli -> db
# ---------------------------------------------------------------------------


def pacioli_to_db(p, db, truncate=True):
    """Write the in-memory :class:`Pacioli` ``p`` into the database ``db``."""
    if truncate:
        for t in ("posting", "txn", "chk", "price", "account"):
            db[t].truncate()

    for name, acc in p.accounts.items():
        db.account.insert(
            name=name,
            type=acc.type,
            assets=" ".join(acc.assets) if acc.assets else None,
            open_date=acc.open_date,
            close_date=acc.close_date if acc.close_date < END_TIME else None,
        )

    for item in p.ledger:
        if isinstance(item, Transaction):
            txn_id = db.txn.insert(
                tdate=item.date,
                edate=item.edate,
                state="!" if item.pending else "*",
                code=item.code,
                info=item.info,
                tags=" ".join(sorted(item.tags)) if item.tags else None,
                seq=item.id,
            )
            for order, posting in enumerate(item.postings):
                db.posting.insert(
                    txn=txn_id,
                    ordering=order,
                    account=posting.name,
                    amount_value=posting.amount.value if posting.amount else None,
                    amount_commodity=posting.amount.asset if posting.amount else None,
                    at_value=posting.at.value if posting.at else None,
                    at_commodity=posting.at.asset if posting.at else None,
                    assertion_value=(
                        posting.assertion.value if posting.assertion else None
                    ),
                    assertion_commodity=(
                        posting.assertion.asset if posting.assertion else None
                    ),
                    virtual=posting.virtual,
                    book=posting.book,
                    comment=posting.comment,
                )
        elif isinstance(item, Check):
            db.chk.insert(
                cdate=item.date,
                account=item.name,
                amount_value=item.amount.value,
                amount_commodity=item.amount.asset,
                balance_with=item.balance_with,
                seq=item.id,
            )

    for (pdate, symbol, amount) in p.prices:
        db.price.insert(
            pdate=pdate, commodity=symbol, value=amount.value, base=amount.asset
        )

    db.commit()
    return db


# ---------------------------------------------------------------------------
# db -> pacioli
# ---------------------------------------------------------------------------


def db_to_pacioli(db, p=None):
    """Populate (or create) a :class:`Pacioli` instance from the database."""
    if p is None:
        p = Pacioli()

    for row in db(db.account).select(orderby=db.account.id):
        assets = row.assets.split() if row.assets else None
        p.open_account(
            row.name, _as_date(row.open_date) or pacioli.BEGIN_TIME, assets, row.type
        )
        if row.close_date:
            p.accounts[row.name].close_date = _as_date(row.close_date)

    for trow in db(db.txn).select(orderby=db.txn.id):
        transaction = Transaction(
            _as_date(trow.tdate),
            trow.info or "",
            id=trow.seq,
            pending=(trow.state == "!"),
            code=trow.code,
            edate=_as_date(trow.edate),
            tags=set(trow.tags.split()) if trow.tags else set(),
        )
        postings = db(db.posting.txn == trow.id).select(orderby=db.posting.ordering)
        for prow in postings:
            transaction.postings.append(
                Posting(
                    name=prow.account,
                    amount=_amount(prow.amount_value, prow.amount_commodity),
                    at=_amount(prow.at_value, prow.at_commodity),
                    assertion=_amount(prow.assertion_value, prow.assertion_commodity),
                    virtual=prow.virtual,
                    book=bool(prow.book),
                    comment=prow.comment,
                )
            )
            p.ensure_account(prow.account, transaction.date)
        p.ledger.append(transaction)

    for crow in db(db.chk).select(orderby=db.chk.id):
        p.ensure_account(crow.account, _as_date(crow.cdate))
        p.ledger.append(
            Check(
                _as_date(crow.cdate),
                crow.account,
                _amount(crow.amount_value, crow.amount_commodity),
                balance_with=crow.balance_with,
                id=crow.seq,
            )
        )

    for prow in db(db.price).select(orderby=db.price.id):
        p.prices.append(
            (_as_date(prow.pdate), prow.commodity, _amount(prow.value, prow.base))
        )

    return p


# ---------------------------------------------------------------------------
# DataSource implementation
# ---------------------------------------------------------------------------


class DBSource(DataSource):
    """A :class:`pacioli.DataSource` backed by a pydal database."""

    def __init__(self, db):
        self.db = db
        define_tables(db)

    def load_into(self, p):
        db_to_pacioli(self.db, p)

    def dump_from(self, p):
        pacioli_to_db(p, self.db)


# ---------------------------------------------------------------------------
# file <-> db convenience functions
# ---------------------------------------------------------------------------


def file_to_db(filename, db):
    """Read a ledger file and store it in the database (return the Pacioli)."""
    p = Pacioli()
    p.load(filename)
    pacioli_to_db(p, db)
    return p


def db_to_file(db, filename):
    """Read the database and write a ledger file (return the Pacioli)."""
    p = db_to_pacioli(db)
    p.finalize()
    p.save(filename)
    return p


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Move a ledger between file and db.")
    parser.add_argument("--uri", default="sqlite://pacioli.sqlite")
    parser.add_argument("--folder", default=".")
    sub = parser.add_argument
    sub("--to-db", help="read this ledger file and write it to the db")
    sub("--to-file", help="read the db and write this ledger file")
    args = parser.parse_args()
    db = connect(args.uri, folder=args.folder)
    if args.to_db:
        file_to_db(args.to_db, db)
        print("imported %s into %s" % (args.to_db, args.uri))
    if args.to_file:
        db_to_file(db, args.to_file)
        print("exported %s from %s" % (args.to_file, args.uri))
