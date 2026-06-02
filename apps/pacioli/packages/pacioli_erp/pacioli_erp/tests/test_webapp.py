"""Tests for the single page editor web app."""

import json
import os
import sys
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pacioli_erp import editor as webapp  # noqa: E402

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestAnalyze(unittest.TestCase):
    def test_valid(self):
        text = "2020-01-01 * x\n  Assets:Cash  $100.00\n  Equity:Opening\n"
        result = webapp.analyze(text)
        self.assertTrue(result["ok"])
        names = {row["name"] for row in result["balance"]}
        self.assertIn("Assets:Cash", names)

    def test_invalid_reports_error(self):
        text = "2020-01-01 * x\n  Assets:Cash  $10.00\n  Equity  $-5.00\n"
        result = webapp.analyze(text)
        self.assertFalse(result["ok"])
        self.assertIn("Unbalanced", result["error"])

    def test_page_renders(self):
        page = webapp.PAGE % {"filename": "demo.ledger"}
        self.assertIn('id="editor"', page)
        self.assertIn("Trial balance", page)


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        webapp.Handler.filename = os.path.join(PKG, "examples", "demo.ledger")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def test_index(self):
        r = urllib.request.urlopen(self._url("/"))
        self.assertEqual(r.status, 200)
        self.assertIn(b"<textarea", r.read())

    def test_load(self):
        data = json.load(urllib.request.urlopen(self._url("/api/load")))
        self.assertGreater(len(data["text"]), 0)

    def test_validate(self):
        text = "2020-01-01 * x\n  A  1\n  B\n"
        req = urllib.request.Request(
            self._url("/api/validate"),
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        data = json.load(urllib.request.urlopen(req))
        self.assertTrue(data["ok"])


if __name__ == "__main__":
    unittest.main()
