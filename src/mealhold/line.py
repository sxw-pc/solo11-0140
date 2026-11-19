"""Record a signed pre-service inspection for one meal on one line."""

from .store import decide, ingest


def run(db, data):
    state = ingest(db, "line", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    line_id = data["line_id"]
    meal_id = data["meal_id"]
    observed_at = int(data["observed_at"])
    if not db.execute("SELECT 1 FROM meal_batch WHERE id=?", (meal_id,)).fetchone():
        raise ValueError("meal must be recorded before line inspection")
    checks = data.get("checks", [])
    if not checks:
        raise ValueError("line inspection needs at least one station")
    valid = True
    for check in checks:
        signed = bool(check.get("inspector_a") and check.get("inspector_b")
                      and check["inspector_a"] != check["inspector_b"])
        passed = bool(check.get("inspected_ok") and check.get("clean_stamp") and signed)
        valid = valid and passed
        db.execute(
            "INSERT INTO line_inspection(line_id,meal_id,station,observed_at,allergen_hint,"
            "inspected_ok,clean_stamp,inspector_a,inspector_b,source_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (line_id, meal_id, check["station"], observed_at,
             check.get("allergen_hint"), int(bool(check.get("inspected_ok"))),
             check.get("clean_stamp"), check.get("inspector_a"),
             check.get("inspector_b"), data["source_id"]),
        )
    status = "ready" if valid else "hold"
    reason = "all stations signed and inspected" if valid else "station inspection incomplete"
    db.execute(
        "INSERT INTO line_case(meal_id,line_id,status,reason) VALUES(?,?,?,?) "
        "ON CONFLICT(meal_id) DO UPDATE SET line_id=excluded.line_id,status=excluded.status,reason=excluded.reason",
        (meal_id, line_id, status, reason),
    )
    decide(db, "line-case", meal_id, status, reason, [data["source_id"]])
    return {"meal": meal_id, "line": line_id, "status": status}
