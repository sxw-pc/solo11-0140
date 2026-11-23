"""Rebuild current retained-sample verdict and its evidence history."""
def build(db):
    tables=['meal_batch','ingredient_lot','sample_seal','custody_event','temperature_window','cold_case','disposal_event','supplier_declaration','composition_link','substitution_cert','meal_label','label_trace','label_signoff','decision']
    return {name:[dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY 1')] for name in tables}
