"""End-to-end CLI examples: original meal/supplier/label flow plus the new entries."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cli(tmp_path, *args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "mealhold", *args],
        capture_output=True, text=True, env=env, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["result"]


def test_original_meal_supplier_label_example(tmp_path):
    db = str(tmp_path / "cli.sqlite")
    cli(tmp_path, "meal", "--db", db, "--input", "fixtures/meal-ok.json")
    cli(tmp_path, "supplier", "--db", db, "--input", "fixtures/supplier-ok.json")
    result = cli(tmp_path, "label", "--db", db, "--input", "fixtures/label-query.json")
    assert result == {"meal": "lunch-1", "status": "draft",
                      "present": ["soy"], "may_contact": ["sesame"]}


def test_composition_substitution_and_sign_example(tmp_path):
    db = str(tmp_path / "cli.sqlite")
    cli(tmp_path, "meal", "--db", db, "--input", "fixtures/meal-s2.json")
    cli(tmp_path, "composition", "--db", db, "--input", "fixtures/composition-s.json")
    cli(tmp_path, "composition", "--db", db, "--input", "fixtures/composition-ok.json")
    cli(tmp_path, "supplier", "--db", db, "--input", "fixtures/supplier-soy-base.json")
    cli(tmp_path, "supplier", "--db", db, "--input", "fixtures/supplier-sesame-absent.json")
    cli(tmp_path, "supplier", "--db", db, "--input", "fixtures/supplier-sesame-may.json")
    cli(tmp_path, "substitution", "--db", db, "--input", "fixtures/substitution-ok.json")
    ordinary = cli(tmp_path, "sign", "--db", db, "--input", "fixtures/sign-query.json")
    zero = cli(tmp_path, "sign", "--db", db, "--input", "fixtures/sign-zero.json")
    assert ordinary["decision"] == "issued"
    assert ordinary["present"] == ["soy"] and ordinary["may_contact"] == ["sesame"]
    assert zero["decision"] == "held"
    report = cli(tmp_path, "report", "--db", db)
    assert len(report["label_signoff"]) == 2
    assert len(report["composition_link"]) == 4
