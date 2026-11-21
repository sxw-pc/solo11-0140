from pathlib import Path

from mealhold import complaint, meal, nutrition, report
from mealhold.store import connect, read


FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def test_nutrition_and_complaint_baselines(tmp_path):
    db = connect(tmp_path / 'food-ops.sqlite')
    with db:
        meal.run(db, read(FIXTURES / 'meal-ok.json'))
        n = nutrition.run(db, read(FIXTURES / 'nutrition-ok.json'))
        c = complaint.run(db, read(FIXTURES / 'complaint-ok.json'))
    assert n == {'meal': 'lunch-1', 'status': 'adequate', 'protein_per_person_g': 20}
    assert c['meal'] == 'lunch-1' and c['reports'] == 3 and c['status'] == 'sentinel'
    assert c['sentinel']['status'] == 'notice' and c['cluster']['status'] == 'hold'
    assert c['people'] == [3, 3] and c['households'] == [1, 1]
    assert len(report.build(db)['nutrition_item']) == 1
    assert len(report.build(db)['complaint_report']) == 3
    assert len(report.build(db)['complaint_alert']) == 1
    with db:
        assert nutrition.run(db, read(FIXTURES / 'nutrition-ok.json'))['status'] == 'duplicate'
        assert complaint.run(db, read(FIXTURES / 'complaint-ok.json'))['status'] == 'duplicate'
    db.close()
