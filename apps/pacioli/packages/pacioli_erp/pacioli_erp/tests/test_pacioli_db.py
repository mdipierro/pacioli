"""Tests for the pydal storage backend (pacioli_db).

Skipped automatically if pydal is not installed."""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pacioli_erp.ledger import Pacioli  # noqa: E402

try:
    from pacioli_erp import storage as pacioli_db

    HAS_PYDAL = True
except ImportError:
    HAS_PYDAL = False

# the package directory; the example ledger lives under examples/
PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

JOURNAL = (
    "2008-01-01 open Assets:Brokerage           USD\n"
    "2008-01-01 open Assets:Brokerage:AAPL      AAPL\n"
    "2008-01-01 open Assets:Cash                USD\n"
    "2008-01-01 open Equity:Opening-Balances    USD\n"
    "2008-01-01 open Income:Gains\n"
    "\n"
    "2008-01-01 pad Assets:Cash Equity:Opening-Balances\n"
    "2008-01-02 balance Assets:Cash 10000 USD\n"
    "\n"
    "2008-01-08 * (#1) Buy AAPL  ; :investment:\n"
    "  Assets:Brokerage:AAPL    30 AAPL @ 100 USD\n"
    "  Assets:Cash\n"
)


@unittest.skipUnless(HAS_PYDAL, "pydal not installed")
class TestPydalRoundTrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = pacioli_db.connect("sqlite://test.sqlite", folder=self.dir)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass

    def _balances(self, p):
        return {n: dict(a.wallet) for n, a in p.accounts.items()}

    def test_define_tables(self):
        for t in ("account", "txn", "posting", "chk", "price"):
            self.assertIn(t, self.db.tables)

    def test_pacioli_to_db_counts(self):
        p = Pacioli()
        p.load(io.StringIO(JOURNAL))
        p.run()
        pacioli_db.pacioli_to_db(p, self.db)
        self.assertEqual(self.db(self.db.txn).count(), 1)
        self.assertEqual(self.db(self.db.chk).count(), 1)
        self.assertGreaterEqual(self.db(self.db.posting).count(), 2)
        self.assertGreater(self.db(self.db.account).count(), 0)

    def test_db_round_trip_balances(self):
        p1 = Pacioli()
        p1.load(io.StringIO(JOURNAL))
        p1.run()
        pacioli_db.pacioli_to_db(p1, self.db)

        p2 = pacioli_db.db_to_pacioli(self.db)
        p2.finalize()
        p2.run()
        self.assertEqual(self._balances(p1), self._balances(p2))

    def test_metadata_preserved(self):
        p1 = Pacioli()
        p1.load(io.StringIO(JOURNAL))
        p1.run()
        pacioli_db.pacioli_to_db(p1, self.db)
        p2 = pacioli_db.db_to_pacioli(self.db)
        p2.finalize()
        txn = [t for t in p2.ledger if getattr(t, "code", None) == "#1"][0]
        self.assertIn("investment", txn.tags)
        self.assertEqual(txn.postings[0].at.value, 100)

    def test_file_to_db_to_file(self):
        demo = os.path.join(PKG, "examples", "demo.ledger")
        p1 = pacioli_db.file_to_db(demo, self.db)
        p1.run()
        out = os.path.join(self.dir, "out.ledger")
        pacioli_db.db_to_file(self.db, out)
        p3 = Pacioli()
        p3.load(out)
        p3.run()
        self.assertEqual(self._balances(p1), self._balances(p3))

    def test_dbsource_via_load(self):
        p1 = Pacioli()
        p1.load(io.StringIO(JOURNAL))
        p1.run()
        pacioli_db.DBSource(self.db).dump_from(p1)

        p2 = Pacioli()
        p2.load(pacioli_db.DBSource(self.db))  # load through DataSource interface
        p2.run()
        self.assertEqual(self._balances(p1), self._balances(p2))


if __name__ == "__main__":
    unittest.main()
