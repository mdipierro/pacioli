"""Tests for the ERP core posting engine (erp_core).

Skipped automatically if pydal is not installed."""

import decimal
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from pydal import DAL

    from pacioli_erp import erp as erp_core
    from pacioli_erp.erp import (
        DEFAULT_CURRENCY,
        ErpError,
        create_bill,
        create_invoice,
        create_item,
        create_party,
        create_payment,
        create_stock_move,
        post_journal,
    )

    HAS_PYDAL = True
except ImportError:
    HAS_PYDAL = False
    DEFAULT_CURRENCY = "USD"  # fallback so class definitions below still load

D = decimal.Decimal


@unittest.skipUnless(HAS_PYDAL, "pydal not installed")
class ErpCoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = DAL("sqlite://erp.sqlite", folder=self.dir)
        erp_core.setup(self.db)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass

    # -- helpers --
    def acc(self, name, commodity=DEFAULT_CURRENCY):
        return erp_core.account_balance(self.db, name)

    def acc_value(self, name, commodity=DEFAULT_CURRENCY):
        for a in erp_core.account_balance(self.db, name):
            if a["commodity"] == commodity:
                return D(a["value"])
        return D("0")

    def assert_ledger_balances(self):
        """The whole ledger must net to zero per commodity."""
        p = erp_core.build_pacioli(self.db)
        net = {}
        for top in ("Assets", "Liabilities", "Equity", "Income", "Expenses"):
            for c, v in p.accounts[top].wallet:
                net[c] = net.get(c, D("0")) + v
        for c, v in net.items():
            self.assertEqual(v, D("0"), "ledger does not net to zero for %r" % c)


class TestJournal(ErpCoreTest):
    def test_balanced_journal(self):
        txn = post_journal(
            self.db,
            [
                {"account": "Assets:Cash", "amount": 100},
                {"account": "Equity:Opening-Balances", "amount": -100},
            ],
            memo="opening",
        )
        self.assertTrue(txn)
        self.assertEqual(self.acc_value("Assets:Cash"), D("100"))
        self.assert_ledger_balances()

    def test_unbalanced_journal_raises(self):
        with self.assertRaises(ErpError):
            post_journal(
                self.db,
                [
                    {"account": "Assets:Cash", "amount": 100},
                    {"account": "Equity:Opening-Balances", "amount": -50},
                ],
            )


class TestPartiesItems(ErpCoreTest):
    def test_party_creates_control_accounts(self):
        pid = create_party(self.db, "Acme Corp", kind="both")
        party = self.db.party[pid]
        self.assertIn("Assets:AccountsReceivable", party.ar_account)
        names = set(r.name for r in self.db(self.db.account).select())
        self.assertIn(party.ar_account, names)
        self.assertIn(party.ap_account, names)

    def test_item_creates_inventory_account(self):
        iid = create_item(self.db, "WIDGET", cost="5.00")
        item = self.db.item[iid]
        self.assertEqual(item.cost, D("5.00"))
        names = set(r.name for r in self.db(self.db.account).select())
        self.assertIn("Assets:Inventory:WIDGET", names)


class TestPurchaseToStock(ErpCoreTest):
    def test_bill_with_item_receives_stock_and_posts_ap(self):
        vid = create_party(self.db, "WoodCo", kind="vendor")
        iid = create_item(self.db, "WOOD", cost="0")
        create_bill(
            self.db, vid,
            [{"item": iid, "qty": 100, "unit_price": "5.00"}],
            memo="buy wood",
        )
        # stock 100, moving-avg cost 5, inventory asset 500, AP -500
        self.assertEqual(erp_core.stock_on_hand(self.db, iid), D("100"))
        self.assertEqual(self.db.item[iid].cost, D("5"))
        self.assertEqual(self.acc_value("Assets:Inventory:WOOD"), D("500"))
        vendor = self.db.party[vid]
        self.assertEqual(self.acc_value(vendor.ap_account), D("-500"))
        self.assert_ledger_balances()

    def test_moving_average_cost(self):
        vid = create_party(self.db, "WoodCo", kind="vendor")
        iid = create_item(self.db, "WOOD", cost="0")
        create_bill(self.db, vid, [{"item": iid, "qty": 100, "unit_price": "5.00"}])
        create_bill(self.db, vid, [{"item": iid, "qty": 100, "unit_price": "7.00"}])
        # (100*5 + 100*7) / 200 = 6.00
        self.assertEqual(self.db.item[iid].cost, D("6"))
        self.assertEqual(self.acc_value("Assets:Inventory:WOOD"), D("1200"))


