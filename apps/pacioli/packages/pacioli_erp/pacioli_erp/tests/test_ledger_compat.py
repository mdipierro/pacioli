"""Compatibility tests against authoritative journals harvested from the
ledger-cli baseline test-suite (test/baseline/*.test).

ledger is authoritative: every harvested journal must parse and run, and the
account totals pacioli computes must match the numbers ledger reports with its
`balance` command.  The expected numbers below were copied verbatim from the
corresponding ledger baseline test output.
"""

import decimal
import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pacioli_erp.ledger import Pacioli  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger_fixtures")
D = decimal.Decimal


def run_fixture(name):
    p = Pacioli()
    p.load(os.path.join(FIX, name + ".ledger"))
    p.run()
    return p


# expected[name] = {account: {commodity: value}} taken from ledger's `bal` output
EXPECTED = {
    # test/baseline/cmd-balance.test  (bal --flat -E)
    "cmd-balance": {
        "Assets:A": {"": "0"},
        "Assets:B": {"": "5"},
        "Assets:C": {"": "5"},
        "Equity:Opening balances": {"": "-10"},
        "Liabilities:A": {"": "0"},
    },
    # test/baseline/opt-flat.test  (bal --flat)
    "opt-flat": {
        "Expenses:Books": {"$": "10"},
        "Expenses:One:Books": {"$": "10"},
        "Expenses:One:Two:Books": {"$": "10"},
        "Expenses:One:Two:Three:Books": {"$": "10"},
        "Income:Books": {"$": "-10"},
        "Income:One:Books": {"$": "-10"},
        "Income:One:Two:Books": {"$": "-10"},
        "Income:One:Two:Three:Books": {"$": "-10"},
    },
    # test/baseline/opt-no-total.test  (bal --no-total) -- exercises @ cost
    "opt-no-total": {
        "Assets:Investments:Vanguard:VMMXX": {"VMMXX": "0.350"},
        "Income:Dividends:Vanguard:VMMXX": {"$": "-0.35"},
    },
}


class TestLedgerFixturesParse(unittest.TestCase):
    """Every harvested journal must parse and run without error."""

    def test_all_fixtures_parse(self):
        fixtures = sorted(glob.glob(os.path.join(FIX, "*.ledger")))
        self.assertTrue(fixtures, "no fixtures harvested")
        for path in fixtures:
            with self.subTest(fixture=os.path.basename(path)):
                p = Pacioli()
                p.load(path)
                p.run()  # raises on any unbalanced transaction


class TestLedgerAuthoritativeBalances(unittest.TestCase):
    """pacioli's totals must equal the numbers ledger reports."""

    def test_balances_match_ledger(self):
        for name, accounts in EXPECTED.items():
            p = run_fixture(name)
            for account, commodities in accounts.items():
                self.assertIn(account, p.accounts, "%s missing in %s" % (account, name))
                for commodity, value in commodities.items():
                    got = p.accounts[account].wallet[commodity]
                    with self.subTest(fixture=name, account=account, commodity=commodity):
                        self.assertEqual(
                            got, D(value),
                            "%s/%s/%r: pacioli=%s ledger=%s"
                            % (name, account, commodity, got, value),
                        )


class TestNonCostLedgersNetToZero(unittest.TestCase):
    """Non-cost ledgers must net to zero per commodity (double-entry invariant
    that ledger guarantees)."""

    NON_COST = ["cmd-balance", "opt-flat", "opt-empty", "opt-subtotal"]

    def test_net_zero(self):
        tops = ("Assets", "Liabilities", "Equity", "Income", "Expenses")
        for name in self.NON_COST:
            p = run_fixture(name)
            net = {}
            for top in tops:
                for commodity, value in p.accounts[top].wallet:
                    net[commodity] = net.get(commodity, D(0)) + value
            for commodity, value in net.items():
                with self.subTest(fixture=name, commodity=commodity):
                    self.assertEqual(value, D(0), "%s does not net to zero" % name)


if __name__ == "__main__":
    unittest.main()
