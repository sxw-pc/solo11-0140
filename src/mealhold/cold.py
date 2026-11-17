"""Cold-window gaps, sustained excursions and independent disposal authority."""
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'cold',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    cabinet=data['cabinet']
    for window in data.get('windows',[]):
        if int(window['ends'])<=int(window['starts']):raise ValueError('temperature window must be positive')
        db.execute('INSERT INTO temperature_window(cabinet,starts,ends,temp_c,sensor,source_id) VALUES(?,?,?,?,?,?)',
                   (cabinet,int(window['starts']),int(window['ends']),float(window['temp_c']),window['sensor'],data['source_id']))
    rows=db.execute('SELECT * FROM temperature_window WHERE cabinet=? ORDER BY starts,ends',(cabinet,)).fetchall()
    excursion=any(r['temp_c']>4 and r['ends']-r['starts']>=900 for r in rows)
    gap=any(b['starts']-a['ends']>1800 for a,b in zip(rows,rows[1:]))
    verdict='quarantined' if excursion else 'gap' if gap or not rows else 'clear'
    decide(db,'temperature',cabinet,verdict,'15 minute over-4C excursion or 30 minute sensor gap',[r['source_id'] for r in rows])
    from .sample import reconcile_sample
    for sample in db.execute('SELECT * FROM sample_seal WHERE cabinet=?',(cabinet,)).fetchall():
        sid=sample['id']
        old=db.execute('SELECT version,status FROM cold_case WHERE sample_id=?',(sid,)).fetchone()
        version=old['version'] if old and old['status']==verdict else old['version']+1 if old else 1
        db.execute('INSERT INTO cold_case(sample_id,status,reason,version) VALUES(?,?,?,?) '
                   'ON CONFLICT(sample_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,version=excluded.version',
                   (sid,verdict,'temperature evidence for cabinet '+cabinet,version))
        decide(db,'cold-case',sid,verdict,'sample linked to cabinet window version',[data['source_id']])
        reconcile_sample(db,sid)
    disposals=[]
    for request in data.get('disposals',[]):
        sample=db.execute('SELECT * FROM sample_seal WHERE id=?',(request['sample_id'],)).fetchone()
        if not sample:status='missing'
        elif sample['status']=='accident_hold':status='blocked_accident'
        elif int(request['at'])<sample['collected_at']+48*3600:status='too_early'
        elif not(request.get('signer_a') and request.get('signer_b') and request['signer_a']!=request['signer_b']):status='missing_approval'
        else:status='authorized'
        db.execute('INSERT INTO disposal_event(sample_id,at,signer_a,signer_b,status,reason) VALUES(?,?,?,?,?,?)',
                   (request['sample_id'],int(request['at']),request.get('signer_a'),request.get('signer_b'),status,'48h retention and two signers'))
        decide(db,'disposal',request['sample_id'],status,'independent retention and accident check',[data['source_id']])
        disposals.append({'sample':request['sample_id'],'status':status})
    return {'cabinet':cabinet,'temperature':verdict,'disposals':disposals}