class TestSaleFlow(ErpCoreTest):
    def setUp(self):
        super().setUp()
        self.vid = create_party(self.db, "WoodCo", kind="vendor")
        self.cid = create_party(self.db, "Bob", kind="customer")
        self.iid = create_item(self.db, "TOY", cost="0", price="20")
        create_bill(self.db, self.vid, [{"item": self.iid, "qty": 100, "unit_price": "5.00"}])

    def test_invoice_revenue_cogs_and_stock(self):
        # sell 10 toys @ $20, tax 0
        create_invoice(
            self.db, self.cid,
            [{"item": self.iid, "qty": 10, "unit_price": "20.00"}],
        )
        cust = self.db.party[self.cid]
        self.assertEqual(self.acc_value(cust.ar_account), D("200"))   # AR
        self.assertEqual(self.acc_value("Income:Sales"), D("-200"))   # revenue
        self.assertEqual(self.acc_value("Expenses:COGS"), D("50"))    # 10 * 5
        self.assertEqual(self.acc_value("Assets:Inventory:TOY"), D("450"))  # 500-50
        self.assertEqual(erp_core.stock_on_hand(self.db, self.iid), D("90"))
        self.assert_ledger_balances()

    def test_invoice_with_tax(self):
        create_invoice(
            self.db, self.cid,
            [{"item": self.iid, "qty": 1, "unit_price": "100.00", "tax_rate": "0.10"}],
        )
        cust = self.db.party[self.cid]
        self.assertEqual(self.acc_value(cust.ar_account), D("110"))
        self.assertEqual(self.acc_value("Income:Sales"), D("-100"))
        self.assertEqual(self.acc_value("Liabilities:Tax"), D("-10"))
        self.assert_ledger_balances()

    def test_payment_clears_receivable(self):
        inv = create_invoice(
            self.db, self.cid,
            [{"item": self.iid, "qty": 10, "unit_price": "20.00"}],
        )
        create_payment(
            self.db, self.cid, "200.00", direction="in",
            allocations=[{"document": inv, "amount": "200.00"}],
        )
        cust = self.db.party[self.cid]
        self.assertEqual(self.acc_value(cust.ar_account), D("0"))
        self.assertEqual(self.acc_value("Assets:Bank"), D("200"))
        self.assertEqual(self.db.document[inv].allocated, D("200"))
        self.assert_ledger_balances()

    def test_gross_profit_via_pl(self):
        create_invoice(
            self.db, self.cid,
            [{"item": self.iid, "qty": 10, "unit_price": "20.00"}],
        )
        pl = erp_core.profit_and_loss(self.db)
        # revenue 200, COGS 50 -> net 150
        self.assertEqual(D(pl["total_income"]), D("200"))
        self.assertEqual(D(pl["total_expenses"]), D("50"))
        self.assertEqual(D(pl["net"]), D("150"))


class TestStockMoves(ErpCoreTest):
    def test_adjustment_posts_to_ledger(self):
        iid = create_item(self.db, "GADGET", cost="3.00")
        create_stock_move(self.db, iid, "adjust", qty="10", unit_cost="3.00")
        self.assertEqual(erp_core.stock_on_hand(self.db, iid), D("10"))
        self.assertEqual(self.acc_value("Assets:Inventory:GADGET"), D("30"))
        self.assertEqual(self.acc_value("Expenses:Inventory-Adjustment"), D("-30"))
        self.assert_ledger_balances()

    def test_reconcile_inventory(self):
        vid = create_party(self.db, "V", kind="vendor")
        iid = create_item(self.db, "X", cost="0")
        create_bill(self.db, vid, [{"item": iid, "qty": 50, "unit_price": "2.00"}])
        rec = erp_core.reconcile_inventory(self.db)
        self.assertTrue(rec["ok"], rec)


