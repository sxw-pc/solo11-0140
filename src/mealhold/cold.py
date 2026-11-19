"""Per-container cold attribution, transfer gaps and independent disposal authority."""
import json
from .store import ingest,decide

EXCURSION_LIMIT_S=900   # 15 minutes above 4C quarantines the container
GAP_LIMIT_S=1800        # 30 minutes without temperature evidence is a cold-chain gap
# Fixed transit-gap attribution rule: the signed interval between leaving one
# cabinet and arriving at the next stays attributed to the ORIGIN cabinet, so
# the gap can never count as the destination cabinet's normal refrigeration.
# The alternative route ('unattributed': the gap is a standalone interval with
# no refrigeration evidence, flagged once it exceeds GAP_LIMIT_S) is documented
# here but not selected; residence_segments below shows both branches.
TRANSIT_GAP_RULE='extend_origin'

def merged_excursions(windows):
    """Merge adjacent or overlapping over-4C windows into sustained intervals."""
    spans=sorted((int(w['starts']),int(w['ends'])) for w in windows if float(w['temp_c'])>4)
    merged=[]
    for starts,ends in spans:
        if merged and starts<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],ends))
        else:merged.append((starts,ends))
    return merged

def cabinet_verdict(db,cabinet):
    """Whole-cabinet summary kept for the cabinet-level decision chain."""
    rows=db.execute('SELECT * FROM temperature_window WHERE cabinet=? ORDER BY starts,ends',(cabinet,)).fetchall()
    excursion=any(ends-starts>=EXCURSION_LIMIT_S for starts,ends in merged_excursions(rows))
    gap=False;top=None
    for r in rows:
        if top is not None and r['starts']-top>GAP_LIMIT_S:gap=True
        top=r['ends'] if top is None else max(top,r['ends'])
    return 'quarantined' if excursion else 'gap' if gap or not rows else 'clear'

def residence_segments(sample,transfers):
    """Timeline of cabinets the container provably stayed in, collected_at first.

    Accepted transfers must chain (from_cabinet matches the current cabinet and
    left_at is not before the current position); resolve_transfers guarantees
    that. With TRANSIT_GAP_RULE='extend_origin' the origin segment runs until
    arrival at the destination; with 'unattributed' the transit interval would
    become its own evidence-free segment instead.
    """
    segments=[];cabinet=sample['cabinet'];since=int(sample['collected_at'])
    for tr in transfers:
        left=int(tr['left_at']);arrived=int(tr['arrived_at'])
        if TRANSIT_GAP_RULE=='extend_origin':
            segments.append({'cabinet':cabinet,'starts':since,'ends':arrived,'kind':'cabinet','transfer':tr['source_id']})
        else:
            segments.append({'cabinet':cabinet,'starts':since,'ends':left,'kind':'cabinet','transfer':tr['source_id']})
            if arrived>left:segments.append({'cabinet':None,'starts':left,'ends':arrived,'kind':'transit','transfer':tr['source_id']})
        cabinet=tr['to_cabinet'];since=arrived
    segments.append({'cabinet':cabinet,'starts':since,'ends':None,'kind':'cabinet','transfer':None})
    return [s for s in segments if s['ends'] is None or s['ends']>s['starts']]

