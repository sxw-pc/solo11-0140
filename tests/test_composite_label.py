"""Composite composition, substitution certificates, commissions and sign-off."""
import json
from pathlib import Path

import pytest

from mealhold import meal, sample, supplier, report
from mealhold.store import connect, read

ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def fixture(name):
    return read(ROOT / name)


def db(tmp):
    return connect(tmp / "case.sqlite")


def meal_payload(source_id, meal_id, lots, allergen="soy", version=1, signed_at=-100):
    return {"source_id": source_id,
            "meal": {"id": meal_id, "served_at": 0, "persons": 80,
                     "menu_version": version, "signed_at": signed_at, "allergen": allergen},
            "ingredients": [{"id": lot, "allergen": allergen if allergen != "none" else None,
                             "recalled": False, "cleaned": True} for lot in lots]}


def declaration(source_id, lot, signer, present=(), may_contact=(), absent=(), issued_at=10):
    return {"source_id": source_id, "lot_id": lot, "issued_at": issued_at, "signer": signer,
            "present": list(present), "may_contact": list(may_contact), "absent": list(absent)}


def composition(source_id, links):
    return {"source_id": source_id, "links": links}


def link(parent, child, component=None, amount=10, by="QA", cleaned=True, issued_at=1):
    return {"parent": parent, "child": child, "component": component or child,
            "amount_g": amount, "declared_by": by, "issued_at": issued_at, "cleaned": cleaned}


def seeded_s2(tmp):
    """The S -> S2 substitution story: partial certificate covers only the soy base."""
    cx = db(tmp)
    with cx:
        meal.run(cx, fixture("meal-s2.json"))
        supplier.composition_run(cx, fixture("composition-s.json"))
        supplier.composition_run(cx, fixture("composition-ok.json"))
        supplier.run(cx, fixture("supplier-soy-base.json"))
        supplier.run(cx, fixture("supplier-sesame-absent.json"))
        supplier.run(cx, fixture("supplier-sesame-may.json"))
        supplier.substitution_run(cx, fixture("substitution-ok.json"))
    return cx


def test_partial_substitution_certificate_scopes_inheritance(tmp_path):
    cx = seeded_s2(tmp_path)
    with cx:
        result = supplier.label(cx, "lunch-2")
    # soy base proof carries over through the certificate; sesame stays only may-contact
    assert result == {"meal": "lunch-2", "status": "draft",
                      "present": ["soy"], "may_contact": ["sesame"]}
    row = cx.execute("SELECT * FROM meal_label WHERE meal_id='lunch-2'").fetchone()
    assert json.loads(row["unknown"]) == []
    # the old sauce S evidence proving sesame-adj free of sesame must not leak into S2
    lots = {r["lot_id"] for r in cx.execute("SELECT * FROM label_trace WHERE meal_id='lunch-2'")}
    assert "sesame-adj" not in lots
    soy_trace = cx.execute(
        "SELECT * FROM label_trace WHERE meal_id='lunch-2' AND allergen='soy'").fetchone()
    assert soy_trace["lot_id"] == "soy-base-2"
    assert soy_trace["via"] == "sub-s-to-s2"


def test_substitution_certificate_partial_scope_blocks_wholesale_label_reuse(tmp_path):
    cx = db(tmp_path)
    with cx:
        meal.run(cx, fixture("meal-s2.json"))
        supplier.composition_run(cx, fixture("composition-s.json"))
        supplier.composition_run(cx, fixture("composition-ok.json"))
        supplier.run(cx, fixture("supplier-soy-base.json"))
        supplier.run(cx, fixture("supplier-sesame-absent.json"))
        # no certificate, no declaration for S2's own soy base: old S label cannot be reused
        assert supplier.label(cx, "lunch-2")["status"] == "pending"
        # a certificate covering only the soy base still leaves sesame to S2's own evidence
        supplier.substitution_run(cx, fixture("substitution-ok.json"))
        result = supplier.label(cx, "lunch-2")
    assert result["status"] == "pending"  # sesame-adj-2 has no declaration of its own yet
    with cx:
        supplier.run(cx, fixture("supplier-sesame-may.json"))
        result = supplier.label(cx, "lunch-2")
    assert result["status"] == "draft"
    assert result["may_contact"] == ["sesame"]


