"""Rebuild current retained-sample verdict and its evidence history."""
def build(db):
    tables=['meal_batch','ingredient_lot','sample_seal','custody_event','temperature_window','cold_case','disposal_event','supplier_declaration','meal_label','decision']
    return {name:[dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY 1')] for name in tables}
