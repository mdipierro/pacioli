#!/usr/bin/env python
"""
Author: Massimo Di Pierro <mdipierro@cs.depaul.edu>
License: BSD <http://opensource.org/licenses/BSD-3-Clause>
Created on: 2013-02-20

This program is named after Luca Pacioli, inventor of accounting and double-entry bookkeeping.
Pacioli maintained that business people must insist on justice, honour, and truth.

The program contains four parts:

1) An implementation of double-entry bookkeeping
   - Assets, Liabilities, Equity, Income, Expenses
   - Computations of Balance Sheet and Profit&Losses
   - Automatic computation FIFO capital-gains
   - API

2) A parser/serializer for general ledgers using the syntax of ledger-cli
   (https://ledger-cli.org/).  The following ledger-cli features are supported:
   - `YYYY/MM/DD`, `YYYY-MM-DD` and `YYYY.MM.DD` dates (and `=` effective dates)
   - transaction states `*` (cleared) and `!` (pending), `(code)` and payees
   - full line comments starting with `;`, `#`, `%`, `|` or `*`
   - inline `;` comments and `:tag:` / `key: value` metadata
   - account names containing spaces (account and amount are separated by two
     or more spaces or a tab)
   - amounts with a commodity symbol prefix (`$1,000.00`) or commodity suffix
     (`1,000.00 USD`), thousands separators and signs in any position
   - posting costs `@` (per unit) and `@@` (total)
   - posting balance assertions (`Account  $10 = $100`)
   - virtual postings `(Account)` and `[Account]`
   - the `account`, `commodity`, `include`, `P`, `year`/`Y`, `D`, `bucket`/`A`,
     `apply tag`/`end apply tag`, `comment`/`end comment` directives
   Pacioli extensions kept on top of ledger syntax:
   - `DATE open ACCOUNT [COMMODITY...]` / `DATE close ACCOUNT`
   - `DATE pad ACCOUNT OTHER` / `DATE balance ACCOUNT AMOUNT` (balance checks)
   - `pushtags` / `poptags`
   - `Account  BOOK [COMMODITY]` postings to book FIFO capital gains

3) A data-source abstraction so the ledger can be read from / written to either
   a flat file or a pydal database table (see pacioli_db.py).

4) Reporting
   - HTML
   - Latex/PDF

This program uses a single file (or one database) to store the ledger. This is only
appropriate for small businesses.
"""

import argparse
import collections
import copy
import datetime
import decimal
import html
import os
import re

R = decimal.Decimal
ZERO = R("0.0")
BEGIN_TIME = datetime.date(2000, 1, 1)
END_TIME = datetime.date(2999, 12, 31)

# the default commodity used for bare numbers (ledger uses a null commodity, we
# use the empty string so that bare-number transactions still balance among
# themselves and do not mix with explicit-commodity transactions).
NULL_COMMODITY = ""

__all__ = (
    "Wallet",
    "Amount",
    "Transaction",
    "Check",
    "Posting",
    "Pacioli",
    "parse_amount",
    "parse_date",
    "DataSource",
    "FileSource",
)


def err(msg, *args):
    raise RuntimeError(msg % args if args else msg)


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_SHORT_DATE_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})$")
# a number, optionally signed, with thousands separators and decimals
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?|[-+]?\.\d+")


def parse_date(text, default_year=None):
    """Parse a ledger date.

    Accepts ``YYYY-MM-DD``, ``YYYY/MM/DD`` and ``YYYY.MM.DD`` and, when a
    ``default_year`` is supplied, the short ``MM/DD`` form.  Returns a
    ``datetime.date`` or ``None`` if the string is not a date.
    """
    if not isinstance(text, str):
        return text
    text = text.strip()
    m = _DATE_RE.match(text)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if default_year is not None:
        m = _SHORT_DATE_RE.match(text)
        if m:
            try:
                return datetime.date(default_year, int(m.group(1)), int(m.group(2)))
            except ValueError:
                return None
    return None


