"""Define the ERP + ledger tables via pacioli_erp.erp, then seed the chart of
accounts.

All real schema lives in ``pacioli_erp.erp.define_tables`` so it can be
unit-tested without py4web.
"""

import sys

from pydal.validators import *  # noqa: F401,F403

from pacioli_erp import erp as erp_core

from . import settings
from .common import db, logger

# define the pacioli ledger tables and the ERP operational tables.
erp_core.define_tables(db)

# seed the base chart of accounts (Assets:Bank, Income:Sales, ...)
erp_core.init_accounts(db)

db.commit()

# In development, populate the database with demo data from a JSON fixture so the
# grids are not empty.  Idempotent; skipped under pytest so it never pollutes the
# in-process integration tests (which boot the app with PY4WEB_MODE=development).
if settings.MODE == "development" and "pytest" not in sys.modules:
    try:
        from .seed import load_dev_seed

        n = load_dev_seed(db)
        if n:
            logger.info("dev seed: created %s demo record(s)", n)
    except Exception as ex:  # never let seeding break app startup
        logger.warning("dev seed skipped: %s", ex)

