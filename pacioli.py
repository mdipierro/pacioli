#!/usr/bin/env python
"""
Author: Massimo Di Pierro <mdipierro@cs.depaul.edu>
License: BSD <http://opensource.org/licenses/BSD-3-Clause>
Created on: 2013-02-20

This program is named after Luca Pacioli, inventor of accounting and double-entry bookkeeping.
Pacioli maintained that business people must insist on justice, honour, and truth.

The program contains three parts:
1) An implementation of double-entry bookkeeping 
   - Assets, Liabilities, Equity, Income, Expenses 
   - Computations of Balance Sheet and Profit&Losses
   - Automatic computation FIFO capital-gains
   - API
2) Functions to read and write a general ledger in the beancount format (http://furius.ca/beancount/)
   - list of accounts
   - transactions with multiple postings
   - tags
   - checks
   - support for multiple files and partial output
3) Reporting
   - The output of the program is in HTML
   - Reporting in Latex/PDF (WORK IN PROGRESS)
   - Reporting in JSON (WORK IN PROGRESS)
   - Charting (WORK IN PROGRESS)

This program uses a single file to store the ledger. This is only appropriate for small bussinesses.
Our benchmark indicates that the program requires 0.0004 seconds/transactions. Therefore if a business processes about 1000 transaction/day, the system can process one year of trasactions in about 2 minutes. Additional time is required to generate reports.

Consider a simple business that buys wood and sells wood toys. 

<file: toystore.ledger>
; define your accounts
2000-01-01 open Assets:Cash
2000-01-01 open Assets:AccountsReceivable
2000-01-01 open Liabilities:Loan
2000-01-01 open Liabilities:AccountPayable
2000-01-01 open Equity:Opening-Balances
2000-01-01 open Income:ToySale
2000-01-01 open Income:Capital-Gains
2000-01-01 open Expense:ShippingCosts
2000-01-01 open Expense:Monthly:LoanInterest

; put initial money in cash account
2013-01-01 pad Assets:Cash Equity:Opening-Balances
2013-01-01 balance Assets:Cash 2000 USD

; transactions
2013-01-01 * Receive a loan
    Assets:Cash                   +10000 USD
    Liabilities:Loan              -10000 USD

2013-02-01 * Pay load interest
    Assets:Cash                   -300 USD
    Liabilities:Loan              +125 USD
    Expense:Monthly:LoanInterest           ; automatically computed as 300-125 = 275 USD

2013-03-01 * buy wood
    Expense:ShippingCosts          500 USD
    Assets:Cash                            ; automatically computes as -(9*1000+500) USD
pushtags client-0001
2013-04-01 * first toy sale (cash transaction)
    Income:ToySale                -230 USD ; $230 from client #0001
    Assets:Cash                            ; automatically computed as 230 USD
poptags client-0001

2013-04-01 balance Assets:Cash     2430 USD ; 2000+10000-9*1000-500+230 = 2430 USD

; read more about AccountsReceivable
; http://www.investopedia.com/terms/a/accountsreceivable.asp

pushtags client-0002
2013-04-02 * second toy sale (invoice)
    Income:ToySale                -230 USD ; $230 billed to client #0001
    Assets:AccountsReceivable              ; automatically computed as 230 USD
    tags x y z

2013-04-10 * second toy sale (payment received after one week)
    Assets:AccountsReceivable     -230 USD ; $230 received from client #0001
    Assets:Cash                            ; automatically computed as 230 USD
poptags client-0002

2013-04-10 balance Assets:Cash     2660 USD ; 2000+10000-9*1000-500+230*2 = 2460 USD
<file>

pacioli.py -i toystore.ledger > oystore.balance

<file toystore.balance>
banchmark: 4.11e-04 sec/transaction
Assets                        : 2660.0 USD
Assets:AccountsReceivable     : 0.0 USD
Assets:Cash                   : 2660.0 USD
Equity                        : -2000.0 USD
Equity:Opening-Balances       : -2000.0 USD
Expense                       : 675.0 USD
Expense:Monthly               : 175.0 USD
Expense:Monthly:LoanInterest  : 175.0 USD
Expense:ShippingCosts         : 500.0 USD
Expenses                      : 
Income                        : -460.0 USD
Income:Capital-Gains          : 
Income:ToySale                : -460.0 USD
Liabilities                   : -9875.0 USD
Liabilities:AccountPayable    : 
Liabilities:Loan              : -9875.0 USD
</file>
"""

