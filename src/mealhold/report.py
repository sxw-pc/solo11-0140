"""Rebuild current retained-sample verdict and its evidence history."""
def build(db):
    tables=['meal_batch','ingredient_lot','sample_seal','custody_event','temperature_window','cold_case','disposal_event','nutrition_case','nutrition_item','observation_window','complaint_report','complaint_case','complaint_time_check','complaint_ticket_check','identity_credential','identity_link','complaint_alert','alert_world','complaint_exclusion','decision']
    return {name:[dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY 1')] for name in tables}
