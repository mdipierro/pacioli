"""
pacioli_erp -- double-entry ledger (pacioli) + an API-first ERP core.

Modules:
  * ``pacioli_erp.ledger``  -- the double-entry engine and ledger-cli
                               parser/serializer (pure stdlib).
  * ``pacioli_erp.storage`` -- pydal storage backend for the ledger (needs pydal).
  * ``pacioli_erp.erp``     -- the ERP posting engine: invoices, bills, payments,
                               stock (needs pydal).
  * ``pacioli_erp.editor``  -- a dependency-free single-page ledger file editor.

Only ``ledger`` (and ``editor``) are imported eagerly here so that
``import pacioli_erp`` works without pydal/py4web installed.  Import the
database-backed pieces explicitly, e.g. ``from pacioli_erp import erp``.
"""

from .ledger import (
    Amount,
    Check,
    DataSource,
    FileSource,
    Pacioli,
    Posting,
    Transaction,
    Wallet,
    parse_amount,
    parse_date,
)

__all__ = (
    "Amount",
    "Check",
    "DataSource",
    "FileSource",
    "Pacioli",
    "Posting",
    "Transaction",
    "Wallet",
    "parse_amount",
    "parse_date",
)

__version__ = "0.1.0"
__author__ = "Massimo Di Pierro"
__license__ = "BSD-3-Clause"
