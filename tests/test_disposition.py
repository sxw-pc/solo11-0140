"""Physical portion disposition, contract closeout, and late line-hold tests."""
from pathlib import Path

import pytest

from mealhold import cold, disposition, line, meal, report, sample
from mealhold.store import connect, read

ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def fixture(name):
    return read(ROOT / name)


def seeded(tmp_path):
    db = connect(tmp_path / "case.sqlite")
    with db:
        meal.run(db, fixture("meal-ok.json"))
        sample.run(db, fixture("sample-ok.json"))
        cold.run(db, fixture("cold-ok.json"))
        line.run(db, fixture("line-ok.json"))
    return db


def account(tmp_path, name="disposition-ok.json"):
    db = seeded(tmp_path)
    with db:
        disposition.run(db, fixture(name))
    return db


# --------------------------------------------------------------------------
# The full worked account


def test_full_account_is_98_served_2_destroyed_not_108(tmp_path):
    db = account(tmp_path)
    result = disposition.reconcile(db, "lunch-1")
    assert result["produced_units"] == 100
    assert result["served_units"] == 98
    assert result["destroyed_units"] == 2
    assert result["unknown_units"] == 0
    assert result["served_units"] + result["destroyed_units"] == result["produced_units"]


def test_rework_never_creates_second_served_count(tmp_path):
    db = account(tmp_path)
    # P091-P098 each have returned + rework + served on the same portion ID.
    rows = db.execute(
        "SELECT portion_id,kind FROM portion_event WHERE kind='rework' ORDER BY portion_id"
    ).fetchall()
    assert [r["portion_id"] for r in rows] == [f"P{n:03d}" for n in range(91, 99)]
    # The eight are already counted inside the 98 served units.
    assert db.execute(
        "SELECT COUNT(*) FROM meal_portion WHERE portion_id LIKE 'P%'"
    ).fetchone()[0] == 100


def test_full_account_settles_under_both_contracts(tmp_path):
    db = account(tmp_path)
    with db:
        portion = disposition.closeout(
            db, {**fixture("closeout-ok.json"), "source_id": "co-portion"}
        )
    assert portion["settled"] and portion["contract"] == "portion"
    assert portion["totals"]["served"] == 98
    assert portion["totals"]["destroyed"] == 2


# --------------------------------------------------------------------------
# Missing outlet signature


def test_missing_receipt_is_unknown_not_served_or_destroyed(tmp_path):
    db = account(tmp_path, "disposition-missing-receipt.json")
    result = disposition.reconcile(db, "lunch-1")
    assert result["served_units"] == 97
    assert result["destroyed_units"] == 2
    assert result["unknown_units"] == 1
    assert result["unknown_portions"] == ["P098"]


def test_missing_receipt_portion_contract_stays_open(tmp_path):
    db = account(tmp_path, "disposition-missing-receipt.json")
    with db:
        result = disposition.closeout(
            db, {**fixture("closeout-ok.json"), "source_id": "co-portion-missing"}
        )
    assert not result["settled"]
    assert result["unknown_portions"] == ["P098"]


def test_missing_receipt_limit_contract_settles_with_dual_signed_discrepancy(tmp_path):
    db = account(tmp_path, "disposition-missing-receipt.json")
    with db:
        result = disposition.closeout(
            db, {**fixture("closeout-limit-full.json"), "source_id": "co-limit-missing"}
        )
    assert result["settled"] and result["contract"] == "limit"
    assert result["totals"]["served"] == 97
    assert result["totals"]["destroyed"] == 2
    assert result["totals"]["unknown"] == 1
    # The unknown portion is listed, never counted as served or destroyed.
    assert result["unknown_portions"] == ["P098"]


def test_limit_contract_needs_two_distinct_discrepancy_signers(tmp_path):
    db = seeded(tmp_path)
    data = fixture("disposition-missing-receipt.json")
    data["source_id"] = "missing-with-bad-signature"
    data["discrepancies"][0]["signer_b"] = data["discrepancies"][0]["signer_a"]
    with db:
        disposition.run(db, data)
        result = disposition.closeout(
            db, {**fixture("closeout-limit-full.json"), "source_id": "co-bad-signature"}
        )
    assert not result["settled"]
    assert any("discrepancy" in b for b in result["blockers"])


