"""Record direct supplier allergen declarations for ingredient lots."""

import json

from .store import decide, ingest


def run(db, data):
    state = ingest(db, "supplier", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    lot = data["lot_id"]
    present = sorted(set(data.get("present", [])))
    may_contact = sorted(set(data.get("may_contact", [])))
    if not data.get("signer"):
        raise ValueError("supplier signer required")
    db.execute(
        "INSERT INTO supplier_declaration(lot_id,issued_at,present,may_contact,signer,source_id) VALUES(?,?,?,?,?,?)",
        (lot, int(data["issued_at"]), json.dumps(present), json.dumps(may_contact),
         data["signer"], data["source_id"]),
    )
    meals = db.execute("SELECT DISTINCT meal_id FROM ingredient_lot WHERE id=?", (lot,)).fetchall()
    for row in meals:
        label(db, row["meal_id"])
    return {"lot": lot, "meals_updated": [r["meal_id"] for r in meals]}


def label(db, meal_id):
    lots = db.execute("SELECT id FROM ingredient_lot WHERE meal_id=?", (meal_id,)).fetchall()
    if not lots:
        return {"meal": meal_id, "status": "pending"}
    present, may_contact, evidence = set(), set(), []
    for lot in lots:
        latest = db.execute(
            "SELECT * FROM supplier_declaration WHERE lot_id=? ORDER BY issued_at DESC,id DESC LIMIT 1",
            (lot["id"],),
        ).fetchone()
        if not latest:
            status = "pending"
            break
        present.update(json.loads(latest["present"]))
        may_contact.update(json.loads(latest["may_contact"]))
        evidence.append(latest["source_id"])
    else:
        status = "draft"
    reason = "all direct lot declarations available" if status == "draft" else "missing lot declaration"
    db.execute(
        "INSERT INTO meal_label(meal_id,status,present,may_contact,reason) VALUES(?,?,?,?,?) "
        "ON CONFLICT(meal_id) DO UPDATE SET status=excluded.status,present=excluded.present,"
        "may_contact=excluded.may_contact,reason=excluded.reason",
        (meal_id, status, json.dumps(sorted(present)), json.dumps(sorted(may_contact)), reason),
    )
    decide(db, "label", meal_id, status, reason, evidence)
    return {"meal": meal_id, "status": status,
            "present": sorted(present), "may_contact": sorted(may_contact)}
