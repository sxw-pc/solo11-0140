"""Acceptance tests for transfer records and per-container cold attribution."""
import json
from mealhold import meal,sample,cold,report
from mealhold.store import connect

def db(path):return connect(path/'case.sqlite')

def meal_payload(source='meal-1',recalled=False):
    return {'source_id':source,
            'meal':{'id':'lunch','served_at':0,'persons':80,'menu_version':1,'signed_at':0,'allergen':'none'},
            'ingredients':[{'id':'lot-1','allergen':None,'recalled':recalled,'cleaned':True}]}

def sample_payload(sid,source,collected,cabinet='cab-A',transfers=None):
    return {'source_id':source,
            'sample':{'id':sid,'meal_id':'lunch','cabinet':cabinet,'collected_at':collected,'gross_g':140,'tare_g':10,'seal':'seal-1'},
            'custody':[],'transfers':transfers or []}

def transfer(sid,src,dst,left,arrived,seal_ok=True,signer_a='keeper-A',signer_b='keeper-B'):
    return {'sample_id':sid,'from_cabinet':src,'to_cabinet':dst,'left_at':left,'arrived_at':arrived,
            'seal_ok':seal_ok,'signer_a':signer_a,'signer_b':signer_b}

def cold_payload(source,cabinet,windows,disposals=None):
    return {'source_id':source,'cabinet':cabinet,
            'windows':[{'starts':s,'ends':e,'temp_c':t,'sensor':'sensor-1'} for s,e,t in windows],
            'disposals':disposals or []}

def dispatch(cx,payload):
    if 'meal' in payload:return meal.run(cx,payload)
    if 'sample' in payload:return sample.run(cx,payload)
    return cold.run(cx,payload)

def status(cx,sid):return cx.execute('SELECT status FROM sample_seal WHERE id=?',(sid,)).fetchone()[0]
def cold_status(cx,sid):return cx.execute('SELECT status FROM cold_case WHERE sample_id=?',(sid,)).fetchone()[0]
def segments(cx,sid):
    return cx.execute('SELECT cabinet,starts,ends,kind,exposure_s,gap FROM cold_segment WHERE sample_id=? ORDER BY seq',(sid,)).fetchall()

def test_per_container_attribution_same_cabinet(tmp_path):
    cx=db(tmp_path)
    with cx:
        meal.run(cx,meal_payload())
        sample.run(cx,sample_payload('S1','s1',100))
        sample.run(cx,sample_payload('S2','s2',1300))
        cold.run(cx,cold_payload('cold-a','cab-A',[(0,1200,5),(1200,2400,3)]))
    assert cold_status(cx,'S1')=='quarantined'
    assert status(cx,'S1')=='quarantined'
    assert segments(cx,'S1')[0][4]==1100
    assert cold_status(cx,'S2')=='clear'
    assert status(cx,'S2')=='retained'

def test_transfer_keeps_origin_exposure(tmp_path):
    cx=db(tmp_path)
    with cx:
        meal.run(cx,meal_payload())
        sample.run(cx,sample_payload('S1','s1',100))
        cold.run(cx,cold_payload('cold-a','cab-A',[(0,1200,5),(1200,2400,3)]))
        sample.run(cx,sample_payload('S1','s1-move',100,transfers=[transfer('S1','cab-A','cab-B',1500,1500)]))
        cold.run(cx,cold_payload('cold-b','cab-B',[(0,3000,3)]))
    assert cold_status(cx,'S1')=='quarantined'
    assert status(cx,'S1')=='quarantined'
    segs=segments(cx,'S1')
    assert [(s[0],s[1],s[2],s[4]) for s in segs]==[('cab-A',100,1500,1100),('cab-B',1500,None,0)]
    assert cx.execute("SELECT status FROM transfer_event WHERE sample_id='S1'").fetchone()[0]=='accepted'

