"""Retained sample mass, collection window, custody, transfers and accident holds."""
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
    touched=set()
    for transfer in data.get('transfers',[]):
        tsid=transfer['sample_id'];left=int(transfer['left_at']);arrived=int(transfer['arrived_at'])
        seal=bool(transfer.get('seal_ok'))
        signed=bool(transfer.get('signer_a') and transfer.get('signer_b') and transfer['signer_a']!=transfer['signer_b'])
        if not(seal and signed):status='broken'
        elif arrived<left:status='invalid'
        else:status='accepted'
        db.execute('INSERT INTO transfer_event(sample_id,from_cabinet,to_cabinet,left_at,arrived_at,seal_ok,signer_a,signer_b,status,source_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (tsid,transfer['from_cabinet'],transfer['to_cabinet'],left,arrived,int(seal),transfer.get('signer_a'),transfer.get('signer_b'),status,data['source_id']))
        if status=='broken':decide(db,'transfer',tsid,'broken','transfer requires intact seal and two distinct signers',[data['source_id']])
        elif status=='invalid':decide(db,'transfer',tsid,'invalid','arrival earlier than departure',[data['source_id']])
        touched.add(tsid)
    if touched:
        from .cold import recompute_cold_case
        for tsid in sorted(touched):
            resolve_transfers(db,tsid)
            recompute_cold_case(db,tsid)
            reconcile_sample(db,tsid)
    return reconcile_sample(db,sid)

def resolve_transfers(db,sid):
    """Chain accepted transfers; flag competing destinations instead of picking one.

    A candidate transfer chains only if it departs from the cabinet the
    container is currently attributed to, no earlier than the current position.
    A candidate that cannot chain conflicts with any chained transfer from the
    same cabinet whose [left_at, arrived_at] interval it overlaps; both are
    marked 'conflict' and excluded, so a suspicious path is never silently
    taken as the container's whereabouts.
    """
    sample=db.execute('SELECT * FROM sample_seal WHERE id=?',(sid,)).fetchone()
    if not sample:return
    rows=db.execute("SELECT * FROM transfer_event WHERE sample_id=? AND status IN ('accepted','conflict') ORDER BY left_at,arrived_at,id",(sid,)).fetchall()
    if not rows:return
    conflicted=set()
    while True:
        cabinet=sample['cabinet'];since=int(sample['collected_at']);chained=[];skipped=[]
        for row in rows:
            if row['id'] in conflicted:continue
            if row['from_cabinet']==cabinet and row['left_at']>=since:
                chained.append(row);cabinet=row['to_cabinet'];since=row['arrived_at']
            else:skipped.append(row)
        fresh=set()
        for row in skipped:
            fresh.add(row['id'])
            for other in chained:
                if other['from_cabinet']==row['from_cabinet'] and row['left_at']<=other['arrived_at'] and other['left_at']<=row['arrived_at']:
                    fresh.add(other['id'])
        fresh-=conflicted
        if not fresh:break
        conflicted|=fresh
    for row in rows:
        status='conflict' if row['id'] in conflicted else 'accepted'
        if row['status']!=status:db.execute('UPDATE transfer_event SET status=? WHERE id=?',(status,row['id']))
    sources=sorted({row['source_id'] for row in rows})
    if conflicted:
        evidence=sorted({row['source_id'] for row in rows if row['id'] in conflicted})
        decide(db,'transfer',sid,'conflict','competing transfer destinations; container held for review',evidence)
    else:decide(db,'transfer',sid,'accepted','transfer chain consistent',sources)

def reconcile_meal(db,meal_id):
    for row in db.execute('SELECT id FROM sample_seal WHERE meal_id=?',(meal_id,)).fetchall():
        reconcile_sample(db,row['id'])

def reconcile_sample(db,sid):
    sample=db.execute('SELECT * FROM sample_seal WHERE id=?',(sid,)).fetchone()
    if not sample:return {'sample':sid,'status':'missing'}
    meal=db.execute('SELECT * FROM meal_batch WHERE id=?',(sample['meal_id'],)).fetchone()
    cold=db.execute('SELECT status FROM cold_case WHERE sample_id=?',(sid,)).fetchone()
    broken=db.execute('SELECT 1 FROM custody_event WHERE sample_id=? AND status="broken" LIMIT 1',(sid,)).fetchone()
    broken_transfer=db.execute("SELECT 1 FROM transfer_event WHERE sample_id=? AND status='broken' LIMIT 1",(sid,)).fetchone()
    net=sample['gross_g']-sample['tare_g']
    if not meal:status='pending_meal'
    elif sample['collected_at']>meal['served_at']+7200:status='late_rejected'
    elif meal['status']=='recalled':status='accident_hold'
    elif net<required_grams(meal['persons']):status='insufficient'
    elif broken or broken_transfer:status='broken_seal'
    elif meal['status']!='served':status='meal_hold'
    elif cold and cold['status']=='quarantined':status='quarantined'
    elif cold and cold['status']=='review':status='cold_review'
    elif cold and cold['status']=='gap':status='cold_gap'
    else:status='retained'
    db.execute('UPDATE sample_seal SET status=? WHERE id=?',(status,sid))
    decide(db,'sample',sid,status,f'net {net}g versus required {required_grams(meal["persons"]) if meal else "unknown"}g',[sample['meal_id'],sid])
    return {'sample':sid,'status':status,'net_grams':net}