def evaluate_residence(db,sample,transfers):
    """Attribute excursions and evidence gaps to each residence segment."""
    quarantine=False;gap_found=False;details=[]
    for seq,seg in enumerate(residence_segments(sample,transfers)):
        start=seg['starts'];end=seg['ends']
        if seg['kind']=='transit':
            gap=(end-start)>GAP_LIMIT_S
            gap_found=gap_found or gap
            details.append({'seq':seq,'cabinet':None,'starts':start,'ends':end,'kind':'transit','exposure_s':0,'gap':gap,'sources':[seg['transfer']]})
            continue
        rows=db.execute('SELECT * FROM temperature_window WHERE cabinet=? ORDER BY starts,ends',(seg['cabinet'],)).fetchall()
        clipped=[r for r in rows if r['ends']>start and (end is None or r['starts']<end)]
        sustained=0
        for es,ee in merged_excursions(rows):
            overlap=min(ee,end if end is not None else ee)-max(es,start)
            sustained=max(sustained,overlap)
        if sustained>=EXCURSION_LIMIT_S:quarantine=True
        gap=False
        if not clipped:gap=True
        else:
            spans=sorted((max(r['starts'],start),r['ends'] if end is None else min(r['ends'],end)) for r in clipped)
            if spans[0][0]-start>GAP_LIMIT_S:gap=True
            top=spans[0][1]
            for ws,we in spans[1:]:
                if ws-top>GAP_LIMIT_S:gap=True
                top=max(top,we)
            if end is not None and end-top>GAP_LIMIT_S:gap=True
        gap_found=gap_found or gap
        sources=sorted({r['source_id'] for r in clipped}|({seg['transfer']} if seg['transfer'] else set()))
        details.append({'seq':seq,'cabinet':seg['cabinet'],'starts':start,'ends':end,'kind':'cabinet','exposure_s':int(sustained),'gap':gap,'sources':sources})
    return quarantine,gap_found,details

def recompute_cold_case(db,sid):
    """Recompute one container's cold verdict from its full residence timeline."""
    sample=db.execute('SELECT * FROM sample_seal WHERE id=?',(sid,)).fetchone()
    if not sample:return None
    transfers=db.execute("SELECT * FROM transfer_event WHERE sample_id=? AND status='accepted' ORDER BY left_at,arrived_at,id",(sid,)).fetchall()
    review=db.execute("SELECT 1 FROM transfer_event WHERE sample_id=? AND status IN ('invalid','conflict') LIMIT 1",(sid,)).fetchone() is not None
    quarantine,gap,details=evaluate_residence(db,sample,transfers)
    verdict='quarantined' if quarantine else 'review' if review else 'gap' if gap else 'clear'
    exposure=max((d['exposure_s'] for d in details),default=0)
    reason=f'per-container residence over {len(details)} segment(s), transit gap rule {TRANSIT_GAP_RULE}, max sustained over-4C {exposure}s'
    old=db.execute('SELECT version,status FROM cold_case WHERE sample_id=?',(sid,)).fetchone()
    version=old['version'] if old and old['status']==verdict else old['version']+1 if old else 1
    db.execute('INSERT INTO cold_case(sample_id,status,reason,version) VALUES(?,?,?,?) '
               'ON CONFLICT(sample_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,version=excluded.version',
               (sid,verdict,reason,version))
    db.execute('DELETE FROM cold_segment WHERE sample_id=?',(sid,))
    for d in details:
        db.execute('INSERT INTO cold_segment(sample_id,seq,cabinet,starts,ends,kind,exposure_s,gap,sources) VALUES(?,?,?,?,?,?,?,?,?)',
                   (sid,d['seq'],d['cabinet'],d['starts'],d['ends'],d['kind'],d['exposure_s'],int(d['gap']),json.dumps(d['sources'],ensure_ascii=False)))
    evidence=sorted({source for d in details for source in d['sources']})
    decide(db,'cold-case',sid,verdict,reason,evidence)
    return verdict

def run(db,data):
    state=ingest(db,'cold',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    cabinet=data['cabinet']
    for window in data.get('windows',[]):
        if int(window['ends'])<=int(window['starts']):raise ValueError('temperature window must be positive')
        db.execute('INSERT INTO temperature_window(cabinet,starts,ends,temp_c,sensor,source_id) VALUES(?,?,?,?,?,?)',
                   (cabinet,int(window['starts']),int(window['ends']),float(window['temp_c']),window['sensor'],data['source_id']))
    verdict=cabinet_verdict(db,cabinet)
    rows=db.execute('SELECT source_id FROM temperature_window WHERE cabinet=?',(cabinet,)).fetchall()
    decide(db,'temperature',cabinet,verdict,'15 minute over-4C excursion or 30 minute sensor gap',sorted({r['source_id'] for r in rows}))
    from .sample import reconcile_sample
    for sample in db.execute('SELECT id FROM sample_seal ORDER BY id').fetchall():
        recompute_cold_case(db,sample['id'])
        reconcile_sample(db,sample['id'])
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
