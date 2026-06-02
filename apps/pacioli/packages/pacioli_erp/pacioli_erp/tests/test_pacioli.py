"""Extensive unit tests for the pacioli ledger parser and engine."""

import datetime
import io
import os
import sys
import unittest

# project root (parent of the pacioli_erp package) on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pacioli_erp.ledger import (  # noqa: E402
    Amount,
    Pacioli,
    Posting,
    Transaction,
    parse_amount,
    parse_date,
)


def load(text, **kw):
    p = Pacioli(**kw)
    p.load(io.StringIO(text))
    p.run()
    return p


def bal(p, name, commodity=""):
    return p.accounts[name].wallet[commodity]


class TestParseAmount(unittest.TestCase):
    def test_prefix_symbol(self):
        a = parse_amount("$1,000.00")
        self.assertEqual(a.value, 1000)
        self.assertEqual(a.asset, "$")

    def test_negative_before_symbol(self):
        self.assertEqual(parse_amount("-$1,000.00").value, -1000)

    def test_negative_after_symbol(self):
        self.assertEqual(parse_amount("$-1,000").value, -1000)

    def test_suffix_commodity(self):
        a = parse_amount("1,234.56 EUR")
        self.assertEqual(a.value, datetime_safe("1234.56"))
        self.assertEqual(a.asset, "EUR")

    def test_prefix_word_commodity(self):
        a = parse_amount("USD 100")
        self.assertEqual(a.asset, "USD")
        self.assertEqual(a.value, 100)

    def test_units(self):
        a = parse_amount("30 AAPL")
        self.assertEqual(a.value, 30)
        self.assertEqual(a.asset, "AAPL")

    def test_bare_number(self):
        a = parse_amount("10.00")
        self.assertEqual(a.value, 10)
        self.assertEqual(a.asset, "")

    def test_quoted_commodity(self):
        a = parse_amount('100 "My Fund"')
        self.assertEqual(a.asset, "My Fund")

    def test_default_commodity(self):
        a = parse_amount("10.00", default_commodity="USD")
        self.assertEqual(a.asset, "USD")

    def test_none_when_no_number(self):
        self.assertIsNone(parse_amount("hello"))
        self.assertIsNone(parse_amount(""))


def datetime_safe(s):  # decimal compare helper
    import decimal

    return decimal.Decimal(s)


class TestParseDate(unittest.TestCase):
    def test_dash(self):
        self.assertEqual(parse_date("2008-01-02"), datetime.date(2008, 1, 2))

    def test_slash(self):
        self.assertEqual(parse_date("2008/01/02"), datetime.date(2008, 1, 2))

    def test_dot(self):
        self.assertEqual(parse_date("2008.01.02"), datetime.date(2008, 1, 2))

    def test_short_with_default_year(self):
        self.assertEqual(parse_date("01/02", default_year=2020), datetime.date(2020, 1, 2))

    def test_invalid(self):
        self.assertIsNone(parse_date("not a date"))
        self.assertIsNone(parse_date("2008-13-40"))


class TestEngineBalancing(unittest.TestCase):
    def test_auto_balance_posting(self):
        p = load(
            "2020-01-01 * x\n"
            "  Assets:Cash      $100.00\n"
            "  Equity:Opening\n"
        )
        self.assertEqual(bal(p, "Assets:Cash", "$"), 100)
        self.assertEqual(bal(p, "Equity:Opening", "$"), -100)

    def test_unbalanced_raises(self):
        with self.assertRaises(RuntimeError):
            load("2020-01-01 * x\n  Assets:Cash  $10.00\n  Equity  $-5.00\n")

    def test_three_postings(self):
        p = load(
            "2020-01-01 * pay\n"
            "  Assets:Cash      -300 USD\n"
            "  Liabilities:Loan  125 USD\n"
            "  Expenses:Interest\n"
        )
        self.assertEqual(bal(p, "Expenses:Interest", "USD"), 175)

    def test_parent_rollup(self):
        p = load(
            "2020-01-01 * x\n"
            "  Assets:Bank:Checking   $50.00\n"
            "  Assets:Bank:Savings    $30.00\n"
            "  Equity:Opening\n"
        )
        self.assertEqual(bal(p, "Assets", "$"), 80)
        self.assertEqual(bal(p, "Assets:Bank", "$"), 80)

    def test_account_names_with_spaces(self):
        p = load(
            "2020-01-01 * x\n"
            "  Assets:A                 10.00\n"
            "  Equity:Opening balances\n"
        )
        self.assertEqual(bal(p, "Equity:Opening balances"), -10)


class TestStates(unittest.TestCase):
    def test_pending_state(self):
        p = load("2020-01-01 ! x\n  Assets:Cash  $5.00\n  Equity:O\n")
        self.assertTrue(p.ledger[0].pending)

    def test_cleared_state_and_code(self):
        p = load("2020-01-01 * (#123) Acme\n  Assets:Cash  $5.00\n  Equity:O\n")
        t = p.ledger[0]
        self.assertFalse(t.pending)
        self.assertEqual(t.code, "#123")
        self.assertEqual(t.info, "Acme")

    def test_effective_date(self):
        p = load("2020-01-01=2020-02-01 * x\n  Assets:Cash  $5.00\n  Equity:O\n")
        self.assertEqual(p.ledger[0].edate, datetime.date(2020, 2, 1))