import argparse
import collections
import copy
import datetime
import decimal
import html
import os

R = decimal.Decimal
ZERO = R("0.0")
BEGIN_TIME = datetime.date(2000, 1, 1)
END_TIME = datetime.date(2999, 12, 31)

__all__ = ("Wallet", "Amount", "Transaction", "Check", "Posting", "Pacioli")


def err(msg, *args):
    raise RuntimeError(msg % args)


class Amount:
    __slots__ = ("value", "asset")

    def __init__(self, value, asset="USD"):
        self.value = R(value)
        self.asset = asset

    def __iter__(self):
        yield (self.asset, self.value)

    def __str__(self):
        return f"{self.value:+} {self.asset}"


class Wallet:
    def __init__(self):
        self.assets = {}

    def add(self, other):
        for name, value in other:
            self.assets[name] = self.assets.get(name, ZERO) + value

    def sub(self, other):
        for name, value in other:
            self.assets[name] = self.assets.get(name, ZERO) - value

    def __iter__(self):
        for item in self.assets.items():
            yield item

    def __getitem__(self, name):
        return self.assets.get(name, ZERO)

    def __len__(self):
        return len(self.assets)

    def value(self):
        return sum(abs(value) for _, value in self)

    def __str__(self):
        return " ".join(f"{value:+} {name}" for (name, value) in self)


class Account:
    __slots__ = ("name", "assets", "wallet", "open_date", "close_date")

    def __init__(self, name, open_date=BEGIN_TIME, assets=None):
        self.name = name
        self.open_date = open_date
        self.close_date = END_TIME
        self.assets = assets
        self.wallet = Wallet()

    def __str__(self):
        return str(self.wallet)


class Posting:
    __slots__ = ("name", "amount", "at", "comment", "book")

    def __init__(self, name, amount=None, at=None, comment=None, book=False):
        self.name = name
        self.amount = amount
        self.at = at
        self.comment = comment
        self.book = book


class Transaction:
    __slots__ = ("date", "info", "postings", "tags", "id", "pending")

    def __init__(self, date, info, postings=None, tags=None, id=None, pending=False):
        self.date = parse_date(date) if isinstance(date, str) else date
        self.info = info
        self.postings = postings or []
        self.tags = tags or set()
        self.id = id
        self.pending = pending

    def run(self, p):
        pending_balance = None  # transaction balance
        wallet_balance = Wallet()  # transaction balance
        pending_gains = None  # transaction capital gains
        wallet_gains = Wallet()  # transaction capital gains
        for posting in self.postings:
            if posting.book == True:
                if pending_gains:
                    err("Ambiguous booking")
                pending_gains = posting
                continue
            name = posting.name
            if posting.amount is not None:
                other_value, other_asset = posting.amount.value, posting.amount.asset
                assets = p.accounts[name].assets
                if assets and other_asset not in assets:
                    err("Invalid Currency/Asset in %s" % name)
                elif posting.at:
                    atvalue, atasset = posting.at.value, posting.at.asset
                    if other_value > 0:
                        print(other_asset, other_value, atvalue, atasset)
                        p.fifos[other_asset].append((other_value, atvalue, atasset))
                    elif other_value < 0:
                        amount, profit, fifo = -other_value, 0.0, p.fifos[other_asset]
                        while amount and fifo:
                            (available, value, asset) = fifo[0]
                            delta = min(amount, available)
                            amount -= delta
                            wallet_gains.add(Amount(delta * atvalue, atasset))
                            wallet_gains.add(Amount(-delta * value, asset))
                            if delta == available:
                                fifo = fifo[1:]
                            else:
                                fifo[0] = (available - delta, value, asset)

                    value, asset = other_value * atvalue, atasset
                else:
                    value, asset = other_value, other_asset
                print(value, asset)
                wallet_balance.add(Amount(-value, asset))
                p.tree_add(name, other_value, other_asset)
            elif not pending_balance:
                pending_balance = posting
            else:
                err("Incomplete Transaction: %s" % self.info)
        if pending_balance:
            self.postings.remove(pending_balance)
            for asset, value in wallet_balance:
                if value:
                    self.postings.append(
                        Posting(pending_balance.name, Amount(value, asset))
                    )
                    p.tree_add(pending_balance.name, value, asset)
        elif wallet_balance.value() != 0:
            err("Unbalanced Transaction: %s" % self.info)
        if pending_gains:
            # book capital gains but do not recompute more than once
            if len(wallet_gains) > 1:
                err("Cross Currency Capital Gains")
            for asset, value in wallet_gains:
                pending_gains.amount = Amount(-value, asset)
                if value:
                    p.tree_add(pending_gains.name, -value, asset)
        elif wallet_gains:
            err("Unreported Capital Gains: %s" % self.info)


