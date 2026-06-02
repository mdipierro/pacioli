"""Render an invoice as a (possibly multi-page) PDF.

Kept free of py4web/pydal so it is easy to unit-test: ``render_invoice_pdf``
takes a plain ``dict`` describing the invoice plus the business identity (read
from ``config.toml``) and returns the PDF as ``bytes``.

The business name/logo go in the page header and the contact line in the footer
— both repeat on every page.  The line-items table repeats its column headings
across page breaks, so long invoices paginate cleanly.

Requires the ``fpdf2`` package (imported lazily so the app boots without it).
"""

import os
from decimal import Decimal, InvalidOperation

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

APP_FOLDER = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_FOLDER, "config.toml")

_CONFIG_CACHE = {}


class PdfDependencyError(RuntimeError):
    """Raised when fpdf2 is not installed."""


def load_business(path=CONFIG_PATH):
    """Return the ``[business]`` table from config.toml (cached by mtime)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _CONFIG_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    biz = data.get("business", {}) or {}
    _CONFIG_CACHE[path] = (mtime, biz)
    return biz


def _logo_path(biz):
    logo = (biz.get("logo") or "").strip()
    if not logo:
        return None
    path = logo if os.path.isabs(logo) else os.path.join(APP_FOLDER, logo)
    return path if os.path.isfile(path) else None


def _latin1(text):
    """Core PDF fonts are latin-1 only; replace anything outside it so an
    exotic character in a name/address never crashes PDF generation."""
    return str(text or "").encode("latin-1", "replace").decode("latin-1")


def _money(value):
    try:
        d = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        d = Decimal(0)
    d = d.quantize(Decimal("0.01"))
    s = f"{abs(d):,.2f}"
    return f"({s})" if d < 0 else s


def _pct(value):
    try:
        d = Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return ""
    if d == 0:
        return ""
    return f"{(d * 100).normalize():f}%"


def render_invoice_pdf(invoice, business=None):
    """Render ``invoice`` (a dict, see below) and return PDF ``bytes``.

    invoice keys: number, date, status, currency, bill_to_name,
    bill_to_address, memo, lines (list of {description, qty, unit_price,
    tax_rate, amount}), subtotal, tax_total, total, allocated, balance_due.
    """
    try:
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos
    except ImportError as ex:  # pragma: no cover - exercised only without dep
        raise PdfDependencyError(
            "PDF export needs the 'fpdf2' package — install it or run with "
            "`uv run --with py4web --with fpdf2 ...`"
        ) from ex

    biz = business if business is not None else load_business()
    logo = _logo_path(biz)
    currency = _latin1(invoice.get("currency") or "")

    biz_name = _latin1(biz.get("name") or "")
    biz_addr_lines = [_latin1(l) for l in str(biz.get("address") or "").strip().splitlines() if l.strip()]
    footer_bits = [biz.get("email"), biz.get("phone"), biz.get("website")]
    if biz.get("tax_id"):
        footer_bits.append("Tax ID: %s" % biz["tax_id"])
    footer_line = _latin1("   ·   ".join(b for b in footer_bits if b))

    class InvoicePDF(FPDF):
        def header(self):
            top = self.t_margin
            if logo:
                try:
                    self.image(logo, x=self.l_margin, y=top, w=28)
                except Exception:  # never let a bad image abort the invoice
                    pass
            self.set_xy(self.l_margin + (32 if logo else 0), top)
            self.set_font("Helvetica", "B", 15)
            self.cell(0, 7, biz_name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_font("Helvetica", "", 9)
            for line in biz_addr_lines:
                self.set_x(self.l_margin + (32 if logo else 0))
                self.cell(0, 4.5, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            # rule under the masthead
            y = max(self.get_y(), top + 26)
            self.set_draw_color(180, 188, 200)
            self.line(self.l_margin, y, self.w - self.r_margin, y)
            self.set_y(y + 4)

        def footer(self):
            self.set_y(-15)
            self.set_draw_color(180, 188, 200)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.set_y(-12)
            self.set_font("Helvetica", "", 8)
            self.set_text_color(110, 120, 135)
            if footer_line:
                self.cell(0, 5, footer_line, align="C",
                          new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_x(self.l_margin)
            self.cell(0, 5, "Page %d of {nb}" % self.page_no(), align="R")
            self.set_text_color(0, 0, 0)

    pdf = InvoicePDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_title(_latin1("Invoice %s" % invoice.get("number", "")))
    pdf.add_page()

    # --- invoice meta + bill-to -------------------------------------------
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(40, 60, 90)
    pdf.cell(0, 10, "INVOICE", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)

    meta_y = pdf.get_y()
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(95, 5, _latin1("Invoice #: %s" % invoice.get("number", "")),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(95, 5, _latin1("Date: %s" % (invoice.get("date") or "")),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if invoice.get("status"):
        pdf.cell(95, 5, _latin1("Status: %s" % invoice["status"]),
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Bill-to block on the right of the meta block
    right_x = pdf.w / 2 + 5
    pdf.set_xy(right_x, meta_y)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5, "Bill To", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_x(right_x)
    pdf.cell(0, 5, _latin1(invoice.get("bill_to_name") or ""),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    for line in str(invoice.get("bill_to_address") or "").strip().splitlines():
        if line.strip():
            pdf.set_x(right_x)
            pdf.cell(0, 5, _latin1(line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_y(max(pdf.get_y(), meta_y) + 6)

    # --- line items table (repeats headings across pages) -----------------
    money_hdr = ("Amount (%s)" % currency) if currency else "Amount"
    pdf.set_font("Helvetica", "", 9)
    with pdf.table(
        col_widths=(94, 16, 24, 14, 28),
        text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT", "RIGHT"),
        line_height=6,
        first_row_as_headings=True,
        repeat_headings=1,
    ) as table:
        table.row(["Description", "Qty", "Unit price", "Tax", money_hdr])
        for ln in invoice.get("lines", []):
            table.row([
                _latin1(ln.get("description") or ""),
                _latin1(_qty(ln.get("qty"))),
                _money(ln.get("unit_price")),
                _pct(ln.get("tax_rate")),
                _money(ln.get("amount")),
            ])

    # --- totals -----------------------------------------------------------
    pdf.ln(3)
    label_w, val_w = 38, 32
    totals_x = pdf.w - pdf.r_margin - label_w - val_w

    def total_row(label, value, bold=False):
        pdf.set_x(totals_x)
        pdf.set_font("Helvetica", "B" if bold else "", 10)
        pdf.cell(label_w, 6, _latin1(label), align="R")
        pdf.cell(val_w, 6, _money(value), align="R",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    total_row("Subtotal", invoice.get("subtotal"))
    if invoice.get("tax_total") not in (None, "", "0", "0.00"):
        total_row("Tax", invoice.get("tax_total"))
    total_row("Total", invoice.get("total"), bold=True)
    if invoice.get("allocated") not in (None, ""):
        total_row("Paid", invoice.get("allocated"))
        total_row("Balance due", invoice.get("balance_due"), bold=True)

    # --- notes ------------------------------------------------------------
    if invoice.get("memo"):
        pdf.ln(6)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Notes", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, _latin1(invoice["memo"]))

    out = pdf.output()
    return bytes(out)


def _qty(value):
    """Trim trailing zeros from a decimal quantity (1.000000 -> 1)."""
    try:
        d = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        return str(value or "")
    d = d.normalize()
    # avoid scientific notation for whole numbers like 1E+1
    return format(d, "f")