class TestComments(unittest.TestCase):
    def test_full_line_comments(self):
        for ch in ";#%|*":
            p = load(
                "%s a full line comment\n2020-01-01 * x\n  A  1\n  B\n" % ch,
            )
            self.assertEqual(len(p.ledger), 1)

    def test_inline_comment(self):
        p = load("2020-01-01 * x\n  Assets:Cash  $5.00  ; note here\n  Equity:O\n")
        self.assertEqual(p.ledger[0].postings[0].comment, "note here")

    def test_block_comment(self):
        p = load(
            "comment\nthis is ignored\n2020-01-01 not parsed\nend comment\n"
            "2020-01-01 * x\n  A  1\n  B\n"
        )
        self.assertEqual(len(p.ledger), 1)


class TestTags(unittest.TestCase):
    def test_tags_on_header(self):
        p = load("2020-01-01 * x  ; :food:weekly:\n  A  1\n  B\n")
        self.assertEqual(p.ledger[0].tags, {"food", "weekly"})

    def test_tags_on_posting_comment(self):
        p = load("2020-01-01 * x\n  A  1  ; :food:\n  B\n")
        self.assertIn("food", p.ledger[0].tags)

    def test_key_value_metadata(self):
        p = load("2020-01-01 * x\n  A  1\n  B\n  ; Payee: Acme Corp\n")
        self.assertEqual(p.ledger[0].meta.get("Payee"), "Acme Corp")

    def test_pushtags_poptags(self):
        p = load(
            "pushtags client-1\n"
            "2020-01-01 * x\n  A  1\n  B\n"
            "poptags client-1\n"
            "2020-01-02 * y\n  A  1\n  B\n"
        )
        self.assertIn("client-1", p.ledger[0].tags)
        self.assertNotIn("client-1", p.ledger[1].tags)

    def test_apply_tag(self):
        p = load(
            "apply tag project-x\n"
            "2020-01-01 * x\n  A  1\n  B\n"
            "end apply tag\n"
            "2020-01-02 * y\n  A  1\n  B\n"
        )
        self.assertIn("project-x", p.ledger[0].tags)
        self.assertNotIn("project-x", p.ledger[1].tags)


class TestCostAndCapitalGains(unittest.TestCase):
    JOURNAL = (
        "2008-01-01 open Assets:Brokerage           USD\n"
        "2008-01-01 open Assets:Brokerage:AAPL      AAPL\n"
        "2008-01-01 open Expenses:Commissions\n"
        "2008-01-01 open Assets:Cash                USD\n"
        "2008-01-01 open Income:Gains\n"
        "\n"
        "2008-01-08 * Buy AAPL\n"
        "  Assets:Brokerage:AAPL    30 AAPL @ 100 USD\n"
        "  Assets:Brokerage\n"
        "\n"
        "2008-01-09 * Buy more AAPL\n"
        "  Assets:Brokerage:AAPL    50 AAPL @ 120 USD\n"
        "  Assets:Brokerage\n"
        "\n"
        "2008-02-28 * Sell AAPL\n"
        "  Assets:Brokerage:AAPL    -40 AAPL @ 140 USD\n"
        "  Assets:Brokerage\n"
        "  (Income:Gains)           BOOK AAPL\n"
    )

    def test_units_held(self):
        p = load(self.JOURNAL)
        self.assertEqual(bal(p, "Assets:Brokerage:AAPL", "AAPL"), 40)

    def test_fifo_capital_gains(self):
        # sold 40 @ 140 = 5600; FIFO cost 30@100 + 10@120 = 4200; gain 1400
        p = load(self.JOURNAL)
        self.assertEqual(bal(p, "Income:Gains", "USD"), -1400)

    def test_total_cost_at_at(self):
        p = load(
            "2020-01-01 * buy\n"
            "  Assets:Stock    10 FOO @@ 250 USD\n"
            "  Assets:Cash\n"
        )
        # @@ total 250 over 10 units -> 25/unit, cash -250
        self.assertEqual(bal(p, "Assets:Cash", "USD"), -250)
        self.assertEqual(bal(p, "Assets:Stock", "FOO"), 10)


class TestAssertions(unittest.TestCase):
    def test_passing_assertion(self):
        p = load(
            "2020-01-01 * x\n"
            "  Assets:Cash      $100.00\n"
            "  Equity:Opening   $-100.00 = $-100.00\n"
        )
        self.assertEqual(bal(p, "Equity:Opening", "$"), -100)

    def test_failing_assertion(self):
        with self.assertRaises(RuntimeError):
            load(
                "2020-01-01 * x\n"
                "  Assets:Cash      $100.00\n"
                "  Equity:Opening   $-100.00 = $-1.00\n"
            )