def test_single_signer_substitution_certificate_is_invalid(tmp_path):
    cx = db(tmp_path)
    bad = fixture("substitution-ok.json")
    bad["source_id"] = "sub-bad"
    bad["signer_b"] = bad["signer_a"]
    with cx:
        meal.run(cx, fixture("meal-s2.json"))
        supplier.composition_run(cx, fixture("composition-ok.json"))
        supplier.run(cx, fixture("supplier-soy-base.json"))
        result = supplier.substitution_run(cx, bad)
        assert result["valid"] is False
        assert supplier.label(cx, "lunch-2")["status"] == "pending"
    row = cx.execute("SELECT valid FROM substitution_cert WHERE source_id='sub-bad'").fetchone()
    assert row["valid"] == 0


def test_one_to_many_composition_and_duplicate_paths_count_once(tmp_path):
    cx = db(tmp_path)
    with cx:
        meal.run(cx, meal_payload("meal-p", "lunch-3", ["P"]))
        supplier.composition_run(cx, composition("comp-p", [
            link("P", "Q"), link("P", "R"),
            link("Q", "shared-lot"), link("R", "shared-lot")]))
        supplier.run(cx, declaration("decl-shared", "shared-lot", "QA", present=["soy"]))
        result = supplier.label(cx, "lunch-3")
    assert result == {"meal": "lunch-3", "status": "draft",
                      "present": ["soy"], "may_contact": []}
    rows = cx.execute(
        "SELECT * FROM label_trace WHERE meal_id='lunch-3' AND lot_id='shared-lot'").fetchall()
    assert len(rows) == 1  # the same child lot via two paths is one evidence source
    assert len(json.loads(rows[0]["path"])) == 2


def test_opposite_declarations_force_review_and_isolate_unrelated_meals(tmp_path):
    cx = db(tmp_path)
    with cx:
        meal.run(cx, meal_payload("meal-a", "lunch-4", ["L1"]))
        meal.run(cx, meal_payload("meal-b", "lunch-5", ["L2"]))
        supplier.run(cx, declaration("decl-a", "L1", "QA-A", present=["nuts"]))
        supplier.run(cx, declaration("decl-b", "L1", "QA-B", absent=["nuts"]))
        supplier.run(cx, declaration("decl-c", "L2", "QA-A", present=["soy"]))
        conflicted = supplier.label(cx, "lunch-4")
        unrelated = supplier.label(cx, "lunch-5")
    assert conflicted["status"] == "review"
    row = cx.execute("SELECT * FROM meal_label WHERE meal_id='lunch-4'").fetchone()
    assert "nuts" in row["reason"] and "L1" in row["reason"]
    detail = json.loads(row["detail"])
    assert detail["conflicts"] == [{"type": "declaration", "lot": "L1",
                                    "allergen": "nuts", "sources": ["decl-a", "decl-b"]}]
    # unrelated dish keeps its draft and can still be signed
    assert unrelated == {"meal": "lunch-5", "status": "draft",
                         "present": ["soy"], "may_contact": []}
    with cx:
        held = supplier.sign(cx, "lunch-4", "school-window")
        issued = supplier.sign(cx, "lunch-5", "school-window")
    assert held["decision"] == "held" and "review" in held["reason"]
    assert issued["decision"] == "issued"


def test_composition_cycle_forces_review_with_scope(tmp_path):
    cx = db(tmp_path)
    with cx:
        meal.run(cx, meal_payload("meal-x", "lunch-6", ["X"]))
        meal.run(cx, meal_payload("meal-y", "lunch-7", ["Z"]))
        supplier.composition_run(cx, composition("comp-cycle", [link("X", "Y"), link("Y", "X")]))
        supplier.run(cx, declaration("decl-z", "Z", "QA", present=["soy"]))
        result = supplier.label(cx, "lunch-6")
        other = supplier.label(cx, "lunch-7")
    assert result["status"] == "review"
    row = cx.execute("SELECT * FROM meal_label WHERE meal_id='lunch-6'").fetchone()
    detail = json.loads(row["detail"])
    assert any(c["type"] == "cycle" for c in detail["conflicts"])
    cycle = next(c for c in detail["conflicts"] if c["type"] == "cycle")
    assert cycle["path"] == ["X", "Y", "X"]
    assert other["status"] == "draft"  # cycle does not leak into unrelated meals


