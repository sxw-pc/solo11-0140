from collections import defaultdict
from pathlib import Path
from mealhold import meal,exposure,report
from mealhold.store import connect,read

ROOT=Path(__file__).resolve().parents[1]/'fixtures'
def fixture(name):return read(ROOT/name)
def seeded(tmp):
    cx=connect(tmp/'case.sqlite')
    with cx:
        meal.run(cx,fixture('meal-exposure-n.json'))
        meal.run(cx,fixture('meal-exposure-r.json'))
    return cx
def classes(cx):
    run=cx.execute('SELECT MAX(id) r FROM attribution_run').fetchone()['r']
    return {r['ticket_id']:r['category'] for r in cx.execute('SELECT * FROM exposure_class WHERE run_id=?',(run,))}
def notices(cx,contract,version):
    return {(r['queue'],r['kind'],r['certainty']) for r in
            cx.execute('SELECT * FROM notification WHERE contract=? AND version=?',(contract,version))}

def test_contracts_diverge_under_ambiguity(tmp_path):
    cx=seeded(tmp_path)
    with cx:exposure.run(cx,fixture('exposure-ok.json'))
    assert classes(cx)=={'T1':'possible','T2':'possible','T3':'impossible'}
    # confirmed contract claims nobody yet; both ambiguous queues are pending
    v1=notices(cx,'confirmed',1)
    assert ('Q1','pending','possible') in v1 and ('Q2','pending','possible') in v1
    assert not any(kind=='confirmed' for _,kind,_ in v1)
    # precautionary contract reminds both queues but marks them uncertain
    p1=notices(cx,'precautionary',1)
    assert ('Q1','reminder','possible') in p1 and ('Q2','reminder','possible') in p1
    assert not any(certainty=='necessary' for _,_,certainty in p1)
    reasons={r['reason'] for r in cx.execute("SELECT * FROM notification WHERE contract='precautionary' AND version=1")}
    assert all('uncertain' in r or 'identity' in r for r in reasons)
    # same evidence, actually different lists
    assert {q for q,k,_ in v1 if k=='confirmed'}!={q for q,k,_ in p1 if k=='reminder'}

def test_scan_disambiguates_and_forces_t1(tmp_path):
    cx=seeded(tmp_path)
    with cx:
        exposure.run(cx,fixture('exposure-ok.json'))
        exposure.run(cx,fixture('exposure-scan.json'))
    # the independent scan fixes T2->P-N, so the one-to-one constraint forces
    # T1->P-R; a naive per-ticket window check would still call T1 "possible"
    assert classes(cx)=={'T1':'necessary','T2':'impossible','T3':'impossible'}
    assert notices(cx,'confirmed',2)=={('Q1','confirmed','necessary')}
    p2=notices(cx,'precautionary',2)
    assert ('Q1','reminder','necessary') in p2
    assert ('Q2','revoked','possible') in p2
    assert not any(q=='Q2' and k=='reminder' for q,k,_ in p2)
    # old versions are kept, never rewritten
    assert ('Q2','pending','possible') in notices(cx,'confirmed',1)
    assert ('Q2','reminder','possible') in notices(cx,'precautionary',1)

def test_t3_never_notified(tmp_path):
    cx=seeded(tmp_path)
    with cx:
        exposure.run(cx,fixture('exposure-ok.json'))
        exposure.run(cx,fixture('exposure-scan.json'))
    assert classes(cx)['T3']=='impossible'
    assert cx.execute("SELECT COUNT(*) FROM notification WHERE queue='Q3'").fetchone()[0]==0

def test_conflicting_portion_claims_stay_pending(tmp_path):
    cx=seeded(tmp_path)
    with cx:exposure.run(cx,fixture('exposure-conflict.json'))
    # one portion claimed by two tickets: neither scan is honoured, not even
    # the earliest one (T2 at 650 < T1 at 700)
    assert classes(cx)=={'T1':'conflict','T2':'conflict'}
    v1=notices(cx,'confirmed',1)
    assert ('Q1','pending','conflict') in v1 and ('Q2','pending','conflict') in v1
    assert not any(kind=='confirmed' for _,kind,_ in v1)
    reasons={r['reason'] for r in cx.execute("SELECT * FROM notification WHERE contract='confirmed'")}
    assert any('identity conflict' in r for r in reasons)