class TestVirtualPostings(unittest.TestCase):
    def test_unbalanced_virtual_excluded_from_balance(self):
        # (Account) does not need to balance
        p = load(
            "2020-01-01 * x\n"
            "  Assets:Cash      $100.00\n"
            "  Equity:Opening   $-100.00\n"
            "  (Budget:Cash)    $100.00\n"
        )
        self.assertEqual(bal(p, "Budget:Cash", "$"), 100)

    def test_balanced_virtual_included(self):
        p = load(
            "2020-01-01 * x\n"
            "  Assets:Cash      $100.00\n"
            "  [Equity:Opening] $-100.00\n"
        )
        self.assertEqual(bal(p, "Equity:Opening", "$"), -100)


class TestDirectives(unittest.TestCase):
    def test_open_with_commodity_constraint(self):
        with self.assertRaises(RuntimeError):
            load(
                "2020-01-01 open Assets:Cash USD\n"
                "2020-01-02 * x\n"
                "  Assets:Cash   10 EUR\n"
                "  Equity:O\n"
            )

    def test_close_account(self):
        with self.assertRaises(RuntimeError):
            load(
                "2020-01-01 open Assets:Cash\n"
                "2020-01-05 close Assets:Cash\n"
                "2020-01-10 * x\n"
                "  Assets:Cash   $10.00\n"
                "  Equity:O\n"
            )

    def test_pad_and_balance(self):
        p = load(
            "2020-01-01 open Assets:Cash USD\n"
            "2020-01-01 open Equity:Opening-Balances USD\n"
            "2020-01-01 pad Assets:Cash Equity:Opening-Balances\n"
            "2020-01-02 balance Assets:Cash 2000 USD\n"
        )
        self.assertEqual(bal(p, "Assets:Cash", "USD"), 2000)
        self.assertEqual(bal(p, "Equity:Opening-Balances", "USD"), -2000)

    def test_failing_balance_check(self):
        with self.assertRaises(RuntimeError):
            load(
                "2020-01-01 open Assets:Cash USD\n"
                "2020-01-02 * x\n  Assets:Cash  100 USD\n  Equity:O\n"
                "2020-01-03 balance Assets:Cash 999 USD\n"
            )

    def test_default_commodity_directive(self):
        p = load("D $1000.00\n2020-01-01 * x\n  Assets:Cash  100\n  Equity:O\n")
        self.assertEqual(bal(p, "Assets:Cash", "$"), 100)

    def test_account_directive(self):
        p = load(
            "account Assets:Cash\n"
            "2020-01-01 * x\n  Assets:Cash  $5.00\n  Equity:O\n"
        )
        self.assertIn("Assets:Cash", p.accounts)

    def test_price_directive(self):
        p = load("P 2020-01-01 AAPL $500.00\n2020-01-02 * x\n  A  1\n  B\n")
        self.assertEqual(len(p.prices), 1)
        self.assertEqual(p.prices[0][1], "AAPL")

    def test_year_directive_short_dates(self):
        # not a hard requirement of ledger core but supported
        p = Pacioli()
        p.load(io.StringIO("Y 2021\n01/05 * x\n  A  1\n  B\n"))
        p.run()
        self.assertEqual(p.ledger[0].date, datetime.date(2021, 1, 5))


class TestStrictMode(unittest.TestCase):
    def test_strict_requires_declaration(self):
        with self.assertRaises(RuntimeError):
            load(
                "2020-01-01 * x\n  Assets:Cash  $5.00\n  Equity:O\n",
                strict=True,
            )

    def test_nonstrict_autocreates(self):
        p = load("2020-01-01 * x\n  Assets:Cash  $5.00\n  Equity:O\n")
        self.assertIn("Assets:Cash", p.accounts)


class TestSerializerRoundTrip(unittest.TestCase):
    def _round_trip(self, path):
        p = Pacioli()
        p.load(path)
        p.run()
        buf = io.StringIO()
        p.save(buf)
        p2 = Pacioli()
        p2.load(io.StringIO(buf.getvalue()))
        p2.run()
        for name in p.accounts:
            self.assertEqual(
                dict(p.accounts[name].wallet),
                dict(p2.accounts[name].wallet),
                "mismatch on %s" % name,
            )

    def test_demo_round_trip(self):
        pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        demo = os.path.join(pkg, "examples", "demo.ledger")
        self.assertTrue(os.path.exists(demo))
        self._round_trip(demo)


class TestApi(unittest.TestCase):
    def test_programmatic_api(self):
        p = Pacioli()
        p.add_account("Assets:Cash", "De", "USD")
        p.add_account("Equity", "Cr", "USD")
        p.ledger.append(
            Transaction(
                "2008-10-20",
                "info",
                postings=[
                    Posting("Assets:Cash", amount=Amount(100, "USD")),
                    Posting("Equity"),
                ],
            )
        )
        p.finalize()
        p.run()
        self.assertEqual(p.accounts["Assets"].wallet["USD"], 100)
        self.assertEqual(p.accounts["Equity"].wallet["USD"], -100)


if __name__ == "__main__":
    unittest.main()
