from mealhold import line, meal, report
from mealhold.store import connect, read
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def test_signed_line_inspection_and_replay(tmp_path):
    db = connect(tmp_path / "line.sqlite")
    with db:
        meal.run(db, read(ROOT / "meal-ok.json"))
        first = line.run(db, read(ROOT / "line-ok.json"))
        again = line.run(db, read(ROOT / "line-ok.json"))
    assert first["status"] == "ready"
    assert again["status"] == "duplicate"
    assert len(report.build(db)["line_inspection"]) == 2


def test_unsigned_station_holds_line(tmp_path):
    db = connect(tmp_path / "line.sqlite")
    data = read(ROOT / "line-ok.json")
    data["checks"][0]["inspector_b"] = ""
    with db:
        meal.run(db, read(ROOT / "meal-ok.json"))
        assert line.run(db, data)["status"] == "hold"