def test_limit_contract_rejects_more_than_one_unknown(tmp_path):
    db = seeded(tmp_path)
    data = fixture("disposition-missing-receipt.json")
    data["source_id"] = "two-unknowns"
    # Drop P097's served evidence and receipt too.
    data["events"] = [
        e for e in data["events"]
        if not (e.get("portion_id") == "P097" and e["kind"] == "served")
    ]
    data["receipts"] = [r for r in data["receipts"] if r["portion_id"] != "P097"]
    with db:
        disposition.run(db, data)
        result = disposition.closeout(
            db, {**fixture("closeout-limit-full.json"), "source_id": "co-two-unknowns"}
        )
    assert not result["settled"]
    assert len(result["unknown_portions"]) == 2


def test_two_contracts_differ_for_the_same_account(tmp_path):
    db = account(tmp_path, "disposition-missing-receipt.json")
    preview = disposition.preview(db, "lunch-1")
    assert preview["portion"]["settled"] is False
    assert preview["limit"]["settled"] is True
    assert preview["limit"]["accountable_units"] == 1


def test_contract_cannot_be_switched_after_a_closeout(tmp_path):
    db = account(tmp_path, "disposition-missing-receipt.json")
    with db:
        disposition.closeout(
            db, {**fixture("closeout-limit-full.json"), "source_id": "co-lock-a"}
        )
    with pytest.raises(ValueError):
        with db:
            disposition.closeout(
                db, {**fixture("closeout-ok.json"), "source_id": "co-lock-b"}
            )


# --------------------------------------------------------------------------
# Split and merge bookkeeping


def test_half_boxes_never_count_as_two_whole_portions(tmp_path):
    db = account(tmp_path, "disposition-half-box.json")
    result = disposition.reconcile(db, "lunch-1")
    assert result["produced_units"] == 100
    assert result["served_units"] == 97.5
    assert result["unknown_units"] == 0.5
    assert result["unknown_portions"] == ["P098B"]
    # The parent P098 is consumed by the split: it is not also counted.
    assert "P098" not in result["portion_detail"]
    assert result["portion_detail"]["P098A"]["units"] == 0.5
    assert result["portion_detail"]["P098B"]["units"] == 0.5


def test_split_parent_and_children_are_not_double_counted(tmp_path):
    db = account(tmp_path, "disposition-half-box.json")
    produced = disposition.reconcile(db, "lunch-1")["produced_units"]
    assert produced == 100  # two half boxes add up to the one parent, not two
    # A second serve of the parent ID cannot inflate the count either.
    events = db.execute(
        "SELECT COUNT(*) FROM portion_event WHERE portion_id='P098' AND kind='served'"
    ).fetchone()[0]
    assert events == 0


def test_split_masses_are_recorded_per_child(tmp_path):
    db = account(tmp_path, "disposition-half-box.json")
    edges = db.execute(
        "SELECT parent_id,child_id,mass_g FROM portion_edge ORDER BY child_id"
    ).fetchall()
    assert [(e["parent_id"], e["child_id"], e["mass_g"]) for e in edges] == [
        ("P098", "P098A", 125.0),
        ("P098", "P098B", 125.0),
    ]


def test_half_box_contracts_split_on_the_half_portion(tmp_path):
    db = account(tmp_path, "disposition-half-box.json")
    preview = disposition.preview(db, "lunch-1")
    assert preview["portion"]["settled"] is False
    assert preview["limit"]["settled"] is True
    assert preview["limit"]["totals"]["served"] == 97.5
    assert preview["limit"]["totals"]["unknown"] == 0.5
    assert preview["limit"]["unknown_portions"] == ["P098B"]


