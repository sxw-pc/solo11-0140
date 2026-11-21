"""Rebuild current retained-sample verdict, exposure classes and evidence history."""
def build(db):
    tables=['meal_batch','ingredient_lot','sample_seal','custody_event','temperature_window','cold_case','disposal_event','decision',
            'serving_portion','meal_ticket','delivery_claim','attribution_run','attribution','exposure_class','notification']
    view={name:[dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY 1')] for name in tables}
    run=db.execute('SELECT MAX(id) r FROM attribution_run').fetchone()['r']
    view['exposure_current']=[dict(r) for r in db.execute(
        'SELECT ticket_id,queue,category,reason FROM exposure_class WHERE run_id=? ORDER BY ticket_id',(run,))] if run else []
    latest={}
    for contract in ('confirmed','precautionary'):
        row=db.execute('SELECT MAX(version) v FROM notification WHERE contract=?',(contract,)).fetchone()
        if row['v'] is not None:
            latest[contract]={'version':row['v'],'entries':[dict(r) for r in db.execute(
                'SELECT queue,kind,certainty,reason FROM notification WHERE contract=? AND version=? ORDER BY queue,kind',
                (contract,row['v']))]}
    view['notification_latest']=latest
    return view