def parse_amount(text, default_commodity=NULL_COMMODITY):
    """Parse a ledger amount into an :class:`Amount`.

    Handles a commodity symbol/word as a prefix (``$1,000.00``, ``USD 100``) or
    a suffix (``1,000.00 USD``, ``30 AAPL``), thousands separators, quoted
    commodities (``100 "My Fund"``) and a sign in any position.  Returns
    ``None`` when *text* does not contain a number.
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    number = m.group(0)
    pre = text[: m.start()].strip()
    post = text[m.end():].strip()

    # a sign may sit in the prefix, e.g. "-$100" or "$-100"
    sign = ""
    if pre[:1] in ("-", "+"):
        sign, pre = pre[0], pre[1:].strip()
    if pre[-1:] in ("-", "+"):
        sign, pre = pre[-1], pre[:-1].strip()

    commodity = default_commodity
    if pre:
        commodity = pre.strip('"')
    elif post:
        # the suffix may carry trailing tokens we do not care about here
        token = post.split()[0]
        if post.startswith('"'):
            mq = re.match(r'"([^"]*)"', post)
            if mq:
                token = mq.group(1)
        commodity = token.strip('"')

    value = R(number.replace(",", ""))
    if sign == "-":
        value = -value
    return Amount(value, commodity)


def tree_traverse(name):
    items = name.split(":")
    for k in range(len(items), 0, -1):
        sub = ":".join(items[:k])
        yield sub


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------


class Amount:
    __slots__ = ("value", "asset")

    def __init__(self, value, asset="USD"):
        self.value = R(value)
        self.asset = asset

    def __iter__(self):
        yield (self.asset, self.value)

    def __eq__(self, other):
        return (
            isinstance(other, Amount)
            and self.value == other.value
            and self.asset == other.asset
        )

    def __str__(self):
        if self.asset:
            return f"{self.value:+} {self.asset}"
        return f"{self.value:+}"


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
        return " ".join(
            (f"{value:+} {name}" if name else f"{value:+}") for (name, value) in self
        )


class Account:
    __slots__ = ("name", "assets", "wallet", "open_date", "close_date", "type")

    def __init__(self, name, open_date=BEGIN_TIME, assets=None, type=None):
        self.name = name
        self.open_date = open_date
        self.close_date = END_TIME
        self.assets = assets
        self.type = type
        self.wallet = Wallet()

    def __str__(self):
        return str(self.wallet)


class Posting:
    __slots__ = ("name", "amount", "at", "comment", "book", "virtual", "assertion")

    def __init__(
        self,
        name,
        amount=None,
        at=None,
        comment=None,
        book=False,
        virtual=None,
        assertion=None,
    ):
        self.name = name
        self.amount = amount
        self.at = at
        self.comment = comment
        self.book = book
        # virtual is None, "(" (unbalanced) or "[" (balanced)
        self.virtual = virtual
        # an optional balance assertion (Amount) checked after the posting
        self.assertion = assertion


class Transaction:
    __slots__ = (
        "date",
        "edate",
        "info",
        "postings",
        "tags",
        "id",
        "pending",
        "code",
        "meta",
    )

    def __init__(
        self,
        date,
        info,
        postings=None,
        tags=None,
        id=None,
        pending=False,
        code=None,
        edate=None,
        meta=None,
    ):
        self.date = parse_date(date) if isinstance(date, str) else date
        self.edate = parse_date(edate) if isinstance(edate, str) else edate
        self.info = info
        self.postings = postings or []
        self.tags = tags or set()
        self.id = id
        self.pending = pending
        self.code = code
        self.meta = meta or {}

    def __lt__(self, other):
        return (self.date, self.id or 0) < (other.date, other.id or 0)

    def run(self, p):
        pending_balance = None  # auto-balanced posting
        wallet_balance = Wallet()  # transaction balance
        pending_gains = None  # the BOOK posting
        wallet_gains = Wallet()  # transaction capital gains
        for posting in self.postings:
            if posting.book is True:
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
                        p.fifos[other_asset].append((other_value, atvalue, atasset))
                    elif other_value < 0:
                        amount, fifo = -other_value, p.fifos[other_asset]
                        while amount and fifo:
                            (available, value, asset) = fifo[0]
                            delta = min(amount, available)
                            amount -= delta
                            wallet_gains.add(Amount(delta * atvalue, atasset))
                            wallet_gains.add(Amount(-delta * value, asset))
                            if delta == available:
                                fifo.pop(0)
                            else:
                                fifo[0] = (available - delta, value, asset)
                    value, asset = other_value * atvalue, atasset
                else:
                    value, asset = other_value, other_asset
                # virtual unbalanced postings "(...)" do not enter the balance
                if posting.virtual != "(":
                    wallet_balance.add(Amount(-value, asset))
                p.tree_add(name, other_value, other_asset)
            elif not pending_balance and posting.virtual != "(":
                pending_balance = posting
            elif posting.virtual == "(":
                continue
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
        # verify balance assertions
        for posting in self.postings:
            if posting.assertion is not None:
                asset = posting.assertion.asset
                current = p.accounts[posting.name].wallet[asset]
                if current != posting.assertion.value:
                    err(
                        "%s: Failed Assertion: %s %s != %s"
                        % (self.date, posting.name, current, posting.assertion.value)
                    )


class Check:
    __slots__ = ("date", "name", "amount", "balance_with", "id")

    def __init__(self, date, name, amount, balance_with=None, id=None):
        self.date = parse_date(date) if isinstance(date, str) else date
        self.name = name
        self.amount = amount
        self.balance_with = balance_with
        self.id = id

    def __lt__(self, other):
        return (self.date, self.id or 0) < (other.date, other.id or 0)

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


# ---------------------------------------------------------------------------
# main object
# ---------------------------------------------------------------------------


class Pacioli:

    MODEL = dict(
        Assets="Assets",
        Liabilities="Liabilities",
        Equity="Equity",
        Income="Income",
        Expenses="Expenses",
    )

    def __init__(self, strict=False):
        # strict=False follows ledger and auto-creates accounts on first use;
        # strict=True requires every account to be declared with `open`/`account`.
        self.strict = strict
        self.begin_date = BEGIN_TIME
        self.end_date = END_TIME
        self.ledger = []
        self.accounts = dict()
        self.end_accounts = None
        self.begin_accounts = None
        self.diff_accounts = None
        self.leaf_accounts = []
        self.prices = []
        self.default_commodity = NULL_COMMODITY
        self.fifos = collections.defaultdict(list)
        for value in self.MODEL.values():
            self.open_account(value, BEGIN_TIME)

    # -- account management -------------------------------------------------

    def tree_add(self, name, value, asset):
        amount = Amount(value, asset)
        for sub in tree_traverse(name):
            self.accounts[sub].wallet.add(amount)

    def open_account(self, name, open_date=BEGIN_TIME, assets=None, type=None):
        if isinstance(assets, str):
            assets = [assets] if assets else None
        if assets is not None:
            assets = [a for a in assets if a]
            if not assets:
                assets = None
        for sub in tree_traverse(name):
            if sub not in self.accounts:
                self.accounts[sub] = Account(sub, open_date, assets, type)

    def add_account(self, name, type=None, asset=None, open_date=BEGIN_TIME):
        """Public API to declare an account (see README)."""
        self.open_account(name, open_date, asset, type)

    def close_account(self, name, close_date):
        if name in self.accounts:
            self.accounts[name].close_date = close_date
        prefix = name + ":"
        for key in self.accounts:
            if key.startswith(prefix):
                self.accounts[key].close_date = close_date

    def ensure_account(self, name, date=BEGIN_TIME):
        """Auto-create an account when not in strict mode (ledger behaviour)."""
        if name not in self.accounts:
            if self.strict:
                err("Unknown account: %s", name)
            self.open_account(name, BEGIN_TIME)

    # -- loading ------------------------------------------------------------

    def load(self, source):
        """Load a ledger from a file path, an open stream or a DataSource.

        A ``str`` path or a file-like object is wrapped in a :class:`FileSource`
        for backward compatibility; any object exposing ``load_into(pacioli)``
        is used directly (e.g. a database source)."""
        if hasattr(source, "load_into"):
            source.load_into(self)
        else:
            FileSource(source).load_into(self)
        self.finalize()

    def finalize(self):
        """Sort the ledger and compute the list of leaf accounts."""
        self.ledger.sort(key=lambda obj: (obj.date, obj.id or 0))
        keys = set(x.rsplit(":", 1)[0] + ":" for x in self.accounts)
        self.leaf_accounts = [x for x in self.accounts if x + ":" not in keys]
        self.leaf_accounts.sort()

    def load_lines(self, lines, basedir="."):
        """Parse an iterable of ledger lines into accounts and ledger items.

        This is the ledger-cli compatible parser.  It mutates ``self`` and is
        used by :class:`FileSource`.
        """
        transaction = None
        pads = dict()
        tags = set()
        applied_tags = []
        default_year = None
        in_comment = False
        lineno = 0
        for raw in lines:
            lineno += 1
            line = raw.rstrip("\n")

            # block comments: comment ... end comment
            if in_comment:
                if line.strip().lower() in ("end comment", "end"):
                    in_comment = False
                continue
            if line.strip().lower() == "comment":
                in_comment = True
                continue

            # full line comments
            stripped = line.strip()
            if not stripped:
                transaction = None
                continue
            if stripped[0] in ";#%|*" and not line[0].isspace():
                # transaction-detached comment line
                continue

            indented = line[0].isspace()

            # split inline comment
            content, comment = self._split_comment(line)
            content_stripped = content.strip()

            if not indented:
                transaction = None
                # a `year`/`Y` directive lets short MM/DD dates be used; expand
                # them to a full ISO date so the rest of the parser is uniform
                if default_year is not None:
                    sm = re.match(r"^(\d{1,2}[-/.]\d{1,2})(\s.*)?$", content_stripped)
                    if sm and parse_date(sm.group(1), default_year):
                        full = parse_date(sm.group(1), default_year).isoformat()
                        content_stripped = full + (sm.group(2) or "")
                # try a dated line first
                m = re.match(
                    r"^(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})(?:=(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}))?\s*(.*)$",
                    content_stripped,
                )
                if m and parse_date(m.group(1)):
                    transaction = self._parse_dated(
                        m, content_stripped, tags, applied_tags, pads, lineno
                    )
                    if transaction is not None and comment:
                        self._apply_meta(transaction, comment)
                    continue
                # otherwise a non-dated directive
                default_year = self._parse_directive(
                    content_stripped,
                    comment,
                    tags,
                    applied_tags,
                    pads,
                    basedir,
                    default_year,
                    lineno,
                )
                continue

            # indented line: a posting, a transaction comment or tags
            if content_stripped == "" and comment:
                # transaction level comment / metadata / tags
                if transaction is not None:
                    self._apply_meta(transaction, comment)
                continue
            if transaction is None:
                err("%i: Posting outside transaction: %s", lineno, line)
            if content_stripped.split()[0] == "tags":
                transaction.tags |= set(content_stripped.split()[1:])
                continue
            posting = self._parse_posting(content_stripped, comment, lineno)
            if posting is not None:
                self.ensure_account(posting.name, transaction.date)
                transaction.postings.append(posting)
            if comment:
                self._apply_meta(transaction, comment)

    @staticmethod
    def _split_comment(line):
        if ";" in line:
            i = line.index(";")
            return line[:i], line[i + 1:].strip()
        return line, ""

    @staticmethod
    def _apply_meta(transaction, comment):
        # :tag1:tag2: form
        for t in re.findall(r"(?<=:)([^\s:]+)(?=:)", comment):
            transaction.tags.add(t)
        # key: value form (e.g. "Payee: Acme")
        m2 = re.match(r"^([A-Za-z][\w-]*):\s+(.*)$", comment)
        if m2:
            transaction.meta[m2.group(1)] = m2.group(2)
            transaction.tags.add(m2.group(1))

    def _parse_dated(self, m, content, tags, applied_tags, pads, lineno):
        date = parse_date(m.group(1))
        edate = parse_date(m.group(2)) if m.group(2) else None
        rest = m.group(3)
        parts = rest.split()
        keyword = parts[0] if parts else ""

        if keyword == "open":
            name = parts[1]
            assets = parts[2:]
            self.open_account(name, date, assets)
            return None
        if keyword == "close":
            self.close_account(parts[1], date)
            return None
        if keyword == "balance":
            name = parts[1]
            amount = parse_amount(" ".join(parts[2:]), self.default_commodity)
            self.ensure_account(name, date)
            balance_with = (
                pads[name][1] if name in pads and pads[name][0] <= date else None
            )
            self.ledger.append(
                Check(date, name, amount, balance_with=balance_with, id=lineno)
            )
            return None
        if keyword == "pad":
            name, name2 = parts[1], parts[2]
            self.ensure_account(name, date)
            self.ensure_account(name2, date)
            pads[name] = (date, name2)
            return None

        # otherwise a transaction.  Parse state, code and description.
        pads.clear()
        state = None
        rest2 = rest
        mm = re.match(r"^([*!])\s*(.*)$", rest2)
        if mm:
            state, rest2 = mm.group(1), mm.group(2)
        code = None
        mc = re.match(r"^\(([^)]*)\)\s*(.*)$", rest2)
        if mc:
            code, rest2 = mc.group(1), mc.group(2)
        info = rest2.strip()
        transaction = Transaction(
            date,
            info,
            id=lineno,
            pending=(state == "!"),
            code=code,
            edate=edate,
            tags=set(tags) | set(applied_tags),
        )
        self.ledger.append(transaction)
        return transaction

    def _parse_directive(
        self, content, comment, tags, applied_tags, pads, basedir, default_year, lineno
    ):
        parts = content.split()
        if not parts:
            return default_year
        keyword = parts[0]

        if keyword == "pushtags":
            tags |= set(parts[1:])
        elif keyword == "poptags":
            tags -= set(parts[1:])
        elif keyword in ("apply",) and len(parts) >= 3 and parts[1] == "tag":
            applied_tags.extend(parts[2:])
        elif keyword in ("end",) and len(parts) >= 3 and parts[1] in ("apply", "tag"):
            # `end apply tag [name]`
            names = parts[3:] if parts[1] == "apply" else parts[2:]
            if names:
                for n in names:
                    if n in applied_tags:
                        applied_tags.remove(n)
            elif applied_tags:
                applied_tags.pop()
        elif keyword == "account":
            self.open_account(parts[1], BEGIN_TIME)
        elif keyword == "commodity":
            pass  # declaration only
        elif keyword in ("year", "Y"):
            try:
                default_year = int(parts[1])
            except (IndexError, ValueError):
                pass
        elif keyword.startswith("Y") and keyword[1:].isdigit():
            default_year = int(keyword[1:])
        elif keyword == "D":
            amount = parse_amount(" ".join(parts[1:]))
            if amount:
                self.default_commodity = amount.asset
        elif keyword == "P":
            # P DATE [TIME] SYMBOL PRICE
            self._parse_price(parts)
        elif keyword in ("bucket", "A"):
            pass
        elif keyword in ("include",):
            path = " ".join(parts[1:]).strip().strip('"')
            full = path if os.path.isabs(path) else os.path.join(basedir, path)
            with open(full) as stream:
                self.load_lines(stream, os.path.dirname(full) or ".")
        elif keyword in ("tag", "payee", "define", "alias", "assert", "check", "test"):
            pass  # declarations / not modelled
        else:
            err("%i: Invalid line: %s", lineno, content)
        return default_year

    def _parse_price(self, parts):
        try:
            date = parse_date(parts[1])
            idx = 2
            # optional time token
            if idx < len(parts) and ":" in parts[idx]:
                idx += 1
            symbol = parts[idx]
            amount = parse_amount(" ".join(parts[idx + 1:]), self.default_commodity)
            if date and amount:
                self.prices.append((date, symbol, amount))
        except (IndexError, ValueError):
            pass

    def _parse_posting(self, content, comment, lineno):
        # virtual postings: (Account) ... or [Account] ...
        virtual = None
        if content[:1] == "(":
            virtual = "("
            mm = re.match(r"^\(([^)]*)\)\s*(.*)$", content)
            name, rest = mm.group(1), mm.group(2)
        elif content[:1] == "[":
            virtual = "["
            mm = re.match(r"^\[([^\]]*)\]\s*(.*)$", content)
            name, rest = mm.group(1), mm.group(2)
        else:
            # account and amount are separated by 2+ spaces or a tab
            split = re.split(r"\s{2,}|\t+", content, maxsplit=1)
            name = split[0].strip()
            rest = split[1].strip() if len(split) > 1 else ""

        # capital gains booking
        rtoks = rest.split()
        if rtoks and rtoks[0].upper() == "BOOK":
            return Posting(name=name, comment=comment, book=True, virtual=virtual)

        if not rest:
            return Posting(name=name, comment=comment, virtual=virtual)

        # balance assertion: trailing "= AMOUNT" (single '=' or '==')
        assertion = None
        masrt = re.search(r"==?\s*(.+)$", rest)
        if masrt and "@" not in masrt.group(0):
            assertion = parse_amount(masrt.group(1), self.default_commodity)
            rest = rest[: masrt.start()].strip()

        if not rest:
            # bare balance assignment "Account  = $100"
            return Posting(
                name=name, comment=comment, virtual=virtual, assertion=assertion
            )

        # cost: @@ (total) or @ (per unit)
        at = None
        if "@@" in rest:
            left, right = rest.split("@@", 1)
            amount = parse_amount(left, self.default_commodity)
            total = parse_amount(right, self.default_commodity)
            if amount and total and amount.value:
                at = Amount(total.value / amount.value, total.asset)
        elif "@" in rest:
            left, right = rest.split("@", 1)
            amount = parse_amount(left, self.default_commodity)
            at = parse_amount(right, self.default_commodity)
        else:
            amount = parse_amount(rest, self.default_commodity)

        return Posting(
            name=name,
            amount=amount,
            at=at,
            comment=comment,
            virtual=virtual,
            assertion=assertion,
        )

    # -- running ------------------------------------------------------------

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
            pad1 = " " * max(0, 40 - len(name))
            print(f"{name} {pad1}: {wallet}")

    # -- serialization ------------------------------------------------------

    def save(self, target, balance_with=None):
        """Write the ledger to a file path, an open stream or a DataSource."""
        if hasattr(target, "dump_from"):
            target.dump_from(self)
            return
        if hasattr(target, "write"):
            self._write_stream(target)
            return
        with open(target, "w") as stream:
            self._write_stream(stream)

    def _write_stream(self, stream):
        w = lambda msg: stream.write(msg)
        w(";;; Accounts\n\n")
        for name, acc in self.accounts.items():
            assets = " ".join(acc.assets or [])
            w(f"{acc.open_date} open {name} {assets}".rstrip() + "\n")
            if acc.close_date < END_TIME:
                w(f"{acc.close_date} close {name}".rstrip() + "\n")
        n = max((len(name) for name in self.accounts), default=10)
        w("\n;;; Transactions\n\n")
        for item in self.ledger:
            if item.date > self.end_date:
                break
            elif isinstance(item, Check):
                if item.balance_with:
                    w(f"{item.date} pad {item.name} {item.balance_with}\n")
                w(f"{item.date} balance {item.name} {item.amount}\n\n")
            elif isinstance(item, Transaction):
                status = "!" if item.pending else "*"
                code = f"({item.code}) " if item.code else ""
                w(f"{item.date} {status} {code}{item.info}\n")
                for posting in item.postings:
                    pname = posting.name
                    if posting.virtual == "(":
                        pname = f"({posting.name})"
                    elif posting.virtual == "[":
                        pname = f"[{posting.name}]"
                    if posting.book:
                        padding = " " * max(2, n - len(pname) + 15)
                        w(f"  {pname}{padding}BOOK")
                    elif posting.amount:
                        m = "%.2f" % posting.amount.value
                        padding = " " * max(1, n - len(pname) + 15 - len(m))
                        asset = posting.amount.asset
                        w(f"  {pname}{padding}{m} {asset}".rstrip())
                        if posting.at:
                            w(f" @ {posting.at}")
                        if posting.assertion:
                            w(f" = {posting.assertion}")
                    else:
                        w(f"  {pname}")
                    if posting.comment:
                        w(f" ; {posting.comment}")
                    w("\n")
                if item.tags:
                    w(f"  ; :{':'.join(sorted(item.tags))}:\n")
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

    def trial_balance(self):
        """Return {account_name: Wallet} for all accounts (a trial balance)."""
        return {name: acc.wallet for name, acc in self.accounts.items()}

    def dump_html(self, path):
        HEAD = """<html>
        <head>
        <link href="http://twitter.github.com/bootstrap/assets/css/bootstrap.css" rel="stylesheet">
        <style>th,td{text-align:left}.linetop{border-top: 1px black solid}.asset{padding-left:10px}.value{text-align:right}.level0{padding-left:0;font-weight:bold}.level1{padding-left:20px}.level2{padding-left:40px}.level3{padding-left:60px}</style>
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
                    w('<td class="value">%s</td>', n(p.amount.value) if p.amount else "")
                    w('<td class="asset">%s</td>', p.amount.asset if p.amount else "")
                    if p.at is None:
                        w('<td class="asset"></td>')
                        w('<td class="value"></td>')
                        w('<td class="asset"></td>')
                    else:
                        w('<td class="asset">@</td>')
                        w('<td class="value">%s</td>', n(p.at.value))
                        w('<td class="asset">%s</td>', p.at.asset)
                    w("<td></td>")
                    if wallet is not None and p.amount is not None:
                        if (p.name + ":").startswith(name + ":"):
                            wallet.add(Amount(p.amount.value, p.amount.asset))
                            w(
                                '<td class="value">%s %s</td>',
                                wallet[p.amount.asset],
                                p.amount.asset,
                            )
                        else:
                            w("<td></td>")
                    elif wallet is not None:
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
                        n(p.amount.value) if p.amount else "",
                        n(p.amount.asset) if p.amount else "",
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
        stream.close()