def test_recall_lift_revokes_notifications(tmp_path):
    cx=seeded(tmp_path)
    lifted=fixture('meal-exposure-r.json')
    lifted['source_id']='meal-r-lift';lifted['ingredients'][0]['recalled']=False
    lifted['meal']['signed_at']=1500;lifted['meal']['menu_version']=2
    with cx:
        exposure.run(cx,fixture('exposure-ok.json'))
        exposure.run(cx,fixture('exposure-scan.json'))
        meal.run(cx,lifted)
    assert cx.execute("SELECT status FROM meal_batch WHERE id='lunch-r'").fetchone()[0]=='served'
    assert classes(cx)=={'T1':'impossible','T2':'impossible','T3':'impossible'}
    assert notices(cx,'confirmed',3)=={('Q1','revoked','necessary')}
    assert notices(cx,'precautionary',3)=={('Q1','revoked','necessary')}
    # old versions and the signed menu history are not rewritten
    assert notices(cx,'confirmed',2)=={('Q1','confirmed','necessary')}
    assert cx.execute("SELECT menu_version FROM meal_batch WHERE id='lunch-r'").fetchone()[0]==2

def test_replay_same_source_emits_nothing(tmp_path):
    cx=seeded(tmp_path)
    with cx:
        exposure.run(cx,fixture('exposure-ok.json'))
        runs=cx.execute('SELECT COUNT(*) FROM attribution_run').fetchone()[0]
        sent=cx.execute('SELECT COUNT(*) FROM notification').fetchone()[0]
        again=exposure.run(cx,fixture('exposure-ok.json'))
    assert again['status']=='duplicate'
    assert cx.execute('SELECT COUNT(*) FROM attribution_run').fetchone()[0]==runs
    assert cx.execute('SELECT COUNT(*) FROM notification').fetchone()[0]==sent

def test_unidentifiable_ticket_stays_unknown(tmp_path):
    cx=seeded(tmp_path)
    stray={'source_id':'exposure-stray',
           'portions':[{'id':'P-R','meal_id':'lunch-r','window':'W1','served_from':600,'served_to':900}],
           'tickets':[{'id':'T9','queue':'Q9','window':'W2','pickup_from':600,'pickup_to':1200}],
           'claims':[]}
    with cx:exposure.run(cx,stray)
    assert classes(cx)['T9']=='unknown'
    assert cx.execute("SELECT COUNT(*) FROM notification WHERE queue='Q9'").fetchone()[0]==0

def test_source_conflict_keeps_original_evidence(tmp_path):
    cx=seeded(tmp_path)
    with cx:
        exposure.run(cx,fixture('exposure-ok.json'))
        altered=fixture('exposure-ok.json');altered['tickets'][0]['pickup_to']=999
        result=exposure.run(cx,altered)
    assert result['status']=='conflict'
    assert cx.execute("SELECT pickup_to FROM meal_ticket WHERE id='T1'").fetchone()[0]==1200
    assert cx.execute('SELECT COUNT(*) FROM source_conflict').fetchone()[0]==1

def test_late_recall_reclassifies_exposure(tmp_path):
    cx=connect(tmp_path/'case.sqlite')
    clean=fixture('meal-exposure-r.json')
    clean['source_id']='meal-r-clean';clean['ingredients'][0]['recalled']=False
    with cx:
        meal.run(cx,fixture('meal-exposure-n.json'))
        meal.run(cx,clean)
        exposure.run(cx,fixture('exposure-ok.json'))
    assert classes(cx)['T1']=='impossible'
    assert cx.execute('SELECT COUNT(*) FROM notification').fetchone()[0]==0
    with cx:meal.run(cx,fixture('meal-exposure-r.json'))
    assert cx.execute("SELECT status FROM meal_batch WHERE id='lunch-r'").fetchone()[0]=='recalled'
    assert classes(cx)=={'T1':'possible','T2':'possible','T3':'impossible'}
    assert ('Q1','pending','possible') in notices(cx,'confirmed',1)

def test_attributions_are_one_to_one_and_complete(tmp_path):
    cx=seeded(tmp_path)
    with cx:exposure.run(cx,fixture('exposure-ok.json'))
    rows=cx.execute('SELECT run_id,idx,ticket_id,portion_id FROM attribution').fetchall()
    groups=defaultdict(list)
    for r in rows:groups[(r['run_id'],r['idx'])].append(r)
    # T1/T2/T3 against two delivered portions gives four complete attributions
    assert len(groups)==4
    for group in groups.values():
        assert len(group)==2
        assert len({g['ticket_id'] for g in group})==len(group)
        assert len({g['portion_id'] for g in group})==len(group)

def test_report_lists_attributions_and_notification_versions(tmp_path):
    cx=seeded(tmp_path)
    with cx:
        exposure.run(cx,fixture('exposure-ok.json'))
        exposure.run(cx,fixture('exposure-scan.json'))
    view=report.build(cx)
    assert len(view['attribution_run'])==2
    assert view['attribution'] and view['delivery_claim']
    assert view['notification_latest']['confirmed']['version']==2
    assert {e['queue'] for e in view['notification_latest']['confirmed']['entries']}=={'Q1'}
    assert {r['ticket_id']:r['category'] for r in view['exposure_current']}['T1']=='necessary'
