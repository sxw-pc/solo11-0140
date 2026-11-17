from pathlib import Path
from mealhold import meal,sample,cold,report
from mealhold.store import connect,read

ROOT=Path(__file__).resolve().parents[1]/'fixtures'
def fixture(name):return read(ROOT/name)
def db(tmp):return connect(tmp/'case.sqlite')
def seeded(tmp):
    cx=db(tmp)
    with cx:
        meal.run(cx,fixture('meal-ok.json'))
        sample.run(cx,fixture('sample-ok.json'))
        cold.run(cx,fixture('cold-ok.json'))
    return cx

def test_baseline_net_mass_and_custody(tmp_path):
    cx=seeded(tmp_path)
    assert cx.execute('SELECT status FROM meal_batch').fetchone()[0]=='served'
    assert cx.execute('SELECT status FROM sample_seal').fetchone()[0]=='retained'
    assert cx.execute('SELECT status FROM custody_event').fetchone()[0]=='accepted'

def test_recall_turns_sample_into_accident_hold(tmp_path):
    cx=seeded(tmp_path)
    with cx:meal.run(cx,fixture('meal-conflict.json'))
    assert cx.execute('SELECT status FROM sample_seal').fetchone()[0]=='accident_hold'
    request={'source_id':'dispose-recall','cabinet':'cabinet-1','windows':[],
             'disposals':[{'sample_id':'jar-1','at':200000,'signer_a':'A','signer_b':'B'}]}
    with cx:result=cold.run(cx,request)
    assert result['disposals'][0]['status']=='blocked_accident'

def test_person_count_revision_makes_130g_insufficient(tmp_path):
    cx=seeded(tmp_path)
    changed=fixture('meal-ok.json');changed['source_id']='more-eaters'
    changed['meal']['persons']=120;changed['meal']['signed_at']=100
    with cx:result=meal.run(cx,changed)
    assert result['required_grams']==250
    assert cx.execute('SELECT status FROM sample_seal').fetchone()[0]=='insufficient'
    assert cx.execute('SELECT status FROM cold_case').fetchone()[0]=='clear'

def test_stale_menu_does_not_replace_signed_version(tmp_path):
    cx=seeded(tmp_path)
    older=fixture('meal-ok.json');older['source_id']='older-menu'
    older['meal']['menu_version']=0;older['meal']['signed_at']=-200
    with cx:meal.run(cx,older)
    assert cx.execute('SELECT menu_version FROM meal_batch').fetchone()[0]==1

def test_allergen_cleaning_missing_holds_meal(tmp_path):
    cx=seeded(tmp_path)
    contaminated=fixture('meal-ok.json');contaminated['source_id']='dirty-line'
    contaminated['ingredients'][0]['allergen']='nuts';contaminated['ingredients'][0]['cleaned']=False
    with cx:meal.run(cx,contaminated)
    assert cx.execute('SELECT status FROM meal_batch').fetchone()[0]=='allergen_hold'
    assert cx.execute('SELECT status FROM sample_seal').fetchone()[0]=='meal_hold'

def test_broken_seal_changes_custody_and_sample(tmp_path):
    cx=seeded(tmp_path)
    with cx:sample.run(cx,fixture('sample-conflict.json'))
    assert cx.execute('SELECT status FROM sample_seal').fetchone()[0]=='broken_seal'
    assert cx.execute('SELECT status FROM custody_event ORDER BY id DESC LIMIT 1').fetchone()[0]=='broken'

def test_late_collection_rejected_even_if_cold_clear(tmp_path):
    cx=seeded(tmp_path)
    late=fixture('sample-ok.json');late['source_id']='late-jar';late['sample']['id']='jar-late'
    late['sample']['collected_at']=7500
    with cx:result=sample.run(cx,late)
    assert result['status']=='late_rejected'

def test_temperature_excursion_quarantines_sealed_sample(tmp_path):
    cx=seeded(tmp_path)
    with cx:cold.run(cx,fixture('cold-conflict.json'))
    assert cx.execute('SELECT status FROM cold_case').fetchone()[0]=='quarantined'
    assert cx.execute('SELECT status FROM sample_seal').fetchone()[0]=='quarantined'
    assert cx.execute('SELECT status FROM custody_event').fetchone()[0]=='accepted'

def test_temperature_gap_prevents_clear_verdict(tmp_path):
    cx=seeded(tmp_path)
    gap={'source_id':'gap','cabinet':'cabinet-1','windows':[{'starts':6000,'ends':7800,'temp_c':3,'sensor':'sensor-A'}], 'disposals':[]}
    with cx:cold.run(cx,gap)
    assert cx.execute('SELECT status FROM cold_case').fetchone()[0]=='gap'
    assert cx.execute('SELECT status FROM sample_seal').fetchone()[0]=='cold_gap'

def test_disposal_needs_age_and_two_signers(tmp_path):
    cx=seeded(tmp_path)
    early={'source_id':'early','cabinet':'cabinet-1','windows':[],
           'disposals':[{'sample_id':'jar-1','at':4000,'signer_a':'A','signer_b':'B'}]}
    final={'source_id':'final','cabinet':'cabinet-1','windows':[],
           'disposals':[{'sample_id':'jar-1','at':200000,'signer_a':'A','signer_b':'B'}]}
    with cx:
        a=cold.run(cx,early);b=cold.run(cx,final)
    assert a['disposals'][0]['status']=='too_early'
    assert b['disposals'][0]['status']=='authorized'

def test_same_source_conflicting_bytes_preserves_original(tmp_path):
    cx=seeded(tmp_path)
    altered=fixture('meal-ok.json');altered['meal']['persons']=999
    with cx:result=meal.run(cx,altered)
    assert result['status']=='conflict'
    assert cx.execute('SELECT persons FROM meal_batch').fetchone()[0]==80
    assert cx.execute('SELECT COUNT(*) FROM source_conflict').fetchone()[0]==1

def test_report_contains_replayable_decision_chain(tmp_path):
    cx=seeded(tmp_path)
    with cx:meal.run(cx,fixture('meal-conflict.json'))
    view=report.build(cx)
    assert len(view['decision'])>3
    assert view['sample_seal'][0]['status']=='accident_hold'
