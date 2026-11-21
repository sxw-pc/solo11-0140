"""Record signed pre-service inspections for one meal on one line.

A station case is the union of every inspection recorded for that station: an
inspection that is later contradicted by an independent signed check turns the
station to ``hold``, and the meal's line follows. Earlier signed menu and
sample evidence is never rewritten by this module.
"""

from .store import decide, ingest


def _inspection_ok(check):
    inspector_a = check["inspector_a"]
    inspector_b = check["inspector_b"]
    signed = bool(inspector_a and inspector_b and inspector_a != inspector_b)
    return bool(check["inspected_ok"] and check["clean_stamp"] and signed)


def _recompute_stations(db, meal_id):
    rows = db.execute(
        "SELECT * FROM line_inspection WHERE meal_id=? ORDER BY observed_at,id",
        (meal_id,),
    ).fetchall()
    latest = {}
    held = {}
    for row in rows:
        latest[row["station"]] = row
        if not _inspection_ok(row):
            held[row["station"]] = row
    cases = []
    for station, row in latest.items():
        if station in held:
            status = "hold"
            reason = "an inspection of this station failed or was unsigned"
        else:
            status = "ready"
            reason = "station signed and inspected"
        db.execute(
            "INSERT INTO line_station_case(meal_id,station,status,observed_at,reason,source_id) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(meal_id,station) DO UPDATE SET "
            "status=excluded.status,observed_at=excluded.observed_at,"
            "reason=excluded.reason,source_id=excluded.source_id",
            (meal_id, station, status, row["observed_at"], reason, row["source_id"]),
        )
        cases.append((station, status))
    return cases


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
    for check in checks:
        db.execute(
            "INSERT INTO line_inspection(line_id,meal_id,station,observed_at,allergen_hint,"
            "inspected_ok,clean_stamp,inspector_a,inspector_b,source_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                line_id,
                meal_id,
                check["station"],
                observed_at,
                check.get("allergen_hint"),
                int(bool(check.get("inspected_ok"))),
                check.get("clean_stamp"),
                check.get("inspector_a"),
                check.get("inspector_b"),
                data["source_id"],
            ),
        )
    cases = _recompute_stations(db, meal_id)
    held = [station for station, status in cases if status == "hold"]
    status = "hold" if held else "ready"
    reason = (
        "station held by later independent inspection: " + ",".join(held)
        if held
        else "all stations signed and inspected"
    )
    db.execute(
        "INSERT INTO line_case(meal_id,line_id,status,reason) VALUES(?,?,?,?) "
        "ON CONFLICT(meal_id) DO UPDATE SET line_id=excluded.line_id,"
        "status=excluded.status,reason=excluded.reason",
        (meal_id, line_id, status, reason),
    )
    decide(db, "line-case", meal_id, status, reason, [data["source_id"]])
    return {
        "meal": meal_id,
        "line": line_id,
        "status": status,
        "stations": dict(cases),
    }
