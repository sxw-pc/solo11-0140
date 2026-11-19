from pathlib import Path
from mealhold import meal, supplier, report
from mealhold.store import connect, read


ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def test_direct_supplier_label(tmp_path):
    db = connect(tmp_path / "supplier.sqlite")
    with db:
        meal.run(db, read(ROOT / "meal-ok.json"))
        assert supplier.label(db, "lunch-1")["status"] == "pending"
        supplier.run(db, read(ROOT / "supplier-ok.json"))
        result = supplier.label(db, "lunch-1")
    assert result == {"meal": "lunch-1", "status": "draft", "present": ["soy"],
                      "may_contact": ["sesame"]}
    assert len(report.build(db)["supplier_declaration"]) == 1


def test_supplier_source_replay(tmp_path):
    db = connect(tmp_path / "supplier.sqlite")
    with db:
        meal.run(db, read(ROOT / "meal-ok.json"))
        data = read(ROOT / "supplier-ok.json")
        supplier.run(db, data)
        assert supplier.run(db, data)["status"] == "duplicate"
    assert len(report.build(db)["supplier_declaration"]) == 1
