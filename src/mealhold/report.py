"""Rebuild current retained-sample verdict, physical ledger, and history."""

TABLES = [
    "meal_batch",
    "ingredient_lot",
    "sample_seal",
    "custody_event",
    "temperature_window",
    "cold_case",
    "disposal_event",
    "line_inspection",
    "line_case",
    "line_station_case",
    "meal_portion",
    "portion_event",
    "portion_edge",
    "serving_receipt",
    "destruction",
    "destruction_item",
    "stock_count",
    "stock_item",
    "count_discrepancy",
    "closeout",
    "decision",
]


def build(db):
    view = {
        name: [dict(r) for r in db.execute(f"SELECT * FROM {name} ORDER BY 1")]
        for name in TABLES
    }
    view["disposition"] = disposition_summary(db)
    return view


def disposition_summary(db):
    """Per-meal physical totals and the last recorded closeout, if any."""
    from .disposition import reconcile

    meals = [r["id"] for r in db.execute("SELECT id FROM meal_batch ORDER BY id")]
    summary = {}
    for meal_id in meals:
        result = reconcile(db, meal_id)
        row = db.execute(
            "SELECT * FROM closeout WHERE meal_id=? ORDER BY id DESC LIMIT 1",
            (meal_id,),
        ).fetchone()
        summary[meal_id] = {
            "produced": result["produced_units"],
            "served": result["served_units"],
            "destroyed": result["destroyed_units"],
            "stock": result["stock_units"],
            "verified_servable": result["verified_servable_units"],
            "unverified": result["unverified_units"],
            "unknown": result["unknown_units"],
            "unknown_portions": result["unknown_portions"],
            "conflicts": result["conflicts"],
            "invalid_destruction_portions": result["invalid_destruction_portions"],
            "closeout": (
                {
                    "contract": row["contract"],
                    "status": row["status"],
                    "reason": row["reason"],
                }
                if row
                else None
            ),
        }
    return summary