def test_adjacent_windows_merge_and_overlap_not_double_counted(tmp_path):
    cx=db(tmp_path)
    with cx:
        meal.run(cx,meal_payload())
        sample.run(cx,sample_payload('S4','s4',0,cabinet='cab-C'))
        sample.run(cx,sample_payload('S5','s5',0,cabinet='cab-D'))
        cold.run(cx,cold_payload('cold-c','cab-C',[(0,600,5),(600,1200,5),(1200,2400,3)]))
        cold.run(cx,cold_payload('cold-d','cab-D',[(0,600,5),(400,1000,5),(1000,2400,3)]))
    assert cold_status(cx,'S4')=='quarantined'
    assert segments(cx,'S4')[0][4]==1200
    assert cold_status(cx,'S5')=='quarantined'
    assert segments(cx,'S5')[0][4]==1000

def test_transit_gap_extend_origin_rule(tmp_path):
    cx=db(tmp_path)
    with cx:
        meal.run(cx,meal_payload())
        sample.run(cx,sample_payload('S3','s3',0,transfers=[transfer('S3','cab-A','cab-B',3000,4900)]))
        cold.run(cx,cold_payload('cold-a','cab-A',[(0,3000,3),(3000,5000,5)]))
        cold.run(cx,cold_payload('cold-b','cab-B',[(4900,6000,3)]))
    assert cold_status(cx,'S3')=='quarantined'
    assert status(cx,'S3')=='quarantined'
    seg=cx.execute("SELECT starts,ends,exposure_s FROM cold_segment WHERE sample_id='S3' AND cabinet='cab-A'").fetchone()
    assert (seg[0],seg[1],seg[2])==(0,4900,1900)
    view=report.build(cx)
    assert view['transit_gap_rule']['rule']=='extend_origin'
    sources=json.loads(cx.execute("SELECT sources FROM cold_segment WHERE sample_id='S3' AND cabinet='cab-A'").fetchone()[0])
    assert 'cold-a' in sources and 's3' in sources
    assert any(d['system']=='cold-case' and d['object_id']=='S3' and d['status']=='quarantined' for d in view['decision'])

def run_batches(path,names):
    cx=connect(path)
    batches={'exc':cold_payload('cold-exc','cab-A',[(0,1200,5)]),
             'norm':cold_payload('cold-norm','cab-A',[(1200,2400,3)])}
    with cx:
        meal.run(cx,meal_payload())
        sample.run(cx,sample_payload('S1','s1',100))
        sample.run(cx,sample_payload('S2','s2',1300))
        for name in names:cold.run(cx,batches[name])
    return cx

def test_out_of_order_windows_reach_same_conclusion(tmp_path):
    early=run_batches(tmp_path/'exc-first',['exc','norm'])
    late=run_batches(tmp_path/'exc-last',['norm','exc'])
    for sid in ('S1','S2'):
        assert cold_status(early,sid)==cold_status(late,sid)
        assert status(early,sid)==status(late,sid)
    assert cold_status(late,'S1')=='quarantined'
    assert cold_status(late,'S2')=='clear'
    chain=late.execute("SELECT id,status,previous FROM decision WHERE system='cold-case' AND object_id='S1' ORDER BY id").fetchall()
    assert [row[1] for row in chain]==['clear','quarantined']
    assert chain[1][2]==chain[0][0]

def test_backfill_after_transfer_signoff(tmp_path):
    cx=db(tmp_path)
    with cx:
        meal.run(cx,meal_payload())
        sample.run(cx,sample_payload('S1','s1',100))
        cold.run(cx,cold_payload('cold-b','cab-B',[(0,3000,3)]))
        sample.run(cx,sample_payload('S1','s1-move',100,transfers=[transfer('S1','cab-A','cab-B',1500,1500)]))
        cold.run(cx,cold_payload('cold-exc','cab-A',[(0,1200,5)]))
        cold.run(cx,cold_payload('cold-norm','cab-A',[(1200,2400,3)]))
    assert cold_status(cx,'S1')=='quarantined'
    assert status(cx,'S1')=='quarantined'
    chain=cx.execute("SELECT id,status,previous FROM decision WHERE system='cold-case' AND object_id='S1' ORDER BY id").fetchall()
    statuses=[row[1] for row in chain]
    assert statuses[0]=='gap' and statuses[-1]=='quarantined'
    assert 'clear' not in statuses
    assert all(chain[i][2]==chain[i-1][0] for i in range(1,len(chain)))

