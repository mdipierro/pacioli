"""In-process smoke test of the py4web ERP app via WSGI (no socket).

Boots the app with py4web's loader and drives the REST API end to end:
party -> item -> bill (stock in) -> invoice (sale) -> payment -> reports.
Skipped if py4web is not installed.
"""

import io
import json
import os
import sys
import tempfile
import unittest

# This is an integration test for the py4web app, which is at <repo>/apps/pacioli.
# The package may be vendored inside that app (apps/pacioli/packages/pacioli_erp) or
# live standalone (e.g. when distributed via PyPI), so locate the apps/ folder by
# walking up; if it is not found, the app is not present and the test is skipped.
def _find_apps_root(start):
    d = start
    while True:
        if os.path.isdir(os.path.join(d, "apps", "pacioli")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


BASE = _find_apps_root(os.path.dirname(os.path.abspath(__file__)))
if BASE:
    sys.path.insert(0, BASE)

try:
    import py4web  # noqa: F401

    HAS_PY4WEB = True
except ImportError:
    HAS_PY4WEB = False


@unittest.skipUnless(HAS_PY4WEB and BASE, "py4web not installed or app not present")
class TestErpAppWsgi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["PY4WEB_MODE"] = "development"  # API open, relaxed auth
        # fresh database for the test run
        dbdir = os.path.join(BASE, "apps", "pacioli", "databases")
        for f in os.listdir(dbdir) if os.path.isdir(dbdir) else []:
            if f.endswith(".table") or f.endswith(".db"):
                try:
                    os.remove(os.path.join(dbdir, f))
                except OSError:
                    pass
        import py4web.core as core

        cls.app = core.wsgi(
            apps_folder=os.path.join(BASE, "apps"),
            app_names="pacioli",
            yes=True,
            debug=False,
            logging_level=40,
            mode="development",
            dashboard_mode="none",
            password_file=os.path.join(tempfile.mkdtemp(), "password.txt"),
        )

    def _request(self, method, path, body=None):
        query = ""
        if "?" in path:
            path, query = path.split("?", 1)
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": sys.stderr,
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "QUERY_STRING": query,
        }
        if body is not None:
            data = json.dumps(body).encode()
            environ["wsgi.input"] = io.BytesIO(data)
            environ["CONTENT_LENGTH"] = str(len(data))
            environ["CONTENT_TYPE"] = "application/json"

        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers

        chunks = self.app(environ, start_response)
        payload = b"".join(chunks)
        status = int(captured["status"].split()[0])
        try:
            data = json.loads(payload.decode() or "null")
        except ValueError:
            data = payload.decode()
        return status, data

    def _request_raw(self, method, path, body=None):
        """Like _request but returns (status, raw_bytes, headers) without
        decoding — needed for binary responses such as PDFs."""
        query = ""
        if "?" in path:
            path, query = path.split("?", 1)
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": sys.stderr,
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "QUERY_STRING": query,
        }
        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers

        chunks = self.app(environ, start_response)
        payload = b"".join(chunks)
        status = int(captured["status"].split()[0])
        headers = dict(captured.get("headers") or [])
        return status, payload, headers

    def get(self, path):
        return self._request("GET", path, None)

    def post(self, path, body):
        return self._request("POST", path, body)

    # -- the test ----------------------------------------------------------

    def test_full_store_flow(self):
        # health
        status, data = self.get("/pacioli/api/health")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])

        # create a vendor and a customer
        status, vend = self.post("/pacioli/api/parties", {"name": "WoodCo", "kind": "vendor"})
        self.assertEqual(status, 200, vend)
        vid = vend["party"]["id"]
        status, cust = self.post(
            "/pacioli/api/parties",
            {
                "name": "Bob",
                "kind": "customer",
                "email": "bob@example.com",
                "phone": "+1 555 0100",
                "address": "12 Oak St\nSpringfield, IL 62704",
            },
        )
        cid = cust["party"]["id"]
        # contact fields round-trip
        self.assertEqual(cust["party"]["email"], "bob@example.com")
        self.assertEqual(cust["party"]["address"], "12 Oak St\nSpringfield, IL 62704")

        # create a stocked item
        status, item = self.post(
            "/pacioli/api/items", {"sku": "TOY-1", "name": "Wooden toy", "price": "20"}
        )
        self.assertEqual(status, 200, item)
        iid = item["item"]["id"]

        # vendor bill -> receive 100 @ $5 (stock in, AP up)
        status, bill = self.post(
            "/pacioli/api/bills",
            {"party": vid, "lines": [{"item": iid, "qty": 100, "unit_price": "5.00"}]},
        )
        self.assertEqual(status, 200, bill)

        # stock should now be 100
        status, stock = self.get("/pacioli/api/stock")
        toy = [s for s in stock["stock"] if s["sku"] == "TOY-1"][0]
        self.assertEqual(toy["qty"], "100.000000")
        self.assertEqual(toy["value"], "500.00")

        # sell 10 @ $20 with 10% tax
        status, inv = self.post(
            "/pacioli/api/invoices",
            {
                "party": cid,
                "lines": [{"item": iid, "qty": 10, "unit_price": "20.00", "tax_rate": "0.10"}],
            },
        )
        self.assertEqual(status, 200, inv)
        invoice_id = inv["invoice"]["id"]
        self.assertEqual(inv["invoice"]["total"], "220.000000")  # 200 + 20 tax
        # the invoice snapshots the party name + address at creation time
        self.assertEqual(inv["invoice"]["party_name"], "Bob")
        self.assertEqual(inv["invoice"]["party_address"], "12 Oak St\nSpringfield, IL 62704")

        # PDF export: 200 + %PDF when fpdf2 is available, else a clean 501
        status, body, headers = self._request_raw(
            "GET", "/pacioli/api/invoices/%d/pdf" % invoice_id
        )
        try:
            import fpdf  # noqa: F401

            self.assertEqual(status, 200, body[:200])
            self.assertEqual(headers.get("Content-Type"), "application/pdf")
            self.assertTrue(body.startswith(b"%PDF"), body[:20])
        except ImportError:
            self.assertEqual(status, 501)

        # customer pays 220, allocated to the invoice
        status, pay = self.post(
            "/pacioli/api/payments",
            {
                "party": cid,
                "amount": "220.00",
                "direction": "in",
                "allocations": [{"document": invoice_id, "amount": "220.00"}],
            },
        )
        self.assertEqual(status, 200, pay)

        # reports: P&L gross profit = 200 revenue - 50 COGS = 150
        status, pl = self.get("/pacioli/api/reports/profit_and_loss")
        self.assertEqual(status, 200, pl)
        from decimal import Decimal as Dn

        self.assertEqual(Dn(pl["total_income"]), Dn("200"))
        self.assertEqual(Dn(pl["total_expenses"]), Dn("50"))
        self.assertEqual(Dn(pl["net"]), Dn("150"))

        # the ledger must reconcile (inventory operational == ledger)
        status, rec = self.get("/pacioli/api/stock/reconcile")
        self.assertTrue(rec["ok"], rec)

        # trial balance: AR cleared to 0, bank 220, tax owed 20
        status, tb = self.get("/pacioli/api/reports/trial_balance")
        flat = {a["account"]: a["amounts"] for a in tb["trial_balance"]}
        self.assertIn("Assets:Bank", flat)
        bank = [x for x in flat["Assets:Bank"] if x["commodity"] == "USD"][0]
        from decimal import Decimal as Dn

        self.assertEqual(Dn(bank["value"]), Dn("220"))

        # events recorded
        status, ev = self.get("/pacioli/api/events")
        kinds = {e["kind"] for e in ev["events"]}
        self.assertTrue({"bill.posted", "invoice.posted", "payment.received"} <= kinds)

    def test_tags_and_search_api(self):
        # create tagged records
        status, it = self.post(
            "/pacioli/api/items",
            {"sku": "SRCH-1", "name": "Searchable widget", "tags": ["hardware", "promo"]},
        )
        self.assertEqual(status, 200, it)
        self.assertIn("hardware", it["item"]["tags"])
        iid = it["item"]["id"]

        status, p = self.post(
            "/pacioli/api/parties", {"name": "TagCo", "kind": "vendor", "tags": ["vip"]}
        )
        self.assertEqual(status, 200, p)
        self.assertEqual(p["party"]["tags"], ["vip"])

        # search items by tag
        status, r = self.get("/pacioli/api/search/item?tag=hardware")
        self.assertEqual(status, 200, r)
        skus = {row["label"] for row in r["results"]}
        self.assertIn("SRCH-1", skus)

        # search items by text identifier
        status, r = self.get("/pacioli/api/search/item?q=SRCH")
        self.assertIn("SRCH-1", {row["label"] for row in r["results"]})

        # manage tags via the tags endpoint
        status, t = self.post("/pacioli/api/tags/item/%d" % iid, {"add": ["clearance"], "remove": ["promo"]})
        self.assertEqual(status, 200, t)
        self.assertIn("clearance", t["tags"])
        self.assertNotIn("promo", t["tags"])

        # unknown table -> 404
        status, _ = self.get("/pacioli/api/search/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