def test_merge_consumes_named_source_portions(tmp_path):
    db = seeded(tmp_path)
    data = {
        "source_id": "merge-ok",
        "meal_id": "lunch-1",
        "produce": [
            {"portion_id": "M1", "mass_g": 100, "units": 1, "station": "tray-1"},
            {"portion_id": "M2", "mass_g": 100, "units": 1, "station": "tray-1"},
        ],
        "events": [
            {"kind": "merge", "at": 10, "station": "wok-1",
             "from": ["M1", "M2"], "to": ["M3"], "child_mass_g": [200]},
            {"kind": "served", "portion_id": "M3", "at": 20, "station": "outlet-1"},
        ],
        "receipts": [
            {"receipt_id": "RM3", "portion_id": "M3", "outlet": "outlet-1",
             "at": 20, "signer": "cashier-A", "signature": "sig-RM3"}
        ],
    }
    with db:
        disposition.run(db, data)
    result = disposition.reconcile(db, "lunch-1")
    assert result["produced_units"] == 2
    assert result["served_units"] == 2
    assert result["unknown_units"] == 0


def test_merge_cannot_reuse_an_existing_portion_id(tmp_path):
    db = seeded(tmp_path)
    data = {
        "source_id": "merge-renumber",
        "meal_id": "lunch-1",
        "produce": [{"portion_id": "M1", "mass_g": 100, "units": 1}],
        "events": [
            {"kind": "merge", "at": 10, "from": ["M1"], "to": ["M1"],
             "child_mass_g": [100]},
        ],
    }
    with pytest.raises(ValueError):
        with db:
            disposition.run(db, data)


def test_split_children_cannot_exceed_parent_mass(tmp_path):
    db = seeded(tmp_path)
    data = {
        "source_id": "split-heavy",
        "meal_id": "lunch-1",
        "produce": [{"portion_id": "S1", "mass_g": 100, "units": 1}],
        "events": [
            {"kind": "split", "at": 10, "portion_id": "S1",
             "to": ["S1A", "S1B"], "child_mass_g": [80, 80]},
        ],
    }
    with pytest.raises(ValueError):
        with db:
            disposition.run(db, data)


def test_portion_can_only_be_produced_once(tmp_path):
    db = seeded(tmp_path)
    data = {
        "source_id": "dup-produce",
        "meal_id": "lunch-1",
        "produce": [{"portion_id": "P001", "mass_g": 100, "units": 1}],
    }
    with db:
        disposition.run(db, data)
    again = {**data, "source_id": "dup-produce-2"}
    with pytest.raises(ValueError):
        with db:
            disposition.run(db, again)


# --------------------------------------------------------------------------
# Signature rules


def test_destruction_needs_two_distinct_signers(tmp_path):
    db = seeded(tmp_path)
    data = {
        "source_id": "bad-destruction",
        "meal_id": "lunch-1",
        "produce": [{"portion_id": "X1", "mass_g": 100, "units": 1}],
        "events": [{"kind": "destroyed", "portion_id": "X1", "at": 5}],
        "destructions": [
            {"disposal_id": "DX1", "at": 5, "signer_a": "duty-A",
             "signer_b": "duty-A", "declared_qty": 1, "portions": ["X1"]}
        ],
    }
    with db:
        disposition.run(db, data)
    result = disposition.reconcile(db, "lunch-1")
    assert result["destroyed_units"] == 0
    assert result["unknown_portions"] == ["X1"]
    with db:
        outcome = disposition.closeout(
            db, {"source_id": "co-bad-destruction", "meal_id": "lunch-1",
                 "contract": "portion"}
        )
    assert not outcome["settled"]


def test_destruction_quantity_must_match_listed_portions(tmp_path):
    db = seeded(tmp_path)
    data = {
        "source_id": "qty-mismatch",
        "meal_id": "lunch-1",
        "produce": [{"portion_id": "Y1", "mass_g": 100, "units": 1}],
        "events": [{"kind": "destroyed", "portion_id": "Y1", "at": 5}],
        "destructions": [
            {"disposal_id": "DY1", "at": 5, "signer_a": "duty-A",
             "signer_b": "duty-B", "declared_qty": 2, "portions": ["Y1"]}
        ],
    }
    with db:
        disposition.run(db, data)
    result = disposition.reconcile(db, "lunch-1")
    assert result["destroyed_units"] == 0
    assert result["unknown_portions"] == ["Y1"]