class TestIdempotency(ErpCoreTest):
    def test_invoice_external_id_idempotent(self):
        cid = create_party(self.db, "Carol")
        iid = create_item(self.db, "SVC", tracked=False)
        a = create_invoice(
            self.db, cid,
            [{"item": iid, "qty": 1, "unit_price": "10"}],
            external_id="ext-1",
        )
        b = create_invoice(
            self.db, cid,
            [{"item": iid, "qty": 1, "unit_price": "10"}],
            external_id="ext-1",
        )
        self.assertEqual(a, b)
        self.assertEqual(self.db(self.db.document.kind == "invoice").count(), 1)

    def test_payment_external_id_idempotent(self):
        cid = create_party(self.db, "Dan")
        a = create_payment(self.db, cid, "5.00", external_id="pay-1")
        b = create_payment(self.db, cid, "5.00", external_id="pay-1")
        self.assertEqual(a, b)


class TestEvents(ErpCoreTest):
    def test_events_emitted(self):
        cid = create_party(self.db, "Eve")
        iid = create_item(self.db, "SVC", tracked=False)
        create_invoice(self.db, cid, [{"item": iid, "qty": 1, "unit_price": "10"}])
        kinds = set(r.kind for r in self.db(self.db.event).select())
        self.assertIn("invoice.posted", kinds)


class TestTagsAndSearch(ErpCoreTest):
    def test_every_system_table_has_a_tag_table(self):
        for t in erp_core.SYSTEM_TABLES:
            self.assertIn("%s_tag_tags" % t, self.db.tables, "no tag table for %s" % t)

    def test_add_get_remove_tags(self):
        iid = create_item(self.db, "WIDGET")
        erp_core.add_tags(self.db, "item", iid, ["hardware", "sale"])
        self.assertEqual(sorted(erp_core.get_tags(self.db, "item", iid)), ["hardware", "sale"])
        erp_core.remove_tags(self.db, "item", iid, ["sale"])
        self.assertEqual(erp_core.get_tags(self.db, "item", iid), ["hardware"])

    def test_tags_on_create(self):
        vid = create_party(self.db, "WoodCo", kind="vendor", tags=["supplier", "timber"])
        self.assertEqual(sorted(erp_core.get_tags(self.db, "party", vid)), ["supplier", "timber"])

    def test_search_by_tag(self):
        a = create_item(self.db, "A", tags=["wood"])
        b = create_item(self.db, "B", tags=["metal"])
        create_item(self.db, "C", tags=["wood", "metal"])
        wood = sorted(r.sku for r in erp_core.search(self.db, "item", tags=["wood"]))
        self.assertEqual(wood, ["A", "C"])
        both = sorted(r.sku for r in erp_core.search(self.db, "item", tags=["wood", "metal"]))
        self.assertEqual(both, ["C"])
        either = sorted(r.sku for r in erp_core.search(self.db, "item", tags=["wood", "metal"], tag_mode="or"))
        self.assertEqual(either, ["A", "B", "C"])

    def test_search_by_text_identifier(self):
        create_item(self.db, "TOY-RED")
        create_item(self.db, "TOY-BLUE")
        create_item(self.db, "BOLT-1")
        toys = sorted(r.sku for r in erp_core.search(self.db, "item", text="TOY"))
        self.assertEqual(toys, ["TOY-BLUE", "TOY-RED"])

    def test_search_text_and_tag_combined(self):
        create_item(self.db, "TOY-RED", tags=["clearance"])
        create_item(self.db, "TOY-BLUE", tags=["new"])
        res = [r.sku for r in erp_core.search(self.db, "item", text="TOY", tags=["clearance"])]
        self.assertEqual(res, ["TOY-RED"])

    def test_lookup_by_id_and_identifier(self):
        iid = create_item(self.db, "GIZMO")
        self.assertEqual(erp_core.lookup(self.db, "item", iid).sku, "GIZMO")
        self.assertEqual(erp_core.lookup(self.db, "item", "GIZMO").id, iid)
        pid = create_party(self.db, "Acme")
        self.assertEqual(erp_core.lookup(self.db, "party", "Acme").id, pid)

    def test_record_summary(self):
        iid = create_item(self.db, "SKU9", tags=["x"])
        s = erp_core.record_summary(self.db, "item", self.db.item[iid])
        self.assertEqual(s["id"], iid)
        self.assertEqual(s["label"], "SKU9")
        self.assertEqual(s["tags"], ["x"])

    def test_document_tags(self):
        cid = create_party(self.db, "Cust")
        iid = create_item(self.db, "SVC", tracked=False)
        inv = create_invoice(
            self.db, cid, [{"item": iid, "qty": 1, "unit_price": "10"}], tags=["rush"]
        )
        self.assertIn("rush", erp_core.get_tags(self.db, "document", inv))