def test_two_commissions_decide_differently_on_same_evidence(tmp_path):
    cx = seeded_s2(tmp_path)
    with cx:
        ordinary = supplier.sign(cx, "lunch-2", "school-window")
        zero = supplier.sign(cx, "lunch-2", "sesame-zero-contact")
    assert ordinary["decision"] == "issued"
    assert ordinary["present"] == ["soy"] and ordinary["may_contact"] == ["sesame"]
    assert zero["decision"] == "held"
    assert "sesame-adj-2" in zero["reason"]
    rows = cx.execute("SELECT * FROM label_signoff WHERE meal_id='lunch-2' ORDER BY id").fetchall()
    assert [(r["commission"], r["decision"]) for r in rows] == [
        ("school-window", "issued"), ("sesame-zero-contact", "held")]


def test_zero_contact_commission_issues_only_with_full_proof(tmp_path):
    cx = db(tmp_path)
    with cx:
        meal.run(cx, meal_payload("meal-z", "lunch-8", ["Z1", "Z2"]))
        supplier.run(cx, declaration("decl-z1", "Z1", "QA", present=["soy"], absent=["sesame"]))
        supplier.run(cx, declaration("decl-z2", "Z2", "QA", absent=["sesame"]))
        zero = supplier.sign(cx, "lunch-8", "sesame-zero-contact")
        ordinary = supplier.sign(cx, "lunch-8", "school-window")
    assert zero["decision"] == "issued"
    assert ordinary["decision"] == "issued"


def test_unproven_is_not_absent_for_zero_contact(tmp_path):
    cx = db(tmp_path)
    with cx:
        meal.run(cx, meal_payload("meal-u", "lunch-9", ["U1", "U2"]))
        supplier.run(cx, declaration("decl-u1", "U1", "QA", present=["soy"]))
        supplier.run(cx, declaration("decl-u2", "U2", "QA", absent=["sesame"]))
        result = supplier.label(cx, "lunch-9")
        zero = supplier.sign(cx, "lunch-9", "sesame-zero-contact")
        ordinary = supplier.sign(cx, "lunch-9", "school-window")
    assert result["status"] == "draft"
    row = cx.execute("SELECT unknown FROM meal_label WHERE meal_id='lunch-9'").fetchone()
    assert json.loads(row["unknown"]) == ["sesame"]  # U1 never mentions sesame
    assert zero["decision"] == "held" and "U1" in zero["reason"]
    assert ordinary["decision"] == "issued" and ordinary["unknown"] == ["sesame"]


def test_signoff_versions_are_append_only(tmp_path):
    cx = db(tmp_path)
    with cx:
        meal.run(cx, meal_payload("meal-s", "lunch-10", ["M"]))
        supplier.run(cx, declaration("decl-m1", "M", "QA", present=["soy"]))
        first = supplier.sign(cx, "lunch-10", "school-window")
        supplier.run(cx, declaration("decl-m2", "M", "QA", may_contact=["sesame"], issued_at=20))
        second = supplier.sign(cx, "lunch-10", "school-window")
    assert (first["version"], first["decision"]) == (1, "issued")
    assert (second["version"], second["decision"]) == (2, "issued")
    rows = cx.execute(
        "SELECT * FROM label_signoff WHERE meal_id='lunch-10' ORDER BY version").fetchall()
    assert len(rows) == 2
    assert json.loads(rows[0]["may_contact"]) == []  # the old signed version is untouched
    assert json.loads(rows[1]["may_contact"]) == ["sesame"]


def test_menu_version_constrains_which_label_can_be_signed(tmp_path):
    cx = db(tmp_path)
    with cx:
        meal.run(cx, meal_payload("meal-v1", "lunch-11", ["N1"]))
        supplier.run(cx, declaration("decl-n1", "N1", "QA", present=["soy"]))
        assert supplier.label(cx, "lunch-11")["status"] == "draft"
        # a newer served version arrives with a different lot; the draft is now stale
        meal.run(cx, meal_payload("meal-v2", "lunch-11", ["N2"], version=2, signed_at=100))
        stale = supplier.sign(cx, "lunch-11", "school-window")
        assert stale["decision"] == "held"
        assert "version" in stale["reason"]
        # re-drafting on the current version still needs the new lot's declaration
        assert supplier.label(cx, "lunch-11")["status"] == "pending"
        supplier.run(cx, declaration("decl-n2", "N2", "QA", present=["soy"]))
        assert supplier.label(cx, "lunch-11")["status"] == "draft"
        fresh = supplier.sign(cx, "lunch-11", "school-window")
    assert fresh["decision"] == "issued"