class Check:
    __slots__ = ("date", "name", "amount", "balance_with", "id")

    def __init__(self, date, name, amount, balance_with=None, id=None):
        self.date = parse_date(date) if isinstance(date, str) else date
        self.name = name
        self.amount = amount
        self.balance_with = balance_with
        self.id = id

    def run(self, p):
        name = self.name
        asset, other = self.amount.asset, self.balance_with
        current = p.accounts[name].wallet[asset]
        request = self.amount.value
        delta = request - current
        if delta and other:
            p.tree_add(name, delta, asset)
            p.tree_add(other, -delta, asset)
        elif delta:
            err("%s: Failed Check: %s %s!=%s", self.date, name, current, request)


def parse_date(date):
    try:
        return datetime.datetime.strptime(date, "%Y-%m-%d").date()
    except Exception:
        return None


def tree_traverse(name):
    items = name.split(":")
    for k in range(len(items), 0, -1):
        sub = ":".join(items[:k])
        yield sub


class Pacioli:

    MODEL = dict(
        Assets="Assets",
        Liabilities="Liabilities",
        Equity="Equity",
        Income="Income",
        Expenses="Expenses",
    )

    def __init__(self):
        self.begin_date = BEGIN_TIME
        self.end_date = END_TIME
        self.ledger = []
        self.accounts = dict()
        self.end_accounts = None
        self.begin_accounts = None
        self.diff_accounts = None
        self.leaf_accounts = []
        self.fifos = collections.defaultdict(list)
        for value in self.MODEL.values():
            self.open_account(value, BEGIN_TIME)

    def tree_add(self, name, value, asset):
        amount = Amount(value, asset)
        for sub in tree_traverse(name):
            assets = self.accounts[sub].assets
            self.accounts[sub].wallet.add(amount)

    def open_account(self, name, open_date, assets=None):
        if isinstance(assets, str):
            assets = [assets]
        for sub in tree_traverse(name):
            if not sub in self.accounts:
                self.accounts[sub] = Account(sub, open_date, assets)

    def close_account(self, name, close_date):
        self.accounts[name].close_date = close_date
        prefix = name + ":"
        for key in self.accounts:
            if key.startswith(prefix):
                self.accounts[key].close_date = close_date

    def load(self, filename):
        stream = open(filename, "r") if isinstance(filename, str) else filename
        transaction = None
        pads = dict()
        tags = set()
        for lineno, line in enumerate(stream):
            line, comment = line.split(";", 1) if ";" in line else (line, "")
            if not line.strip():
                continue
            elif line.lower() == "quit":
                break
            comment = comment.lstrip(";").strip()
            if line.startswith("pushtags"):
                tags |= set(line.split()[1:])
            elif line.startswith("poptags"):
                tags = tags - set(line.split()[1:])
            elif not line.startswith(" "):
                parts = line.strip().split()
                date = parse_date(parts[0])
                if not date:
                    continue
                if parts[1] == "open":
                    name = parts[2]
                    if not name.split(":")[0] in self.MODEL:
                        err("%i: invallid account name: %s", lineno, name)
                    assets = parts[3:]
                    self.open_account(name, date, assets)
                elif parts[1] == "close":
                    name = parts[2]
                    self.close_account(name, date)
                elif parts[1] == "balance":
                    name = parts[2]
                    value = parts[3]
                    asset = parts[4]
                    # print line
                    balance_with = (
                        pads[name][1]
                        if name in pads and pads[name][0] <= date
                        else None
                    )
                    self.ledger.append(
                        Check(
                            date,
                            name,
                            Amount(value, asset),
                            balance_with=balance_with,
                            id=lineno,
                        )
                    )
                elif parts[1] == "pad":
                    "@pad 2007-12-31 Assets:Current:Bank:Checking Equity:Opening-Balances"
                    name = parts[2]
                    if not name in self.accounts:
                        err("%i: Unknown account: %s", lineno, line)
                    name2 = parts[3]
                    if not name2 in self.accounts:
                        err("%i: Unknown account: %s", lineno, line)
                    pads[name] = (date, name2)
                elif parts[1] in ("*", "!", "txn", "transaction"):
                    pads.clear()
                    info = " ".join(parts[2:])
                    pending = parts[1] == "!"
                    transaction = Transaction(
                        date,
                        info,
                        id=lineno,
                        pending=pending,
                        tags=tags,
                    )
                    self.ledger.append(transaction)
                else:
                    err("%i: Invalid line: 5s", lineno, line)
            elif line.startswith(" "):
                if not transaction:
                    err("%i: Posting Outside Transaction: %s", lineno, line)
                parts = line.strip().split()
                name = parts[0]
                if name == "tags":
                    transaction.tags |= set(parts[1:])
                    continue
                elif name not in self.accounts:
                    err("%i: Unkown accouunt: %s", lineno, name)
                elif len(parts) == 1:
                    posting = Posting(name=name, comment=comment, book=False)
                elif parts[1].lower() == "book":
                    posting = Posting(name=name, comment=comment, book=True)
                elif len(parts) in (3, 6):
                    amount = Amount(parts[1], parts[2])
                    at = (
                        Amount(parts[4], parts[5])
                        if len(parts) == 6 and parts[3] == "@"
                        else None
                    )
                    posting = Posting(
                        name=name, amount=amount, at=at, comment=comment, book=False
                    )
                else:
                    err("%i: Invalid line: 5s", lineno, line)
                transaction.postings.append(posting)
        if transaction:
            self.ledger.append(transaction)
            transaction = None
        self.ledger.sort(key=lambda obj: (obj.date, obj.id))
        # find accounts which have no children
        keys = set(x.rsplit(":", 1)[0] + ":" for x in self.accounts)
        self.leaf_accounts = [x for x in self.accounts if not x + ":" in keys]
        self.leaf_accounts.sort()
        if stream != filename:
            stream.close()

    def reset(self):
        self.fifos = collections.defaultdict(list)
        for account in self.accounts.values():
            account.wallet = Wallet()

    def run(self):
        self.reset()
        for item in self.ledger:
            if not self.begin_accounts and item.date >= self.begin_date:
                self.begin_accounts = copy.deepcopy(self.accounts)
            if not self.end_accounts and item.date > self.end_date:
                self.end_accounts = copy.deepcopy(self.accounts)
            if isinstance(item, Transaction):
                for posting in item.postings:
                    account = self.accounts[posting.name]
                    if item.date < account.open_date or item.date > account.close_date:
                        err(f"Error: on {item.date} account {posting.name} is closed")
            item.run(self)
        if not self.begin_accounts:
            self.begin_accounts = copy.deepcopy(self.accounts)
        if not self.end_accounts:
            self.end_accounts = copy.deepcopy(self.accounts)
        self.diff_accounts = copy.deepcopy(self.accounts)
        for name in self.diff_accounts:
            self.diff_accounts[name].wallet.sub(self.begin_accounts[name].wallet)

    def report(self):
        for name in sorted(self.accounts):
            wallet = self.accounts[name].wallet
            pad1 = " " * (40 - len(name))
            print(f"{name} {pad1}: {wallet}")

    def save(self, filename, balance_with=None):
        with open(filename, "w") as stream:
            w = lambda msg, *args: stream.write(msg % args)
            stream.write(";;; Accounts\n\n")
            for name, acc in self.accounts.items():
                assets = ' '.join(acc.assets or [])
                w(f"{acc.open_date} open  {name} {assets}".strip() + '\n')
                if acc.close_date < END_TIME:
                    w(f"{acc.close_date} close {name} {assets}".strip() + '\n')
            n = max(len(name) for name in self.accounts)
            w("\n;; Transactions\n\n")
            for item in self.ledger:
                if item.date > self.end_date:
                    break
                elif isinstance(item, Check):
                    if item.balance_with:
                        w(f"{item.date} pad {item.name} {item.balance_with}\n")
                    w(f"{item.date} balance {item.name} {item.amount}\n\n")
                elif isinstance(item, Transaction):
                    status = "!" if item.pending else "*"
                    w(f"{item.date} {status} {item.info}\n")
                    for posting in item.postings:
                        if posting.book:
                            w(f"  {posting.name} BOOK")
                        elif posting.amount:
                            m = "%.2f" % posting.amount.value
                            padding = " " * (n - len(posting.name) + 15 - len(m))
                            w(f"  {posting.name}{padding} {m} {posting.amount.asset}")
                            if posting.at:
                                w(f" @ {posting.at}")
                        if posting.comment:
                            w(f" ; {posting.comment}")
                        w("\n")
                    if item.tags:
                        w(f"  tags {' '.join(item.tags)}\n")
                    w("\n")

    def dates_tags_accounts(self):
        dates = dict()
        tags = dict()
        accounts = dict()
        for transaction in self.ledger:
            if (
                isinstance(transaction, Transaction)
                and self.begin_date <= transaction.date <= self.end_date
            ):
                dates[transaction.date] = dates.get(transaction.date, []) + [
                    transaction
                ]
                for tag in transaction.tags:
                    tags[tag] = tags.get(tag, []) + [transaction]
                subs = set()
                for posting in transaction.postings:
                    for sub in tree_traverse(posting.name):
                        subs.add(sub)
                for sub in subs:
                    accounts[sub] = accounts.get(sub, []) + [transaction]
        return dates, tags, accounts

    def dump_html(self, path):
        HEAD = """<html>
        <head>
        <link href="http://twitter.github.com/bootstrap/assets/css/bootstrap.css" rel="stylesheet">
        <style>th,td{text-align:left}.linetop{border-top: 1px black solid}.asset{padding-left:10px}.value{text-align:right}.level0{padding-left:0;font-weight:bold}.level1{padding-left:20px}.level2{padding-left:40px}.level3{padding-left:60px}</style>
        <!--link href="../static/jquery.treeTable.css" rel="stylesheet">
        <script src="../static/jquery.js"></script>
        <script src="../static/jquery.treeTable.js"></script>
        <script>jQuery(function(){jQuery("#table").treeTable();});</script//-->
        </head>
        <body>
        """
        dates, tags, accounts = self.dates_tags_accounts()
        e = lambda t: html.escape(t.replace("_", " "))
        n = (
            lambda v: v
            if isinstance(v, str)
            else ("(%.2f)" % -v)
            if v < 0
            else ("%.2f" % v)
        )
        if not os.path.exists(path):
            os.mkdir(path)
        ALE = (self.MODEL["Assets"], self.MODEL["Liabilities"], self.MODEL["Equity"])
        PL = (self.MODEL["Income"], self.MODEL["Expenses"])

        def link_date(date):
            return '<a href="date-%s.html">%s</a>' % (date, date)

        def link_tag(tag):
            return '<a href="tag-%s.html">%s</a>' % (e(tag.lower()), e(tag))

        def link_account(name, short=None, path=""):
            return '<a href="%saccount-%s.html">%s</a>' % (
                path,
                e(name.lower().replace(":", "-")),
                e(short or name),
            )

        def dump_html_accounts(filename, header, accounts, s):
            stream = open(filename, "w")
            w = lambda s, *args: stream.write(s % args)
            w(HEAD)
            w('<div class="container">\n')
            w('<a href="index.html">Back to Index</a>')
            w("<h1>%s</h1>", header)
            w('<table id="table">')
            for name in sorted(accounts):
                if name.split(":")[0] in s:
                    for k, key in enumerate(accounts[name].wallet):
                        if k == 0:
                            w('<tr class="linetop">')
                            w(
                                '<td class="level%s">%s</td>',
                                name.count(":"),
                                link_account(name, name.rsplit(":")[-1]),
                            )
                        else:
                            w("<tr>")
                            w('<td class="level%s">...</td>', name.count(":"))
                        w('<td class="value">%s</td>', n(accounts[name].wallet[key]))
                        w('<td class="asset">%s</td>', key)
                        w("</tr>")
            w("</table>")
            w("</div></body></html>")
            stream.close()

        def dump_html_index(filename, header):
            stream = open(filename, "w")
            w = lambda s, *args: stream.write(s % args)
            w(HEAD)
            w('<div class="container">\n')
            w("<h1>%s</h1>", header)
            w('<table id="table">')
            w('<tr><td><a href="begin_balance.html">Begin Balance</a></td></tr>')
            w('<tr><td><a href="end_balance.html">End Balance</a></td></tr>')
            w('<tr><td><a href="diff_balance.html">Diff Balance</a></td></tr>')
            w(
                '<tr><td><a href="profits_and_losses.html">Profits and Losses</a></td></tr>'
            )
            w("</table>")
            w("<h2>Tags</h2>")
            w('<table id="table">')
            for tag in sorted(tags):
                w("<tr><td>%s</td></tr>", link_tag(tag))
            w("</table>")
            w("<h2>Journal</h2>")
            w('<table id="table">')
            previous = None
            for date in sorted(dates):
                current = (date.year, date.month)
                if previous != current:
                    previous = current
                    w("<tr><td>%s</td><td></td></tr>", date.strftime("%b %Y"))
                w("<tr><td></td><td>%s</td></tr>", link_date(date))
            w("</table>")
            w("</div></body></html>")
            stream.close()

        def dump_html_accounts_diff(
            filename, header, accounts1, accounts2, accounts3, s
        ):
            stream = open(filename, "w")
            w = lambda s, *args: stream.write(s % args)
            w(HEAD)
            w('<div class="container">\n')
            w('<a href="index.html">Back to Index</a>')
            w("<h1>%s</h1>", header)
            w('<table id="table">')
            w(
                "<tr><th>Account</th><th>Begin</th><th>End</th><th>Difference</th><th>Asset</td></tr>\n"
            )
            for name in sorted(accounts3):
                if name.split(":")[0] in s:
                    for k, key in enumerate(accounts3[name].wallet):
                        if k == 0:
                            w('<tr class="linetop">')
                            w(
                                '<td class="level%s">%s</td>',
                                name.count(":"),
                                link_account(name, name.rsplit(":")[-1]),
                            )
                        else:
                            w("<tr>")
                            w("<td></td>")
                        w('<td class="value">%s</td>', n(accounts1[name].wallet[key]))
                        w('<td class="value">%s</td>', n(accounts2[name].wallet[key]))
                        w('<td class="value">%s</td>', n(accounts3[name].wallet[key]))
                        w('<td class="asset">%s</td>', key)
                        w("</tr>")
            w("</table>")
            w("</div></body></html>")
            stream.close()

        def dump_html_transaction(
            filename, header, transactions, name=None, wallet=None
        ):
            wallet = copy.copy(wallet)
            stream = open(filename, "w")
            w = lambda s, *args: stream.write(s % args)
            w(HEAD)
            w('<div class="container">\n')
            w('<a href="index.html">Back to Index</a>')
            w("<h1>%s</h1>", e(str(header)))
            w('<table id="table">')
            w(
                '<tr><th>Date</th><th>Account</th><th colspan="5">Amount</th><th>Tags</th>'
            )
            if wallet is not None:
                w("<th>Balance</th>")
            w("</tr>")
            for t in transactions:
                w('<tr class="transaction">')
                w("<td>%s</td>", link_date(t.date))
                w('<td colspan="6">%s</td>', e(t.info))
                w("<td>%s</td>", " ".join(link_tag(tag) for tag in t.tags))
                if wallet is not None:
                    w("<td></td>")
                w("</tr>")
                for p in t.postings:
                    w("<tr>")
                    w("<td></td>")
                    w("<td>%s</td>", link_account(p.name))
                    w('<td class="value">%s</td>', n(p.amount.value))
                    w('<td class="asset">%s</td>', p.amount.asset)
                    if p.at is None:
                        w('<td class="asset"></td>')
                        w('<td class="value"></td>')
                        w('<td class="asset"></td>')
                    else:
                        w('<td class="asset">@</td>')
                        w('<td class="value">%s</td>', n(p.at.value))
                        w('<td class="asset">%s</td>', p.at.asset)
                    w("<td></td>")
                    if wallet is not None:
                        # FIX THIS FOR COUNTS WITH MULTIPLE ASSETS
                        if (p.name + ":").startswith(name + ":"):
                            wallet.add(Amount(p.amount.value, p.amount.asset))
                            w(
                                '<td class="value">%s %s</td>',
                                wallet[p.amount.asset],
                                p.amount.asset,
                            )
                        else:
                            w("<td></td>")
                    w("</tr>")
            w("</table>")
            w("</div></body></html>")
            stream.close()

        dump_html_index(os.path.join(path, "index.html"), "Index")
        dump_html_accounts(
            os.path.join(path, "begin_balance.html"),
            "Opening Balance (%s)" % self.begin_date,
            self.begin_accounts,
            ALE,
        )
        dump_html_accounts(
            os.path.join(path, "end_balance.html"),
            "Closing Balance (%s)" % self.end_date,
            self.end_accounts,
            ALE,
        )
        dump_html_accounts_diff(
            os.path.join(path, "diff_balance.html"),
            "Difference Balance (%s-%s)" % (self.begin_date, self.end_date),
            self.begin_accounts,
            self.end_accounts,
            self.diff_accounts,
            ALE,
        )
        dump_html_accounts(
            os.path.join(path, "profits_and_losses.html"),
            "Profits and Losses (%s-%s)" % (self.begin_date, self.end_date),
            self.diff_accounts,
            PL,
        )
        for date in dates:
            dump_html_transaction(
                os.path.join(path, "date-%s.html" % date),
                "Date: %s" % date,
                dates[date],
            )
        for tag in tags:
            dump_html_transaction(
                os.path.join(path, "tag-%s.html" % tag.lower()),
                "Tag: %s" % tag,
                tags[tag],
            )
        for name in accounts:
            dump_html_transaction(
                os.path.join(path, "account-%s.html" % name.lower().replace(":", "-")),
                "Account: %s" % name,
                accounts[name],
                name,
                self.begin_accounts[name].wallet,
            )

    @staticmethod
    def escape_latex(name):
        return (
            name.replace("#", r"\#")
            .replace("_", " ")
            .replace("%", r"\%")
            .replace("&", r"\&")
        )

    @staticmethod
    def number_latex(value):
        return (
            value
            if isinstance(value, str)
            else ("(%.2f)" % -value)
            if value < 0
            else ("%.2f" % value)
        )

    def dump_latex(self, filename):
        dates, tags, accounts = self.dates_tags_accounts()
        e, n = self.escape_latex, self.number_latex
        ALE = (self.MODEL["Assets"], self.MODEL["Liabilities"], self.MODEL["Equity"])
        PL = (self.MODEL["Income"], self.MODEL["Expenses"])

        stream = open(filename, "w")
        w = lambda s, *args: stream.write((s % args) + "\n")
        w("\\documentclass[11pt]{report}")
        w("\\begin{document}")

        def dump_latex_accounts(header, accounts, s):
            w("\\section{%s}", header)
            w("\\begin{tabular}{llr}")
            for name in sorted(accounts):
                if name.split(":")[0] in s:
                    for k, key in enumerate(accounts[name].wallet):
                        if k == 0:
                            w(
                                "{\\hskip %scm} %s & %s & %s \\\\",
                                name.count(":"),
                                name.rsplit(":")[-1],
                                n(accounts[name].wallet[key]),
                                key,
                            )
                        else:
                            w(
                                "{\\hskip %scm} ... & %s & %s \\\\",
                                name.count(":"),
                                n(accounts[name].wallet[key]),
                                key,
                            )
            w("\\end{tabular}")

        def dump_latex_accounts_diff(header, accounts1, accounts2, accounts3, s):
            w("\\section{%s}", header)
            w("\\begin{tabular}{llllr}")
            w("Account & Begin & End & Difference \\\\")
            for name in sorted(accounts2):
                if name.split(":")[0] in s:
                    for k, key in enumerate(accounts2[name].wallet):
                        if k == 0:
                            w(
                                "{\\hskip %scm} %s & %s & %s & %s & %s \\\\",
                                name.count(":"),
                                name.rsplit(":")[-1],
                                n(accounts1[name].wallet[key]),
                                n(accounts2[name].wallet[key]),
                                n(accounts3[name].wallet[key]),
                                key,
                            )
                        else:
                            w(
                                "{\\hskip %scm} ... & %s & %s & %s & %s \\\\",
                                name.count(":"),
                                n(accounts1[name].wallet[key]),
                                n(accounts2[name].wallet[key]),
                                n(accounts3[name].wallet[key]),
                                key,
                            )
            w("\\end{tabular}")

        def dump_latex_transaction(header, transactions):
            w("\\section{%s}", header)
            w("\\begin{tabular}{lllrll}")
            w("Date & Account & Amount & Tags \\\\")
            for t in transactions:
                w("%s & %s & & & & %s \\\\", t.date, e(t.info), " ".join(t.tags))
                for p in t.postings:
                    w(
                        "& %s & %s & %s & %s & \\\\",
                        p.name,
                        n(p.amount.value),
                        n(p.amount.asset),
                        " @ %s %s" % (n(p.at.value), p.at.asset) if p.at else "",
                    )
            w("\\end{tabular}")

        dump_latex_accounts_diff(
            "Difference Balance (%s-%s)" % (self.begin_date, self.end_date),
            self.begin_accounts,
            self.end_accounts,
            self.diff_accounts,
            ALE,
        )
        dump_latex_accounts(
            "Profits and Losses (%s-%s)" % (self.begin_date, self.end_date),
            self.diff_accounts,
            PL,
        )
        for date in dates:
            dump_latex_transaction("Date: %s" % date, dates[date])
        for tag in tags:
            dump_latex_transaction("Tag: %s" % tag, tags[tag])
        for name in accounts:
            dump_latex_transaction("Account: %s" % name, accounts[name])
        w("\\end{document}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-i",
        "--input",
        default="demo.ledger",
        help="the <input.ledger> file",
    )
    parser.add_argument("-b", "--begin_date", default="2000-01-01", help="begin date")
    parser.add_argument("-e", "--end_date", default="2999-12-31", help="end date")
    parser.add_argument(
        "-f",
        "--folder",
        default="{input.ledger}.output",
        help="folder where to store output",
    )
    args = parser.parse_args()
    folder = args.folder.replace("{input.ledger}", args.input)

    p = Pacioli()
    p.load(args.input)
    p.begin_date = parse_date(args.begin_date)
    p.end_date = parse_date(args.end_date)
    p.run()
    p.report()
    if folder:
        p.dump_html(folder)
        p.dump_latex(os.path.join(folder, args.input + ".latex"))
        p.save(os.path.join(folder, args.input + ".end"))


if __name__ == "__main__":
    main()