class TestVoid(ErpCoreTest):
    def setUp(self):
        super().setUp()
        self.vid = create_party(self.db, "WoodCo", kind="vendor")
        self.cid = create_party(self.db, "Bob", kind="customer")
        self.iid = create_item(self.db, "TOY", cost="0", price="20")
        create_bill(self.db, self.vid, [{"item": self.iid, "qty": 100, "unit_price": "5.00"}])

    def test_void_invoice_reverses_revenue_cogs_and_stock(self):
        cust = self.db.party[self.cid]
        ar0 = self.acc_value(cust.ar_account)
        inv = create_invoice(
            self.db, self.cid, [{"item": self.iid, "qty": 10, "unit_price": "20.00"}]
        )
        self.assertEqual(erp_core.stock_on_hand(self.db, self.iid), D("90"))
        erp_core.void_document(self.db, inv)
        # every balance returns to where it was before the invoice
        self.assertEqual(self.acc_value(cust.ar_account), ar0)
        self.assertEqual(self.acc_value("Income:Sales"), D("0"))
        self.assertEqual(self.acc_value("Expenses:COGS"), D("0"))
        self.assertEqual(erp_core.stock_on_hand(self.db, self.iid), D("100"))
        self.assertEqual(self.db.document[inv].status, "void")
        self.assert_ledger_balances()

    def test_void_invoice_with_tax(self):
        inv = create_invoice(
            self.db, self.cid,
            [{"item": self.iid, "qty": 1, "unit_price": "100.00", "tax_rate": "0.10"}],
        )
        self.assertEqual(self.acc_value("Liabilities:Tax"), D("-10"))
        erp_core.void_document(self.db, inv)
        self.assertEqual(self.acc_value("Liabilities:Tax"), D("0"))
        self.assert_ledger_balances()

    def test_void_bill_reverses_ap_and_stock(self):
        vendor = self.db.party[self.vid]
        ap_before = self.acc_value(vendor.ap_account)
        qty_before = erp_core.stock_on_hand(self.db, self.iid)
        bill = create_bill(
            self.db, self.vid, [{"item": self.iid, "qty": 20, "unit_price": "5.00"}]
        )
        self.assertEqual(erp_core.stock_on_hand(self.db, self.iid), qty_before + D("20"))
        erp_core.void_document(self.db, bill)
        self.assertEqual(self.acc_value(vendor.ap_account), ap_before)
        self.assertEqual(erp_core.stock_on_hand(self.db, self.iid), qty_before)
        self.assertEqual(self.db.document[bill].status, "void")
        self.assert_ledger_balances()

    def test_void_payment_restores_receivable_and_allocation(self):
        cust = self.db.party[self.cid]
        inv = create_invoice(
            self.db, self.cid, [{"item": self.iid, "qty": 10, "unit_price": "20.00"}]
        )
        pay = create_payment(
            self.db, self.cid, "200.00", direction="in",
            allocations=[{"document": inv, "amount": "200.00"}],
        )
        self.assertEqual(self.acc_value(cust.ar_account), D("0"))
        self.assertEqual(self.db.document[inv].allocated, D("200"))
        erp_core.void_document(self.db, pay)
        self.assertEqual(self.acc_value("Assets:Bank"), D("0"))
        self.assertEqual(self.acc_value(cust.ar_account), D("200"))  # receivable back
        self.assertEqual(self.db.document[inv].allocated, D("0"))    # allocation unwound
        self.assert_ledger_balances()

    def test_void_twice_raises(self):
        iid = create_item(self.db, "SVC", tracked=False)
        inv = create_invoice(self.db, self.cid, [{"item": iid, "qty": 1, "unit_price": "10"}])
        erp_core.void_document(self.db, inv)
        with self.assertRaises(ErpError):
            erp_core.void_document(self.db, inv)

    def test_void_standalone_stock_move(self):
        iid = create_item(self.db, "GADGET", cost="3.00")
        mv = create_stock_move(self.db, iid, "adjust", qty="10", unit_cost="3.00")
        self.assertEqual(self.acc_value("Assets:Inventory:GADGET"), D("30"))
        erp_core.void_stock_move(self.db, mv)
        self.assertEqual(erp_core.stock_on_hand(self.db, iid), D("0"))
        self.assertEqual(self.acc_value("Assets:Inventory:GADGET"), D("0"))
        self.assert_ledger_balances()


if __name__ == "__main__":
    unittest.main()