def test_conflicting_destinations_hold_for_review(tmp_path):
    cx=db(tmp_path)
    with cx:
        meal.run(cx,meal_payload())
        sample.run(cx,sample_payload('S6','s6',0))
        sample.run(cx,sample_payload('S6','s6-move-b',0,transfers=[transfer('S6','cab-A','cab-B',1500,1500)]))
        sample.run(cx,sample_payload('S6','s6-move-c',0,transfers=[transfer('S6','cab-A','cab-C',1500,1500)]))
        cold.run(cx,cold_payload('cold-a','cab-A',[(0,3000,3)]))
    assert [row[0] for row in cx.execute("SELECT status FROM transfer_event WHERE sample_id='S6' ORDER BY id")]==['conflict','conflict']
    assert cold_status(cx,'S6')=='review'
    assert status(cx,'S6')=='cold_review'
    row=cx.execute("SELECT evidence FROM decision WHERE system='transfer' AND object_id='S6' AND status='conflict'").fetchone()
    assert json.loads(row[0])==['s6-move-b','s6-move-c']

def test_broken_transfer_flags_seal_exception(tmp_path):
    cx=db(tmp_path)
    with cx:
        meal.run(cx,meal_payload())
        sample.run(cx,sample_payload('S7','s7',0,transfers=[transfer('S7','cab-A','cab-B',1500,1500,seal_ok=False)]))
        sample.run(cx,sample_payload('S8','s8',0,transfers=[transfer('S8','cab-A','cab-B',1500,1500,signer_b=None)]))
        cold.run(cx,cold_payload('cold-a','cab-A',[(0,3000,3)]))
    assert cx.execute("SELECT status FROM transfer_event WHERE sample_id='S7'").fetchone()[0]=='broken'
    assert cx.execute("SELECT status FROM transfer_event WHERE sample_id='S8'").fetchone()[0]=='broken'
    assert status(cx,'S7')=='broken_seal'
    assert status(cx,'S8')=='broken_seal'
    assert cold_status(cx,'S7')=='clear'

def test_inverted_transfer_holds_for_review(tmp_path):
    cx=db(tmp_path)
    with cx:
        meal.run(cx,meal_payload())
        sample.run(cx,sample_payload('S9','s9',0,transfers=[transfer('S9','cab-A','cab-B',2000,1000)]))
        cold.run(cx,cold_payload('cold-a','cab-A',[(0,3000,3)]))
    assert cx.execute("SELECT status FROM transfer_event WHERE sample_id='S9'").fetchone()[0]=='invalid'
    assert cold_status(cx,'S9')=='review'
    assert status(cx,'S9')=='cold_review'

def test_accident_hold_survives_normal_transfer(tmp_path):
    cx=db(tmp_path)
    with cx:
        meal.run(cx,meal_payload())
        sample.run(cx,sample_payload('S10','s10',100))
        cold.run(cx,cold_payload('cold-a','cab-A',[(0,5000,3)]))
        meal.run(cx,meal_payload('meal-recall',recalled=True))
        sample.run(cx,sample_payload('S10','s10-move',100,transfers=[transfer('S10','cab-A','cab-B',1000,1000)]))
        cold.run(cx,cold_payload('cold-b','cab-B',[(0,5000,3)]))
        result=cold.run(cx,cold_payload('dispose','cab-B',[],disposals=[{'sample_id':'S10','at':200000,'signer_a':'A','signer_b':'B'}]))
    assert status(cx,'S10')=='accident_hold'
    assert result['disposals'][0]['status']=='blocked_accident'

def test_replay_is_idempotent(tmp_path):
    cx=db(tmp_path)
    payloads=[meal_payload(),
              sample_payload('S1','s1',100),
              sample_payload('S1','s1-move',100,transfers=[transfer('S1','cab-A','cab-B',1500,1500)]),
              cold_payload('cold-a','cab-A',[(0,1200,5),(1200,2400,3)]),
              cold_payload('cold-b','cab-B',[(0,3000,3)])]
    tables=['transfer_event','temperature_window','cold_case','cold_segment','custody_event','disposal_event','decision']
    with cx:
        for payload in payloads:dispatch(cx,payload)
    before={table:cx.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables}
    snapshot=report.build(cx)
    with cx:replays=[dispatch(cx,payload) for payload in payloads]
    assert all(result['status']=='duplicate' for result in replays)
    after={table:cx.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables}
    assert before==after
    assert report.build(cx)==snapshot
