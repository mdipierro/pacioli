## About

    Author: Massimo Di Pierro <mdipierro@cs.depaul.edu>
    License: BSD <http://opensource.org/licenses/BSD-3-Clause>
    Created on: 2013-02-20

This program is named after [Luca Pacioli](http://en.wikipedia.org/wiki/Luca_Pacioli) (1445–1517), 
inventor of accounting and double-entry bookkeeping. 
Pacioli maintained that business people must insist on justice, honour, and truth.

The program contains three parts:

### An implementation of double-entry bookkeeping 
- Assets, Liabilities, Equity, Income, Expenses 
- Computations of Balance Sheet and Profit/Losses
- Automatic computation FIFO capital-gains
- API
- Scenario analysis (WORK IN PROGRESS)

### Functions to read and write a general ledger in the [beancount format](http://furius.ca/beancount/)
- list of accounts
- support for ofx records
- transactions with multiple postings
- tags
- checks
- support for multiple files and partial output

### Reporting
- The output of the program is in HTML
- Reporting in Latex/PDF (can be improved)
- Reporting in JSON (WORKM IN PROGRESS)
- Charting

This program uses a single file to store the input ledger.
This is only appropriate for small bussinesses.
Our benchmark indicates that the program requires 0.0004 seconds/transactions. Therefore if a business processes about 1000 transaction/day, the system can process one year of trasactions in about 2 minutes. Additional time is required to generate reports.

## !!Attention!!

This program is usable but:
- It is a work in progress.
- It may need more testing.
- We plan to add feature.
- We may break the API. The documentation is incomplete.
- I am not an accountant

## Help

    ./pacioli.py -h

## Example

Consider a simple business that buys wood and sells wood toys. 

    ; file: toystore.ledger
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

Run it with
       
    ./pacioli.py -B -i toystore.ledger > toystore.balance

The output will be in toystore.ledger.output and toystore.balance

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

## More docs?

[Most of the documentation](http://furius.ca/beancount/) about ledger and beancount applies to pacioli.

## Why?

So why pacioli.py when we have beancount? I mostly made it for myself. I wanted to rewrite beancount from scratch to understand it. The result (pacioli) performs the same functions but it is much smaller (~800 lines of code). I believe pacioli has better API. I have plans to turn it into a web application and integrate it with d3.js. Beancount is GPL. Pacioli is BSD. 

## API

Pacioli can be used programmatically:

    >>> from pacioli import *
    >>> p = Pacioli()
    >>> p.add_account('Assets:Cash','De','USD')
    >>> p.add_account('Equity','Cr','USD')
    >>> p.ledger.append(Transaction('2008-10-20','info',postings=[
    ...     Posting('Assets:Cash',amount=Amount(100,'USD')),
    ...     Posting('Equity')]))
    >>> p.ledger.sort()
    >>> p.save('test.ledger')
    >>> p.run()
    >>> p.report()
    >>> p.dump_html('folder')
    >>> p.dump_latex('folder/report.latex')
    >>> assert p.accounts['Assets'].wallet['USD'] == +100
    >>> assert p.accounts['Equity'].wallet['USD'] == -100

## Web server

Pacioli uses Tornado to serve the genarated documents:

    ./pacioli.py -i demo.ledger -w 8080

The generated documents are just static html files therefore they can served using other server.

## Latex

Pacioli generates also a <input.ledger>.output/<input.ledger>.latex file. You can process it with

    ./pacioli.py -i demo.ledger
    cd demo.ledger.output
    pdflatex demo.ledger.latex
    open demo.ledger.pdf

## Other files

Pacioli includes the file demo.ledger which we stole from [beancount](http://furius.ca/beancount/) and we used for testing purposes. It also includes a demo.ledger.output generated from this example.

Pacioli also includes a gaap.lendger (GAAP = General Accepted Accounting Practices) which you can customized and use for your business.

## License

BSD v3