from pathlib import Path

from mealhold import complaint, meal, report
from mealhold.store import connect, read

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def fixture(name):
    return read(FIXTURES / name)


def run_all(db, names):
    results = []
    for name in names:
        with db:
            results.append(complaint.run(db, fixture(name)))
    return results


def test_sentinel_notice_precedes_formal_cluster(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
    with db:
        s1 = complaint.run(db, fixture('complaint-ok.json'))
    assert s1['meal'] == 'lunch-1' and s1['reports'] == 3
    assert s1['people'] == [3, 3] and s1['households'] == [1, 1]
    assert s1['sentinel']['status'] == 'notice'
    assert s1['sentinel']['declaration'] == 'temporary_verification_only'
    assert 'identity_unresolved' in s1['sentinel']['caveats']
    assert '不构成独立聚集' in s1['sentinel']['notice']
    assert s1['cluster']['status'] == 'hold'
    assert s1['status'] == 'sentinel'

    with db:
        s2 = complaint.run(db, fixture('identity-roster.json'))['meals']['lunch-1']
    # three different people in one household: sentinel fires, formal cannot
    assert s2['people'] == [3, 3] and s2['households'] == [1, 1]
    assert s2['sentinel']['status'] == 'notice'
    assert s2['sentinel']['caveats'] == []
    assert s2['cluster']['status'] == 'hold'

    with db:
        s3 = complaint.run(db, fixture('complaint-r4.json'))
    assert s3['people'] == [4, 4] and s3['households'] == [2, 2]
    # H1 meal claims are not ticket-verified yet, so formal still holds
    assert s3['cluster']['status'] == 'hold'

    with db:
        s4 = complaint.run(db, fixture('identity-roster-2.json'))['meals']['lunch-1']
    assert s4['cluster']['status'] == 'hold'

    with db:
        s5 = complaint.run(db, fixture('complaint-tickets.json'))['meals']['lunch-1']
    assert s5['cluster']['status'] == 'signed'
    assert s5['status'] == 'cluster'
    assert '正式调查单' in s5['cluster']['order']

    row = db.execute("SELECT status FROM complaint_case WHERE meal_id='lunch-1'").fetchone()
    assert row['status'] == 'cluster'
    last = db.execute("SELECT status FROM decision WHERE system='complaint-cluster' "
                      'ORDER BY id DESC LIMIT 1').fetchone()
    assert last['status'] == 'signed'
    # the investigation never rewrites the meal's serving or recall state
    assert db.execute("SELECT status FROM meal_batch WHERE id='lunch-1'").fetchone()[0] == 'served'
    # every reassessment is a new version and old versions remain
    versions = [r['version'] for r in db.execute('SELECT version FROM complaint_alert ORDER BY version')]
    assert versions == list(range(1, len(versions) + 1)) and len(versions) >= 4
    db.close()


def test_same_person_credential_counts_once(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
    run_all(db, ['complaint-ok.json'])
    with db:
        s = complaint.run(db, fixture('identity-duplicate.json'))['meals']['lunch-1']
    # r2 is proven to be r1's second account: only two people remain
    assert s['people'] == [2, 2]
    assert s['sentinel']['status'] == 'hold'
    merged = s['excluded']['merged_accounts']
    assert merged[0]['aliases'] == ['acct-01', 'acct-02']
    assert merged[0]['reports'] == ['r1', 'r2']
    db.close()


def test_contradictory_identity_credentials_keep_every_interpretation(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
    run_all(db, ['complaint-ok.json', 'identity-roster.json'])
    with db:
        s = complaint.run(db, fixture('identity-duplicate.json'))['meals']['lunch-1']
    # roster says three people, clinic says two: neither side is picked
    assert s['people'] == [2, 3]
    assert len(s['detail']['worlds']) == 2
    # one feasible world has only two people, so the sentinel cannot stand
    assert s['sentinel']['status'] == 'hold'
    assert 'identity_unresolved' in s['sentinel']['caveats']
    db.close()


def test_three_aliases_of_one_person_never_count_as_three(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
    run_all(db, ['complaint-ok.json'])
    with db:
        s = complaint.run(db, fixture('identity-triple.json'))['meals']['lunch-1']
    assert s['people'] == [1, 1]
    assert s['sentinel']['status'] == 'hold'
    assert s['cluster']['status'] == 'hold'
    db.close()


def test_same_household_evidence_splits_the_two_contracts(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
    run_all(db, ['complaint-ok.json', 'identity-roster.json', 'complaint-tickets.json'])
    row = db.execute('SELECT sentinel,cluster,people_min,households_max FROM complaint_alert '
                     'ORDER BY version DESC LIMIT 1').fetchone()
    # same household never merges the three people, yet one household cannot sign
    assert row['people_min'] == 3 and row['households_max'] == 1
    assert row['sentinel'] == 'notice' and row['cluster'] == 'hold'
    db.close()


def test_possible_overlap_blocks_formal_until_time_verified(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
    run_all(db, ['complaint-ok.json', 'identity-roster.json', 'complaint-tickets.json',
                 'identity-roster-2.json'])
    with db:
        s = complaint.run(db, fixture('complaint-r4-possible.json'))
    assert s['detail']['relations']['r4'] == 'possible'
    assert s['sentinel']['status'] == 'notice'
    assert 'time_unverified' in s['sentinel']['caveats']
    # a merely overlapping interval is not a definite in-window case
    assert s['cluster']['status'] == 'hold'
    with db:
        s2 = complaint.run(db, fixture('complaint-timecheck.json'))['meals']['lunch-1']
    assert s2['detail']['relations']['r4'] == 'definite'
    assert s2['cluster']['status'] == 'signed'
    # the original claimed interval is preserved, not rewritten
    row = db.execute("SELECT symptoms_from,symptoms_to FROM complaint_report WHERE report_id='r4'").fetchone()
    assert (row['symptoms_from'], row['symptoms_to']) == (1500, 2200)
    db.close()


def test_late_time_check_can_rule_out(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
    run_all(db, ['complaint-ok.json', 'identity-roster.json', 'complaint-tickets.json',
                 'identity-roster-2.json', 'complaint-r4-possible.json'])
    with db:
        s = complaint.run(db, fixture('complaint-timecheck-out.json'))['meals']['lunch-1']
    assert s['detail']['relations']['r4'] == 'unrelated'
    assert s['cluster']['status'] == 'hold'
    # r1..r3 still stand on their own
    assert s['sentinel']['status'] == 'notice'
    db.close()


def test_adjacent_meal_reports_are_never_relocated(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
        meal.run(db, fixture('meal-lunch2.json'))
    with db:
        s1 = complaint.run(db, fixture('complaint-ok.json'))
    assert s1['people'] == [3, 3]
    before = db.execute("SELECT COUNT(*) c FROM complaint_alert WHERE meal_id='lunch-1'").fetchone()['c']
    with db:
        s2 = complaint.run(db, fixture('complaint-lunch2.json'))
    assert s2['meal'] == 'lunch-2' and s2['reports'] == 2
    assert s2['people'] == [2, 2] and s2['sentinel']['status'] == 'hold'
    # lunch-1 keeps its own three reports; nothing is borrowed from the other meal
    lunch1 = db.execute("SELECT people_min,people_max FROM complaint_alert WHERE meal_id='lunch-1' "
                        'ORDER BY version DESC LIMIT 1').fetchone()
    assert (lunch1['people_min'], lunch1['people_max']) == (3, 3)
    assert db.execute("SELECT COUNT(*) c FROM complaint_alert WHERE meal_id='lunch-1'").fetchone()['c'] == before
    rows = db.execute("SELECT report_id,meal_id,symptoms_from,symptoms_to FROM complaint_report "
                      "WHERE report_id IN ('r5','r6') ORDER BY report_id").fetchall()
    assert {row['meal_id'] for row in rows} == {'lunch-2'}
    assert [(row['symptoms_from'], row['symptoms_to']) for row in rows] == [(4300, 4600), (4400, 4700)]
    db.close()


def test_contradictory_households_block_formal_without_picking_sides(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
    run_all(db, ['complaint-ok.json', 'identity-roster.json', 'complaint-r4.json',
                 'identity-roster-2.json', 'complaint-tickets.json'])
    top = db.execute('SELECT cluster FROM complaint_alert ORDER BY version DESC LIMIT 1').fetchone()
    assert top['cluster'] == 'signed'
    with db:
        s = complaint.run(db, fixture('identity-conflict.json'))['meals']['lunch-1']
    # acct-04 is H2 in one interpretation and H1 in the other; both are kept
    assert s['people'] == [4, 4]
    assert s['households'] == [1, 2]
    # the favourable interpretation may not be picked, so formal falls back
    assert s['cluster']['status'] == 'hold'
    assert s['sentinel']['status'] == 'notice'
    latest = db.execute('SELECT id FROM complaint_alert ORDER BY version DESC LIMIT 1').fetchone()
    worlds = db.execute('SELECT DISTINCT world_index FROM alert_world WHERE alert_id=?',
                        (latest['id'],)).fetchall()
    assert len(worlds) == 2
    # the earlier signed version remains traceable
    statuses = {row['cluster'] for row in db.execute('SELECT cluster FROM complaint_alert')}
    assert statuses >= {'signed', 'hold'}
    db.close()


def test_replay_is_idempotent_and_logged(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
        complaint.run(db, fixture('complaint-ok.json'))
    before = db.execute('SELECT COUNT(*) c FROM complaint_alert').fetchone()['c']
    with db:
        again = complaint.run(db, fixture('complaint-ok.json'))
    assert again['status'] == 'duplicate'
    assert db.execute('SELECT COUNT(*) c FROM complaint_report').fetchone()['c'] == 3
    assert db.execute('SELECT COUNT(*) c FROM complaint_alert').fetchone()['c'] == before
    replays = db.execute("SELECT * FROM complaint_exclusion WHERE kind='replay'").fetchall()
    assert len(replays) == 1 and replays[0]['meal_id'] == 'lunch-1'
    altered = fixture('complaint-ok.json')
    altered['reports'][0]['symptoms_from'] = 999
    with db:
        conflict = complaint.run(db, altered)
    assert conflict['status'] == 'conflict'
    row = db.execute("SELECT symptoms_from FROM complaint_report WHERE report_id='r1'").fetchone()
    assert row['symptoms_from'] == 1000
    db.close()


def test_report_view_exposes_alert_versions_and_worlds(tmp_path):
    db = connect(tmp_path / 'case.sqlite')
    with db:
        meal.run(db, fixture('meal-ok.json'))
    run_all(db, ['complaint-ok.json', 'identity-roster.json'])
    view = report.build(db)
    assert len(view['complaint_alert']) >= 2
    assert {row['alias'] for row in view['alert_world']} == {'acct-01', 'acct-02', 'acct-03'}
    assert {row['credential_id'] for row in view['identity_credential']} == {'roster-1'}
    db.close()
