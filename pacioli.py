#! /usr/bin/env python

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
   - support for ofx records
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
@defaccount De Assets:Cash
@defaccount De Assets:AccountsReceivable
@defaccount Cr Liabilities:Loan
@defaccount Cr Liabilities:AccountPayable
@defaccount Cr Equity:Opening-Balances
@defaccount De Income:ToySale
@defaccount De Income:Capital-Gains
@defaccount Cr Expense:ShippingCosts
@defaccount Cr Expense:Monthly:LoanInterest

; put initial money in cash account
@pad   2013-01-01 Assets:Cash Equity:Opening-Balances
@check 2013-01-01 Assets:Cash 2000 USD

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

@begintag client-0001
2013-04-01 * first toy sale (cash transaction)
    Income:ToySale                -230 USD ; $230 from client #0001
    Assets:Cash                            ; automatically computed as 230 USD
@endtag client-0001

@check 2013-04-01 Assets:Cash     2430 USD ; 2000+10000-9*1000-500+230 = 2430 USD

; read more about AccountsReceivable
; http://www.investopedia.com/terms/a/accountsreceivable.asp

@begintag client-0002
2013-04-02 * second toy sale (invoice)
    Income:ToySale                -230 USD ; $230 billed to client #0001
    Assets:AccountsReceivable              ; automatically computed as 230 USD

2013-04-10 * second toy sale (payment received after one week)
    Assets:AccountsReceivable     -230 USD ; $230 received from client #0001
    Assets:Cash                            ; automatically computed as 230 USD
@endtag client-0002

@check 2013-04-10 Assets:Cash     2660 USD ; 2000+10000-9*1000-500+230*2 = 2460 USD
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
import io
import os
import random
import re
import sys
import time

R = decimal.Decimal
ZERO = R("0.0")

__all__ = ("Wallet", "Amount", "Transaction", "Check", "Posting", "Pacioli")


def err(msg, *args):
    raise RuntimeError(msg % args)


class Wallet(dict):
    def add(self, other):
        if isinstance(other, Amount):
            other = {other.asset: other.value}
        for asset, value in other.items():
            self[asset] = self.get(asset, ZERO) + value

    def sub(self, other):
        if isinstance(other, Amount):
            other = {other.asset: other.value}
        for asset, value in other.items():
            self[asset] = self.get(asset, ZERO) - value

    def __getitem__(self, asset):
        return self.get(asset, ZERO)

    def value(self):
        return sum(abs(x) for x in self.values())

    def __str__(self):
        return " ".join(f"{v:+,} {k}" for (k, v) in self.items())


class Account:
    __slots__ = ("name", "atype", "assets", "ofx", "wallet")

    def __init__(self, name, atype, assets=None, ofx=None, leaf=None):
        self.name = name
        self.atype = atype
        self.assets = assets
        self.ofx = ofx or {}
        self.wallet = Wallet()

    def __str__(self):
        return str(self.wallet)


class Amount:
    __slots__ = ("value", "asset")

    def __init__(self, value, asset="USD"):
        self.value = R(value)
        self.asset = asset

    def __str__(self):
        return "{self.value:+,} {self.asset}"


class Posting:
    __slots__ = ("name", "amount", "at", "comment", "book")

    def __init__(self, name, amount=None, at=None, comment=None, book=False):
        self.name = name
        self.amount = amount
        self.at = at
        self.comment = comment
        self.book = book