def test_unsigned_receipt_does_not_count_as_served(tmp_path):
    db = seeded(tmp_path)
    data = {
        "source_id": "unsigned-receipt",
        "meal_id": "lunch-1",
        "produce": [{"portion_id": "Z1", "mass_g": 100, "units": 1}],
        "events": [{"kind": "served", "portion_id": "Z1", "at": 5}],
        "receipts": [
            {"receipt_id": "RZ1", "portion_id": "Z1", "outlet": "outlet-1",
             "at": 5, "signer": "", "signature": ""}
        ],
    }
    with db:
        disposition.run(db, data)
    result = disposition.reconcile(db, "lunch-1")
    assert result["served_units"] == 0
    assert result["unknown_portions"] == ["Z1"]


def test_duplicate_receipt_does_not_double_count(tmp_path):
    db = seeded(tmp_path)
    data = {
        "source_id": "double-receipt",
        "meal_id": "lunch-1",
        "produce": [{"portion_id": "W1", "mass_g": 100, "units": 1}],
        "events": [{"kind": "served", "portion_id": "W1", "at": 5}],
        "receipts": [
            {"receipt_id": "RW1a", "portion_id": "W1", "outlet": "outlet-1",
             "at": 5, "signer": "cashier-A", "signature": "sig-a"},
            {"receipt_id": "RW1b", "portion_id": "W1", "outlet": "outlet-1",
             "at": 6, "signer": "cashier-A", "signature": "sig-b"},
        ],
    }
    with db:
        disposition.run(db, data)
    assert disposition.reconcile(db, "lunch-1")["served_units"] == 1


# --------------------------------------------------------------------------
# Late independent inspection holding a station


def test_late_hold_preserves_physical_totals_but_blocks_verified_serving(tmp_path):
    db = account(tmp_path)
    before = meal.servable_check(db, "lunch-1")
    with db:
        line.run(db, fixture("line-late-hold.json"))
    after = meal.servable_check(db, "lunch-1")

    # Physical disposition is unchanged and still conserves.
    assert after["physical"]["served_units"] == 98
    assert after["physical"]["destroyed_units"] == 2
    assert after["physical"]["unknown_units"] == 0
    # The eight reworked portions passed the held wok station.
    assert after["unverified"] == 8
    assert after["verified_servable"] == 90
    assert after["line_status"] == "hold"
    # The signed menu and count are untouched.
    assert after["menu_version"] == before["menu_version"] == 1
    assert after["signed_persons"] == before["signed_persons"] == 80
    assert after["menu_status"] == "served"


def test_late_hold_readjudicates_closeout_eligibility(tmp_path):
    db = account(tmp_path)
    with db:
        line.run(db, fixture("line-late-hold.json"))
        result = disposition.closeout(
            db, {**fixture("closeout-ok.json"), "source_id": "co-after-hold"}
        )
    assert not result["settled"]
    assert any("held" in b for b in result["blockers"])
    assert result["totals"]["produced"] == 100  # physical total still conserves
    assert result["verified_servable_units"] == 90


def test_late_hold_does_not_erase_signed_sample_evidence(tmp_path):
    db = account(tmp_path)
    sample_before = dict(db.execute("SELECT * FROM sample_seal").fetchone())
    meal_before = dict(db.execute("SELECT * FROM meal_batch").fetchone())
    with db:
        line.run(db, fixture("line-late-hold.json"))
    assert dict(db.execute("SELECT * FROM sample_seal").fetchone()) == sample_before
    assert dict(db.execute("SELECT * FROM meal_batch").fetchone()) == meal_before


def test_held_station_only_affects_portions_through_it(tmp_path):
    db = account(tmp_path)
    with db:
        line.run(db, fixture("line-late-hold.json"))
    result = disposition.reconcile(db, "lunch-1")
    # P001-P090 never touched wok-1, so they remain verified.
    assert result["portion_detail"]["P001"]["unverified"] is False
    assert result["portion_detail"]["P091"]["unverified"] is True


# --------------------------------------------------------------------------
# Report and CLI surface


def test_report_lists_disposition_and_closeout(tmp_path):
    db = account(tmp_path, "disposition-missing-receipt.json")
    with db:
        disposition.closeout(
            db, {**fixture("closeout-limit-full.json"), "source_id": "co-report"}
        )
    view = report.build(db)
    summary = view["disposition"]["lunch-1"]
    assert summary["produced"] == 100
    assert summary["served"] == 97
    assert summary["destroyed"] == 2
    assert summary["unknown"] == 1
    assert summary["closeout"]["contract"] == "limit"
    assert summary["closeout"]["status"] == "settled"
    assert len(view["closeout"]) == 1
    assert len(view["serving_receipt"]) == 97


