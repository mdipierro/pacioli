"""Development-only seeding of demo data from a JSON fixture.

``load_dev_seed(db)`` reads ``fixtures/dev_seed.json`` and creates the parties,
items and opening bills it lists, going through ``pacioli_erp.erp`` so every
record posts the right ledger/stock entries.  It is **idempotent** (records that
already exist are skipped) and is only invoked from ``models.py`` when running in
development mode and not under pytest -- see the guard there.
"""

import json
import os

from pacioli_erp import erp as erp_core

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "dev_seed.json")


def load_dev_seed(db, path=FIXTURE):
    """Populate the database with the demo fixture.  Returns the number of new
    records created (0 if the file is missing or everything already exists)."""
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    created = 0

    for p in data.get("parties", []):
        if db(db.party.name == p["name"]).isempty():
            erp_core.create_party(
                db,
                p["name"],
                kind=p.get("kind", "customer"),
                currency=p.get("currency", erp_core.DEFAULT_CURRENCY),
                email=p.get("email"),
                phone=p.get("phone"),
                address=p.get("address"),
                tags=p.get("tags"),
            )
            created += 1

    for it in data.get("items", []):
        if db(db.item.sku == it["sku"]).isempty():
            erp_core.create_item(
                db,
                it["sku"],
                name=it.get("name"),
                cost=it.get("cost", 0),
                price=it.get("price", 0),
                tracked=it.get("tracked", True),
                tags=it.get("tags"),
            )
            created += 1

    for b in data.get("bills", []):
        vendor = erp_core.lookup(db, "party", b["vendor"])
        ext = b.get("external_id")
        already = ext and not db(
            (db.document.kind == "bill") & (db.document.external_id == ext)
        ).isempty()
        if not vendor or already:
            continue
        lines = []
        for ln in b.get("lines", []):
            item = erp_core.lookup(db, "item", ln["item"])
            if item:
                lines.append(
                    {"item": item.id, "qty": ln["qty"], "unit_price": ln["unit_price"]}
                )
        if lines:
            erp_core.create_bill(db, vendor.id, lines, external_id=ext, memo="dev seed")
            created += 1

    db.commit()
    return created
