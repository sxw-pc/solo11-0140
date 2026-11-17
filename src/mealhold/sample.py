"""Retained sample mass, collection window, custody and accident holds."""
from .meal import required_grams
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'sample',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    sample=data['sample'];sid=sample['id']
    if int(sample['gross_g'])<int(sample['tare_g']):raise ValueError('tare exceeds gross')
    old=db.execute('SELECT version FROM sample_seal WHERE id=?',(sid,)).fetchone()
    version=old['version']+1 if old else 1
    db.execute('INSERT INTO sample_seal(id,meal_id,cabinet,collected_at,gross_g,tare_g,seal,status,version) VALUES(?,?,?,?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET cabinet=excluded.cabinet,collected_at=excluded.collected_at,gross_g=excluded.gross_g,tare_g=excluded.tare_g,seal=excluded.seal,version=excluded.version',
               (sid,sample['meal_id'],sample['cabinet'],int(sample['collected_at']),int(sample['gross_g']),int(sample['tare_g']),sample['seal'],'pending',version))
    for handoff in data.get('custody',[]):
        valid=bool(handoff.get('seal_ok') and handoff.get('signer_a') and handoff.get('signer_b') and handoff['signer_a']!=handoff['signer_b'])
        db.execute('INSERT INTO custody_event(sample_id,at,from_person,to_person,seal_ok,signer_a,signer_b,status) VALUES(?,?,?,?,?,?,?,?)',
                   (sid,int(handoff['at']),handoff['from'],handoff['to'],int(bool(handoff.get('seal_ok'))),handoff.get('signer_a'),handoff.get('signer_b'),'accepted' if valid else 'broken'))
        decide(db,'custody',sid,'accepted' if valid else 'broken','seal and two signatures required',[data['source_id']])
    return reconcile_sample(db,sid)

def reconcile_meal(db,meal_id):
    for row in db.execute('SELECT id FROM sample_seal WHERE meal_id=?',(meal_id,)).fetchall():
        reconcile_sample(db,row['id'])

def reconcile_sample(db,sid):
    sample=db.execute('SELECT * FROM sample_seal WHERE id=?',(sid,)).fetchone()
    if not sample:return {'sample':sid,'status':'missing'}
    meal=db.execute('SELECT * FROM meal_batch WHERE id=?',(sample['meal_id'],)).fetchone()
    cold=db.execute('SELECT status FROM cold_case WHERE sample_id=?',(sid,)).fetchone()
    broken=db.execute('SELECT 1 FROM custody_event WHERE sample_id=? AND status="broken" LIMIT 1',(sid,)).fetchone()
    net=sample['gross_g']-sample['tare_g']
    if not meal:status='pending_meal'
    elif sample['collected_at']>meal['served_at']+7200:status='late_rejected'
    elif meal['status']=='recalled':status='accident_hold'
    elif net<required_grams(meal['persons']):status='insufficient'
    elif broken:status='broken_seal'
    elif meal['status']!='served':status='meal_hold'
    elif cold and cold['status']=='quarantined':status='quarantined'
    elif cold and cold['status']=='gap':status='cold_gap'
    else:status='retained'
    db.execute('UPDATE sample_seal SET status=? WHERE id=?',(status,sid))
    decide(db,'sample',sid,status,f'net {net}g versus required {required_grams(meal["persons"]) if meal else "unknown"}g',[sample['meal_id'],sid])
    return {'sample':sid,'status':status,'net_grams':net}