def test_late_hold_is_not_cleared_by_a_later_passing_check(tmp_path):
    db = account(tmp_path)
    with db:
        line.run(db, fixture("line-late-hold.json"))
    cleared = fixture("line-late-hold.json")
    cleared["source_id"] = "line-late-hold-cleared"
    cleared["observed_at"] = 900
    cleared["checks"][0].update(inspected_ok=True, clean_stamp="wash-9")
    with db:
        line.run(db, cleared)
    case = db.execute(
        "SELECT status FROM line_station_case WHERE meal_id='lunch-1' AND station='wok-1'"
    ).fetchone()
    assert case["status"] == "hold"
    assert meal.servable_check(db, "lunch-1")["verified_servable"] == 90


def test_registrations_are_evidence_only(tmp_path):
    db = seeded(tmp_path)
    before = dict(db.execute("SELECT * FROM meal_batch").fetchone())
    with db:
        result = meal.registrations(db, fixture("registrations-ok.json"))
    assert result["outlets"] == 2 and result["duty_managers"] == 3
    assert dict(db.execute("SELECT * FROM meal_batch").fetchone()) == before


def test_countable_stock_balances_without_serving_or_destroying(tmp_path):
    db = seeded(tmp_path)
    data = {
        "source_id": "stock-balance",
        "meal_id": "lunch-1",
        "produce": [
            {"portion_id": "K1", "mass_g": 100, "units": 1},
            {"portion_id": "K2", "mass_g": 100, "units": 1},
        ],
        "events": [
            {"kind": "served", "portion_id": "K1", "at": 5, "station": "outlet-1"},
            {"kind": "stock", "portion_id": "K2", "at": 6, "station": "cooler-1"},
        ],
        "receipts": [
            {"receipt_id": "RK1", "portion_id": "K1", "outlet": "outlet-1",
             "at": 5, "signer": "cashier-A", "signature": "sig-RK1"}
        ],
        "counts": [
            {"stock_id": "SK2", "at": 6, "counted_by": "keeper-B", "portions": ["K2"]}
        ],
    }
    with db:
        disposition.run(db, data)
        result = disposition.closeout(
            db, {"source_id": "co-stock", "meal_id": "lunch-1", "contract": "portion"}
        )
    assert result["settled"]
    assert result["totals"]["served"] == 1
    assert result["totals"]["stock"] == 1
    assert result["totals"]["destroyed"] == 0


def test_units_conserve_across_the_chain(tmp_path):
    for i, name in enumerate(
        (
            "disposition-ok.json",
            "disposition-missing-receipt.json",
            "disposition-half-box.json",
        )
    ):
        case = tmp_path / f"case-{i}"
        case.mkdir()
        db = seeded(case)
        with db:
            disposition.run(db, fixture(name))
        result = disposition.reconcile(db, "lunch-1")
        total = (
            result["served_units"]
            + result["destroyed_units"]
            + result["stock_units"]
            + result["unknown_units"]
        )
        assert total == pytest.approx(result["produced_units"]), name


def test_stock_claim_without_countable_record_is_unknown(tmp_path):
    db = seeded(tmp_path)
    data = {
        "source_id": "stock-unlisted",
        "meal_id": "lunch-1",
        "produce": [{"portion_id": "Q1", "mass_g": 100, "units": 1}],
        "events": [{"kind": "stock", "portion_id": "Q1", "at": 5}],
    }
    with db:
        disposition.run(db, data)
    result = disposition.reconcile(db, "lunch-1")
    assert result["stock_units"] == 0
    assert result["unknown_portions"] == ["Q1"]


def test_disposition_source_replays_as_duplicate(tmp_path):
    db = account(tmp_path)
    with db:
        again = disposition.run(db, fixture("disposition-ok.json"))
    assert again["status"] == "duplicate"
    assert disposition.reconcile(db, "lunch-1")["served_units"] == 98

