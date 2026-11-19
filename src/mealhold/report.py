"""Rebuild current retained-sample verdict and its evidence history."""
from . import cold

def build(db):
    tables=['meal_batch','ingredient_lot','sample_seal','custody_event','transfer_event','temperature_window','cold_case','cold_segment','disposal_event','source','source_conflict','decision']
    view={name:[dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY 1')] for name in tables}
    view['transit_gap_rule']={'rule':cold.TRANSIT_GAP_RULE,
        'meaning':'signed transit interval stays attributed to the origin cabinet' if cold.TRANSIT_GAP_RULE=='extend_origin'
                  else 'signed transit interval is a standalone gap without refrigeration evidence'}
    return view
