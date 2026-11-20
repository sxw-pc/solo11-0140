"""Rebuild current retained-sample verdict and its evidence history."""
def build(db):
    tables=['meal_batch','ingredient_lot','observation_window','sample_seal','custody_event','temperature_window','cold_case','disposal_event','nutrition_case','nutrition_item','complaint_report','identity_credential','time_verification','complaint_case','complaint_alert','complaint_explanation','decision']
    return {name:[dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY 1')] for name in tables}