def test_recalled_child_lot_blocks_meal_and_cleaning_cannot_lift_it(tmp_path):
    cx = db(tmp_path)
    with cx:
        meal.run(cx, meal_payload("meal-sauce", "lunch-12", ["SAUCE"]))
        supplier.composition_run(cx, composition("comp-sauce", [link("SAUCE", "child-x")]))
        supplier.run(cx, declaration("decl-child-x", "child-x", "QA", present=["soy"]))
        assert cx.execute("SELECT status FROM meal_batch WHERE id='lunch-12'").fetchone()[0] == "served"
        # the child lot is recalled elsewhere with a spotless cleaning record
        recall = meal_payload("meal-recall-x", "lunch-13", ["child-x"], version=1)
        recall["ingredients"][0]["recalled"] = True
        recall["ingredients"][0]["cleaned"] = True
        meal.run(cx, recall)
    assert cx.execute("SELECT status FROM meal_batch WHERE id='lunch-12'").fetchone()[0] == "recalled"
    assert cx.execute("SELECT status FROM meal_batch WHERE id='lunch-13'").fetchone()[0] == "recalled"


def test_undeclared_proven_allergen_holds_meal_and_sample(tmp_path):
    cx = db(tmp_path)
    with cx:
        payload = meal_payload("meal-plain", "lunch-14", ["PL"], allergen="none")
        meal.run(cx, payload)
        sample.run(cx, {"source_id": "sample-14",
                        "sample": {"id": "jar-14", "meal_id": "lunch-14", "cabinet": "cabinet-1",
                                   "collected_at": 100, "gross_g": 200, "tare_g": 10, "seal": "s"},
                        "custody": []})
        assert cx.execute("SELECT status FROM sample_seal WHERE id='jar-14'").fetchone()[0] == "retained"
        supplier.run(cx, declaration("decl-pl", "PL", "QA", present=["nuts"]))
    assert cx.execute("SELECT status FROM meal_batch WHERE id='lunch-14'").fetchone()[0] == "allergen_hold"
    assert cx.execute("SELECT status FROM sample_seal WHERE id='jar-14'").fetchone()[0] == "meal_hold"


def test_same_source_replay_is_idempotent(tmp_path):
    cx = db(tmp_path)
    with cx:
        meal.run(cx, fixture("meal-s2.json"))
        supplier.composition_run(cx, fixture("composition-ok.json"))
        again = supplier.composition_run(cx, fixture("composition-ok.json"))
        supplier.substitution_run(cx, fixture("substitution-ok.json"))
        assert supplier.substitution_run(cx, fixture("substitution-ok.json"))["status"] == "duplicate"
    assert again["status"] == "duplicate"
    assert len(report.build(cx)["composition_link"]) == 2
    assert len(report.build(cx)["substitution_cert"]) == 1
    assert cx.execute("SELECT COUNT(*) FROM source_conflict").fetchone()[0] == 0


def test_composition_requires_full_provenance(tmp_path):
    cx = db(tmp_path)
    bad = composition("comp-bad", [{"parent": "P", "child": "C", "amount_g": 1}])
    with pytest.raises(ValueError):
        with cx:
            supplier.composition_run(cx, bad)


def test_report_lists_component_paths_and_signoff_sources(tmp_path):
    cx = seeded_s2(tmp_path)
    with cx:
        supplier.sign(cx, "lunch-2", "school-window")
        supplier.sign(cx, "lunch-2", "sesame-zero-contact")
    view = report.build(cx)
    assert len(view["composition_link"]) == 4
    assert len(view["substitution_cert"]) == 1
    trace = {(r["lot_id"], r["allergen"]): r for r in view["label_trace"] if r["meal_id"] == "lunch-2"}
    assert json.loads(trace[("sesame-adj-2", "sesame")]["path"]) == [["S2", "sesame-adj-2"]]
    assert trace[("sesame-adj-2", "sesame")]["source_id"] == "supplier-sesame-adj-2"
    assert trace[("soy-base-2", "soy")]["via"] == "sub-s-to-s2"
    signoffs = {(r["commission"], r["decision"]) for r in view["label_signoff"]}
    assert signoffs == {("school-window", "issued"), ("sesame-zero-contact", "held")}