class Transaction:
    __slots__ = ("date", "info", "postings", "tags", "id", "pending", "edate")

    def __init__(
        self, date, info, postings=None, tags=None, id=None, pending=False, edate=None
    ):
        self.date = parse_date(date) if isinstance(date, str) else date
        self.info = info
        self.postings = postings or []
        self.tags = tags or []
        self.id = id
        self.pending = pending
        self.edate = parse_date(edate) if isinstance(edate, str) else date

    def run(self, p):
        pending_balance = None  # transaction balance
        wallet = Wallet()  # transaction balance
        pending_gains = None  # transaction capital gains
        wallet2 = Wallet()  # transaction capital gains
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
                            wallet2.add(Amount(delta * atvalue, atasset))
                            wallet2.add(Amount(-delta * value, asset))
                            if delta == available:
                                fifo = fifo[1:]
                            else:
                                fifo[0] = (available - delta, value, asset)

                    value, asset = other_value * atvalue, atasset
                else:
                    value, asset = other_value, other_asset
                print(value, asset)
                wallet.add(Amount(-value, asset))
                p.tree_add(name, other_value, other_asset)
            elif not pending_balance:
                pending_balance = posting
            else:
                err("Incomplete Transaction: %s" % self.info)
        if pending_balance:
            self.postings.remove(pending_balance)
            for asset, value in wallet.items():
                if value:
                    self.postings.append(
                        Posting(pending_balance.name, Amount(value, asset))
                    )
                    p.tree_add(pending_balance.name, value, asset)
        elif wallet.value() != 0:
            err("Unbalanced Transaction: %s" % self.info)
        if pending_gains:
            # book capital gains but do not recompute more than once
            if len(wallet2) > 1:
                err("Cross Currency Capital Gains")
            for asset, value in wallet2.items():
                pending_gains.amount = Amount(-value, asset)
                if value:
                    p.tree_add(pending_gains.name, -value, asset)
        elif wallet2:
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
    return date and datetime.datetime.strptime(date, "%Y-%m-%d").date() or None


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
        self.begin_date = parse_date("2000-01-01")
        self.end_date = parse_date("2999-12-31")
        self.ledger = []
        self.accounts = dict()
        self.end_accounts = None
        self.begin_accounts = None
        self.diff_accounts = None
        for value in self.MODEL.values():
            self.add_account(value)

    def tree_add(self, name, value, asset):
        amount = Amount(value, asset)
        for sub in tree_traverse(name):
            assets = self.accounts[sub].assets
            self.accounts[sub].wallet.add(amount)

    def add_account(self, name, atype=None, assets=None):
        if isinstance(assets, str):
            assets = [assets]
        for sub in tree_traverse(name):
            if not sub in self.accounts:
                self.accounts[sub] = Account(sub, atype, assets)

    def load(self, filename):
        stream = open(filename, "r") if isinstance(filename, str) else filename
        transaction = None
        pads = dict()
        tags = []
        for lineno, line in enumerate(stream):
            if line[:1] in ";#%":
                continue
            line, comment = line.split(";", 1) if ";" in line else (line, "")
            line = line.rstrip()
            comment = comment.strip()
            if not line:
                continue  # it is a comment
            elif line == "QUIT":
                break
            if line.startswith(" "):
                if not transaction:
                    err("%i: Posting Outside Transaction: %s", lineno, line)
                match = self.re_posting.match(line)
                if match:
                    name = match.group("name")
                    if not name in self.accounts:
                        err("%i: Unknown account: %s", lineno, line)
                    value = match.group("amount")
                    other = match.group("amount2")
                    amount = value and Amount(value, match.group("asset"))
                    at = other and Amount(other, match.group("asset2"))
                    posting = Posting(name=name, amount=amount, at=at, comment=comment)
                else:
                    match = self.re_book.match(line)
                    if match:
                        name = match.group("name")
                        if not name in self.accounts:
                            err("%i: Unknown account: %s", lineno, line)

                        posting = Posting(name=name, comment=comment, book=True)
                    else:
                        err("%i: Invalid Posting: %s", lineno, line)
                transaction.postings.append(posting)
            else:
                """
                2008-01-22 * Online Banking payment - 5051 - VISA
                  Assets:Current:Bank:Checking     -791.34 USD
                  Liabilities:Credit-Card:VISA
                """
                if transaction:
                    self.ledger.append(transaction)
                    transaction = None
                if line.startswith("@defaccount "):
                    "@defaccount Cr Liabilities:Credit-Card:VISA USD"
                    match = self.re_account.match(line)
                    if not match:
                        err("%i: Invalid Account Entry: ", lineno, line)
                    atype = match.group("atype")
                    name = match.group("name")
                    asset = match.group("asset")
                    asset = [s.strip() for s in asset.split()] if asset else None
                    # print '@defaccount', atype, name, asset or ''
                    self.add_account(name, atype, asset)
                elif line.startswith("@var"):
                    "@var ofx accid  1234...  Liabilities:Credit-Card:VISA"
                    match = self.re_varofx.match(line)
                    if not match:
                        err("%i: Invalid Var Entry: %s", lineno, line)
                    var = match.group("var")
                    value = match.group("value")
                    name = match.group("name")
                    # print line
                    if not name in self.accounts:
                        err("%i: Unknown account: %s", lineno, line)
                    self.accounts[name].ofx[var] = value
                elif line.startswith("@check"):
                    "@check 2008-01-01 Assets:Current:Bank:Checking  12.24 USD"
                    match = self.re_check.match(line)
                    if not match:
                        err("%i: Invalid Check: %s", lineno, line)
                    date = parse_date(match.group("date"))
                    name = match.group("name")
                    if not name in self.accounts:
                        err("%i: Unknown account: %s", lineno, line)
                    value = match.group("amount")
                    asset = match.group("asset")
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
                elif line.startswith("@pad"):
                    "@pad 2007-12-31 Assets:Current:Bank:Checking Equity:Opening-Balances"
                    match = self.re_pad.match(line)
                    if not match:
                        err("%i: Invalid Pad: %s", lineno, line)
                    date = parse_date(match.group("date"))
                    name = match.group("name")
                    if not name in self.accounts:
                        err("%i: Unknown account: %s", lineno, line)
                    name2 = match.group("name2")
                    if not name2 in self.accounts:
                        err("%i: Unknown account: %s", lineno, line)
                    pads[name] = (date, name2)
                elif line.startswith("@begintag"):
                    tags.append(line[9:].strip())
                elif line.startswith("@endtag"):
                    tags.remove(line[7:].strip())
                else:
                    pads.clear()
                    match = self.re_transaction.match(line)
                    # print line
                    if not match:
                        err("%i: Invalid Transaction: %s", lineno, line)
                    date = parse_date(match.group("date"))
                    edate = parse_date(match.group("edate")) or date
                    info = match.group("description")
                    pending = match.group("status") == "!"
                    transaction = Transaction(
                        date,
                        info,
                        id=lineno,
                        tags=copy.copy(tags),
                        pending=pending,
                        edate=edate,
                    )
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
            item.run(self)
        if not self.begin_accounts:
            self.begin_accounts = copy.deepcopy(self.accounts)
        if not self.end_accounts:
            self.end_accounts = copy.deepcopy(self.accounts)
        self.diff_accounts = copy.deepcopy(self.accounts)
        for name in self.diff_accounts:
            self.diff_accounts[name].wallet.sub(self.begin_accounts[name].wallet)

    re_account = re.compile(
        r"^@defaccount\s+(?P<atype>(De|Cr))\s+(?P<name>\S+)(\s+(?P<asset>\S+))?\s*$"
    )
    re_varofx = re.compile(
        r"^@var\s+ofx\s+(?P<var>\S+)\s+(?P<value>\S+)\s+(?P<name>\S+)\s*$"
    )
    re_check = re.compile(
        r"^@check\s+(?P<date>\d+\-\d+\-\d+)\s+(?P<name>\S+)\s+(?P<amount>[\-\+]?\d+(\.\d+)?)\s+(?P<asset>\S+)\s*$"
    )
    re_pad = re.compile(
        r"^@pad\s+(?P<date>\d+\-\d+\-\d+)\s+(?P<name>\S+)\s+(?P<name2>\S+)\s*$"
    )
    re_transaction = re.compile(
        r"^(?P<date>\d+\-\d+\-\d+)(?P<edate>\[\=\d+\-\d+\-\d+\])?\s+(?P<status>[*!])\s+(?P<description>.*)\s*$"
    )
    re_posting = re.compile(
        r"^\s+(?P<name>\S+)(\s+(?P<amount>[\-\+]?\d+(\.\d+)?)\s+(?P<asset>\S+)(\s+@\s+(?P<amount2>[\-\+]?\d+(\.\d+)?)\s+(?P<asset2>\S+))?)?\s*$"
    )
    re_book = re.compile(r"^\s+\((?P<name>\S+)\)\s+BOOK\s+(?P<asset>\S+)\s*$")

    def report(self):
        for name in sorted(self.accounts):
            wallet = self.accounts[name].wallet
            pad1 = " " * (40 - len(name))
            print(f"{name} {pad1}: {wallet}")

    def save(self, filename, transactions=True, balance_with=None):
        stream = open(filename, "w") if isinstance(filename, str) else filename
        w = lambda msg, *args: stream.write(msg % args)
        stream.write(";;; Accounts\n\n")
        for name, acc in self.accounts.items():
            w("@defaccount %s %s", acc.atype, name)
            if acc.assets is not None:
                w(" " + ",".join(acc.assets))
            w("\n")
        n = max(len(name) for name in self.accounts)
        if transactions:
            w("\n;;; Transactions\n\n")
            for item in self.ledger:
                if item.date > self.end_date:
                    break
                elif isinstance(item, Check):
                    if item.balance_with:
                        w("@pad %s %s %s\n", item.date, item.name, item.balance_with)
                    w(
                        "@check %s %s %s %s\n\n",
                        item.date,
                        item.name,
                        item.amount.value,
                        item.amount.asset,
                    )
                elif isinstance(item, Transaction):
                    for tag in item.tags:
                        w("@begintag %s\n", tag)
                    status = "!" if item.pending else "*"
                    w("%s %s %s\n", item.date, status, item.info)
                    for posting in item.postings:
                        w("    %s", posting.name)
                        if posting.amount:
                            m = posting.amount.value
                            if not isinstance(m, str):
                                m = "%.2f" % m
                            w(" " * (n - len(posting.name) + 15 - len(m)))
                            w("%s %s", m, posting.amount.asset)
                        if posting.at:
                            w(" @ %s %s", posting.at.value, posting.at.asset)
                        if posting.comment:
                            w(" ; %s", posting.comment)
                        w("\n")
                    for tag in item.tags:
                        w("@endtag %s\n\n", tag)
                    w("\n")
        date = self.ledger[-1].date
        for name, acc in self.accounts.items():
            for asset in acc.wallet:
                if not acc.assets or asset in acc.assets:
                    if not transactions and balance_with:
                        w("@pad %s %s %s\n", date, name, balance_with)
                    w("@check %s %s %s %s\n", date, name, acc.wallet[asset], asset)
        if stream != filename:
            stream.close()

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
            if wallet != None:
                w("<th>Balance</th>")
            w("</tr>")
            for t in transactions:
                w('<tr class="transaction">')
                w("<td>%s</td>", link_date(t.date))
                w('<td colspan="6">%s</td>', e(t.info))
                w("<td>%s</td>", " ".join(link_tag(tag) for tag in t.tags))
                if wallet != None:
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
                    if wallet != None:
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

    def dump_latex(self, filename):
        dates, tags, accounts = self.dates_tags_accounts()
        e = (
            lambda t: t.replace("#", "\#")
            .replace("_", " ")
            .replace("%", "\%")
            .replace("&", "\&")
        )
        n = (
            lambda v: v
            if isinstance(v, str)
            else ("(%.2f)" % -v)
            if v < 0
            else ("%.2f" % v)
        )
        ALE = (self.MODEL["Assets"], self.MODEL["Liabilities"], self.MODEL["Equity"])
        PL = (self.MODEL["Income"], self.MODEL["Expenses"])

        def link_account(name, short=None, path=""):
            return '<a href="%saccount-%s.html">%s</a>' % (
                path,
                e(name.lower().replace(":", "-")),
                e(short or name),
            )

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
    USAGE = "pacioli.py <input.ledger>"
    parser = argparse.ArgumentParser(description=USAGE)
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