# ---------------------------------------------------------------------------
# data source abstraction
# ---------------------------------------------------------------------------


class DataSource:
    """Abstract source/sink for a ledger.

    A data source knows how to populate a :class:`Pacioli` instance
    (``load_into``) and how to serialize one back (``dump_from``).  The two
    concrete implementations are :class:`FileSource` (a flat ledger file) and
    ``pacioli_db.DBSource`` (a pydal database)."""

    def load_into(self, pacioli):  # pragma: no cover - interface
        raise NotImplementedError

    def dump_from(self, pacioli):  # pragma: no cover - interface
        raise NotImplementedError


class FileSource(DataSource):
    """Read/write a ledger from a flat file path or an open stream."""

    def __init__(self, file):
        self.file = file

    def load_into(self, pacioli):
        file = self.file
        if isinstance(file, str):
            basedir = os.path.dirname(os.path.abspath(file)) or "."
            with open(file, "r") as stream:
                pacioli.load_lines(stream, basedir)
        else:
            pacioli.load_lines(file, ".")

    def dump_from(self, pacioli):
        file = self.file
        if isinstance(file, str):
            with open(file, "w") as stream:
                pacioli._write_stream(stream)
        else:
            pacioli._write_stream(file)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Pacioli double-entry accounting.")
    default_input = os.path.join(os.path.dirname(__file__), "examples", "demo.ledger")
    parser.add_argument(
        "-i", "--input", default=default_input, help="the <input.ledger> file"
    )
    parser.add_argument("-b", "--begin_date", default="2000-01-01", help="begin date")
    parser.add_argument("-e", "--end_date", default="2999-12-31", help="end date")
    parser.add_argument(
        "-f",
        "--folder",
        default="{input.ledger}.output",
        help="folder where to store output",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="a pydal uri (e.g. sqlite://ledger.sqlite); read from db instead of file",
    )
    parser.add_argument(
        "--to-db", default=None, help="import the input file into this pydal uri"
    )
    parser.add_argument(
        "-w", "--web", type=int, default=None, help="serve the editor on this port"
    )
    args = parser.parse_args()
    folder = args.folder.replace("{input.ledger}", args.input)

    if args.web:
        from .editor import serve

        serve(args.input, args.web)
        return

    if args.to_db:
        from . import storage

        db = storage.connect(args.to_db)
        storage.file_to_db(args.input, db)
        print("imported %s into %s" % (args.input, args.to_db))
        return

    p = Pacioli()
    if args.db:
        from . import storage

        db = storage.connect(args.db)
        p.load(storage.DBSource(db))
    else:
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
