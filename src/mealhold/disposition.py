"""Physical portion disposition: one-time numbering, transfers, closeout.

The primary result of this module is whether a production batch can be settled
and how many physical portions have no accounted destination. Menu person
counts and laboratory/recipe verdicts are never used as a substitute for a
portion leaving the kitchen with an independent outlet signature.

Rules implemented here:

* Every unit of food is a portion ID created exactly once. Reprocessing,
  plating, split and merge events never create a new served count; they move
  the same physical portion along a directed, acyclic graph.
* A split records parent and child IDs plus both actual masses. The two halves
  are physical children, never a second whole portion, and the parent is
  consumed by the split so parent and children are never counted together.
* A merge consumes named source portions into one new portion; no cycle is
  permitted, so a rework loop cannot mint units.
* Only a portion whose terminal event is ``served`` and which is backed by an
  independent outlet receipt counts as sent out. Destruction needs two
  distinct signers and an explicit portion list. A portion with no receipt is
  neither guessed as served nor written off as destroyed.
* A station held by later independent inspection does not erase the physical
  disposition of portions that passed through it, but those portions cannot
  be counted as verified servable (``unverified``).
"""

from .store import canonical, ingest, payload_digest

CONTRACTS = ("portion", "limit")

# Event kinds that move a portion's material into descendants.
SPLIT_KINDS = ("split", "merge")


def _digest(data):
    return payload_digest(data)


def _require(cond, message):
    if not cond:
        raise ValueError(message)


def _portion(db, pid):
    return db.execute("SELECT * FROM meal_portion WHERE portion_id=?", (pid,)).fetchone()


def _stage(db, data, digest):
    """Write one event's raw records without yet deciding the whole batch."""
    meal_id = data["meal_id"]
    if not db.execute("SELECT 1 FROM meal_batch WHERE id=?", (meal_id,)).fetchone():
        raise ValueError("meal must be recorded before portion evidence")
    produced = data.get("produce", [])
    events = data.get("events", [])
    receipts = data.get("receipts", [])
    destructions = data.get("destructions", [])
    stock_counts = data.get("counts", [])
    discrepancies = data.get("discrepancies", [])

    for item in produced:
        pid = item["portion_id"]
        if _portion(db, pid):
            raise ValueError(f"portion {pid} was already produced once")
        _require(float(item.get("mass_g", 0)) >= 0, f"portion {pid} mass must be non-negative")
        units = float(item.get("units", 1))
        _require(units > 0, f"portion {pid} must have positive units")
        db.execute(
            "INSERT INTO meal_portion(portion_id,meal_id,produced_at,station,mass_g,units,"
            "produced_by,origin,source_id,source_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                meal_id,
                int(item.get("produced_at", data.get("at", 0))),
                item.get("station"),
                float(item.get("mass_g", 0)),
                units,
                item.get("produced_by"),
                "produced",
                data["source_id"],
                digest,
            ),
        )
        db.execute(
            "INSERT INTO portion_event(portion_id,meal_id,kind,at,station,container,mass_g,"
            "actor,source_id,source_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pid, meal_id, "produced", int(item.get("produced_at", data.get("at", 0))),
             item.get("station"), item.get("container"), float(item.get("mass_g", 0)),
             item.get("produced_by"), data["source_id"], digest),
        )

    for event in events:
        _stage_event(db, meal_id, event, data["source_id"], digest)

    for receipt in receipts:
        _require(receipt.get("receipt_id"), "receipt needs receipt_id")
        _require(
            receipt.get("portion_id") and _portion(db, receipt["portion_id"]),
            f"receipt references unknown portion {receipt.get('portion_id')}",
        )
        _require(
            _portion(db, receipt["portion_id"])["meal_id"] == meal_id,
            "receipt references a portion of another meal",
        )
        db.execute(
            "INSERT OR IGNORE INTO serving_receipt(receipt_id,portion_id,meal_id,outlet,at,"
            "signer,signature,source_id,source_digest) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                receipt["receipt_id"], receipt["portion_id"], meal_id,
                receipt.get("outlet"), int(receipt.get("at", 0)),
                receipt.get("signer"), receipt.get("signature"),
                data["source_id"], digest,
            ),
        )

    for record in destructions:
        _stage_destruction(db, meal_id, record, data["source_id"], digest)

    for record in stock_counts:
        did = record["stock_id"]
        for pid in record.get("portions", []):
            _require(
                _portion(db, pid) and _portion(db, pid)["meal_id"] == meal_id,
                f"stock count references unknown portion {pid}",
            )
        existing = db.execute(
            "SELECT 1 FROM stock_count WHERE stock_id=?", (did,)
        ).fetchone()
        if existing:
            continue
        db.execute(
            "INSERT INTO stock_count(stock_id,meal_id,at,counted_by,note,source_id,source_digest) "
            "VALUES(?,?,?,?,?,?,?)",
            (did, meal_id, int(record.get("at", 0)), record.get("counted_by"),
             record.get("note"), data["source_id"], digest),
        )
        for pid in record.get("portions", []):
            db.execute(
                "INSERT OR IGNORE INTO stock_item(stock_id,portion_id) VALUES(?,?)",
                (did, pid),
            )

    for record in discrepancies:
        did = record["discrepancy_id"]
        _require(
            record.get("portion_id") and _portion(db, record["portion_id"]),
            f"discrepancy references unknown portion {record.get('portion_id')}",
        )
        existing = db.execute(
            "SELECT 1 FROM count_discrepancy WHERE discrepancy_id=?", (did,)
        ).fetchone()
        if existing:
            continue
        db.execute(
            "INSERT INTO count_discrepancy(discrepancy_id,meal_id,portion_id,at,signer_a,"
            "signer_b,note,source_id,source_digest) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                did, meal_id, record["portion_id"], int(record.get("at", 0)),
                record.get("signer_a"), record.get("signer_b"), record.get("note"),
                data["source_id"], digest,
            ),
        )


def _stage_event(db, meal_id, event, source_id, digest):
    kind = event["kind"]
    _require(kind in ("plated", "returned", "rework", "served", "destroyed", "stock", "split", "merge"),
             f"unknown event kind {kind}")
    pid = event.get("portion_id")
    at = int(event.get("at", 0))
    if kind in SPLIT_KINDS:
        _stage_edge(db, meal_id, event, source_id, digest)
    if pid is not None:
        _require(_portion(db, pid), f"event references unknown portion {pid}")
        _require(_portion(db, pid)["meal_id"] == meal_id,
                 f"portion {pid} belongs to another meal")
        db.execute(
            "INSERT INTO portion_event(portion_id,meal_id,kind,at,station,container,mass_g,"
            "actor,source_id,source_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pid, meal_id, kind, at, event.get("station"), event.get("container"),
             _opt_float(event.get("mass_g")), event.get("actor"), source_id, digest),
        )
    if kind == "destroyed" and event.get("disposal_id"):
        _stage_link_destruction(db, meal_id, event["disposal_id"], source_id, digest)


def _opt_float(value):
    return None if value is None else float(value)


def _stage_edge(db, meal_id, event, source_id, digest):
    kind = event["kind"]
    at = int(event.get("at", 0))
    parents = list(event.get("from", []))
    children = list(event.get("to", []))
    if "portion_id" in event and "parent_id" not in event:
        parents = [event["portion_id"]] + parents
    _require(parents, f"{kind} needs at least one source portion")
    _require(children, f"{kind} needs at least one destination portion")
    _require(len(set(parents)) == len(parents), f"{kind} lists a source portion twice")
    _require(len(set(children)) == len(children), f"{kind} lists a destination twice")
    for cid in children:
        _require(not _portion(db, cid), f"{kind} destination {cid} already exists; rework may not renumber")
    if kind == "split":
        _require(len(parents) == 1 and len(children) >= 1,
                 "split has one parent and one or more children")
    else:
        _require(len(children) == 1, "merge has exactly one destination portion")
    for pid in parents:
        row = _portion(db, pid)
        _require(row, f"{kind} references unknown portion {pid}")
        _require(row["meal_id"] == meal_id, f"portion {pid} belongs to another meal")

    # Mass bookkeeping: children carry at most what their parents supplied.
    given = event.get("mass_g")
    supplied = sum(_portion(db, p)["mass_g"] or 0 for p in parents)
    child_masses = event.get("child_mass_g") or []
    _require(len(child_masses) == len(children), f"{kind} needs a mass for every destination")
    child_masses = [float(m) for m in child_masses]
    _require(all(m >= 0 for m in child_masses), f"{kind} destination mass must be non-negative")
    _require(supplied > 0, f"{kind} sources carry no mass")
    _require(sum(child_masses) <= supplied + 1e-9,
             f"{kind} children ({sum(child_masses)}g) exceed parent mass ({supplied}g)")
    if given is not None:
        _require(abs(float(given) - supplied) <= 1e-9,
                 f"{kind} declared mass must equal the mass of its sources")

    for cid, cmass in zip(children, child_masses):
        parent_units = sum(_portion(db, p)["units"] for p in parents)
        # Children inherit the parents' units in proportion to the mass they took.
        units = parent_units * (cmass / supplied) if supplied else 0.0
        db.execute(
            "INSERT INTO meal_portion(portion_id,meal_id,produced_at,station,mass_g,units,"
            "produced_by,origin,source_id,source_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cid, meal_id, at, event.get("station"), cmass, units, event.get("actor"),
             kind, source_id, digest),
        )
        db.execute(
            "INSERT INTO portion_event(portion_id,meal_id,kind,at,station,container,mass_g,"
            "actor,source_id,source_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cid, meal_id, "produced_" + kind, at, event.get("station"), event.get("container"),
             cmass, event.get("actor"), source_id, digest),
        )
    for pid in parents:
        for cid, cmass in zip(children, child_masses):
            db.execute(
                "INSERT INTO portion_edge(parent_id,child_id,kind,mass_g,units,event_at,"
                "source_id,source_digest) VALUES(?,?,?,?,?,?,?,?)",
                (pid, cid, kind, cmass, _portion(db, cid)["units"], at, source_id, digest),
            )
    if _graph_has_cycle(db, meal_id):
        raise ValueError(f"{kind} would create a cycle in the disposition chain")


def _stage_destruction(db, meal_id, record, source_id, digest):
    did = record["disposal_id"]
    db.execute(
        "INSERT INTO destruction(disposal_id,meal_id,at,signer_a,signer_b,declared_qty,"
        "reason,source_id,source_digest) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(disposal_id) DO UPDATE SET at=excluded.at,"
        "signer_a=excluded.signer_a,signer_b=excluded.signer_b,"
        "declared_qty=excluded.declared_qty,reason=excluded.reason,"
        "source_id=excluded.source_id,source_digest=excluded.source_digest",
        (
            did, meal_id, int(record.get("at", 0)), record.get("signer_a"),
            record.get("signer_b"), int(record.get("declared_qty", 0)),
            record.get("reason"), source_id, digest,
        ),
    )
    for pid in record.get("portions", []):
        db.execute(
            "INSERT OR IGNORE INTO destruction_item(disposal_id,portion_id) VALUES(?,?)",
            (did, pid),
        )


def _stage_link_destruction(db, meal_id, disposal_id, source_id, digest):
    db.execute(
        "INSERT OR IGNORE INTO destruction(disposal_id,meal_id,at,signer_a,signer_b,"
        "declared_qty,reason,source_id,source_digest) VALUES(?,?,?,?,?,?,?,?,?)",
        (disposal_id, meal_id, 0, None, None, 0, None, source_id, digest),
    )


def _graph_has_cycle(db, meal_id):
    edges = db.execute(
        "SELECT parent_id,child_id FROM portion_edge WHERE EXISTS("
        "SELECT 1 FROM meal_portion p WHERE p.portion_id=portion_edge.child_id AND p.meal_id=?)",
        (meal_id,),
    ).fetchall()
    children = {}
    for row in edges:
        children.setdefault(row["parent_id"], set()).add(row["child_id"])
    color = {}

    def visit(node):
        state = color.get(node, 0)
        if state == 1:
            return True
        if state == 2:
            return False
        color[node] = 1
        for nxt in children.get(node, ()):
            if visit(nxt):
                return True
        color[node] = 2
        return False

    return any(visit(node) for node in list(children))


# --------------------------------------------------------------------------
# Reconciliation


def _graph(db, meal_id):
    portions = {
        r["portion_id"]: r
        for r in db.execute("SELECT * FROM meal_portion WHERE meal_id=?", (meal_id,))
    }
    events = db.execute(
        "SELECT * FROM portion_event WHERE meal_id=? ORDER BY at,id", (meal_id,)
    ).fetchall()
    edges = db.execute(
        "SELECT * FROM portion_edge WHERE child_id IN "
        "(SELECT portion_id FROM meal_portion WHERE meal_id=?)", (meal_id,)
    ).fetchall()
    return portions, events, edges


def _by_portion(events):
    out = {}
    for event in events:
        out.setdefault(event["portion_id"], []).append(event)
    return out


def _signed_receipts(db, meal_id):
    return {
        r["portion_id"]: r
        for r in db.execute(
            "SELECT * FROM serving_receipt WHERE meal_id=? AND signer IS NOT NULL "
            "AND signer<>'' AND signature IS NOT NULL AND signature<>''",
            (meal_id,),
        )
    }


def _valid_destruction(row):
    return bool(
        row["signer_a"] and row["signer_b"] and row["signer_a"] != row["signer_b"]
    )


def reconcile(db, meal_id):
    """Evaluate one meal's physical ledger and return its closeout inputs."""
    portions, events, edges = _graph(db, meal_id)
    events_by = _by_portion(events)
    children = {}
    parents = {}
    for edge in edges:
        children.setdefault(edge["parent_id"], set()).add(edge["child_id"])
        parents.setdefault(edge["child_id"], set()).add(edge["parent_id"])

    # Units are conserved along split/merge edges: roots carry the produced
    # unit count, splits divide it by mass, merges add their sources' units.
    roots = [pid for pid in portions if pid not in parents]
    produced_units = sum(float(portions[pid]["units"]) for pid in roots)
    leaves = [pid for pid in portions if not children.get(pid)]

    # A portion whose material passed a station later held by an independent
    # inspection keeps its physical destination, but its units cannot be
    # reported as verified servable.
    holds = {
        r["station"]
        for r in db.execute(
            "SELECT station FROM line_station_case WHERE meal_id=? AND status='hold'",
            (meal_id,),
        )
    }

    # Destruction coverage: portion -> valid destruction record.
    destroyed = {}
    invalid_destruction = set()
    for record in db.execute(
        "SELECT * FROM destruction WHERE meal_id=?", (meal_id,)
    ).fetchall():
        items = [
            r["portion_id"]
            for r in db.execute(
                "SELECT portion_id FROM destruction_item WHERE disposal_id=?",
                (record["disposal_id"],),
            )
        ]
        valid = (
            _valid_destruction(record)
            and bool(items)
            and int(record["declared_qty"]) == len(items)
        )
        if valid:
            for pid in items:
                destroyed[pid] = record
        else:
            invalid_destruction.update(items)

    receipts = _signed_receipts(db, meal_id)
    stock_listed = {
        r["portion_id"]
        for r in db.execute(
            "SELECT si.portion_id FROM stock_item si JOIN stock_count sc "
            "ON sc.stock_id=si.stock_id WHERE sc.meal_id=?", (meal_id,)
        )
    }

    # Station holds: a portion is unverified if its material physically passed
    # a station that an independent, later inspection holds.
    stations_by_portion = {}
    for event in events:
        if event["station"]:
            stations_by_portion.setdefault(event["portion_id"], set()).add(event["station"])
    for row in portions.values():
        if row["station"]:
            stations_by_portion.setdefault(row["portion_id"], set()).add(row["station"])

    lineage_cache = {}

    def lineage(pid):
        if pid in lineage_cache:
            return lineage_cache[pid]
        acc = set(stations_by_portion.get(pid, set()))
        for parent in parents.get(pid, ()):  # acyclic by construction
            acc |= lineage(parent)
        lineage_cache[pid] = acc
        return acc

    def passed_hold(pid):
        return bool(lineage(pid) & holds)

    served_units = 0.0
    destroyed_units = 0.0
    stock_units = 0.0
    unverified_units = 0.0
    verified_units = 0.0
    unknown = []          # portion IDs whose physical outcome cannot be established
    residual_units = 0.0  # split mass/units not assigned to a child
    conflicts = []
    detail = {}

    # A split parent must hand all of its units to children; any unassigned
    # remainder is physically unaccounted for.
    for pid, row in portions.items():
        if pid in children:
            assigned = sum(
                edge["units"]
                for edge in edges
                if edge["parent_id"] == pid
            )
            remainder = float(row["units"]) - assigned
            if remainder > 1e-9:
                residual_units += remainder
                unknown.append(pid + "~residual")

    for pid in leaves:
        units = float(portions[pid]["units"])
        own = events_by.get(pid, [])
        kinds = [e["kind"] for e in own]
        has_served = "served" in kinds
        has_stock = "stock" in kinds and pid in stock_listed
        terminal_claims = sum(
            [1 if has_served else 0, 1 if has_stock else 0,
             1 if pid in destroyed else 0]
        )
        if terminal_claims > 1:
            conflicts.append(pid + ":multiple-terminal-claims")
        signed_served = has_served and pid in receipts
        if terminal_claims == 1 and signed_served:
            served_units += units
            if passed_hold(pid):
                unverified_units += units
            else:
                verified_units += units
            terminal = "served"
        elif terminal_claims == 1 and has_stock:
            stock_units += units
            if passed_hold(pid):
                unverified_units += units
            terminal = "stock"
        elif terminal_claims == 1 and pid in destroyed:
            destroyed_units += units
            terminal = "destroyed"
        else:
            unknown.append(pid)
            terminal = None
        detail[pid] = {"units": units, "terminal": terminal,
                       "unverified": passed_hold(pid)}

    # A parent consumed by a split/merge cannot also be claimed as destroyed:
    # its material is already carried by its children.
    consumed = set(children)
    for pid in destroyed:
        if pid in consumed:
            conflicts.append(pid + ":destroyed-but-consumed-by-split")
    for pid in receipts:
        if pid in consumed:
            conflicts.append(pid + ":served-but-consumed-by-split")

    pending_units = sum(float(portions[p]["units"]) for p in unknown if p in portions)
    pending_units += residual_units

    valid_discrepancy = {}
    for row in db.execute(
        "SELECT * FROM count_discrepancy WHERE meal_id=?", (meal_id,)
    ).fetchall():
        if (
            row["signer_a"]
            and row["signer_b"]
            and row["signer_a"] != row["signer_b"]
            and row["portion_id"] in unknown
        ):
            valid_discrepancy[row["portion_id"]] = row

    result = {
        "meal_id": meal_id,
        "produced_units": produced_units,
        "served_units": served_units,
        "destroyed_units": destroyed_units,
        "stock_units": stock_units,
        "unverified_units": unverified_units,
        "verified_servable_units": verified_units,
        "unknown_units": pending_units,
        "unknown_portions": sorted(unknown),
        "conflicts": sorted(conflicts),
        "invalid_destruction_portions": sorted(invalid_destruction),
        "signed_discrepancies": sorted(valid_discrepancy),
        "portion_detail": detail,
    }
    return result


def run(db, data):
    """Stage one signed disposition source and re-evaluate its meal."""
    state = ingest(db, "portion", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    digest = _digest(data)
    _stage(db, data, digest)
    result = reconcile(db, data["meal_id"])
    _decide_evidence(db, data["source_id"], result)
    return {
        "source_id": data["source_id"],
        "meal": data["meal_id"],
        "status": "recorded",
        "portions_recorded": len(data.get("produce", [])),
        "reconciliation": _summary(result),
    }


def _summary(result):
    return {
        "produced": result["produced_units"],
        "served": result["served_units"],
        "destroyed": result["destroyed_units"],
        "stock": result["stock_units"],
        "verified_servable": result["verified_servable_units"],
        "unverified": result["unverified_units"],
        "unknown": result["unknown_units"],
        "unknown_portions": result["unknown_portions"],
    }

def _decide_evidence(db, source_id, result):
    from .store import decide

    decide(
        db,
        "portion-disposition",
        result["meal_id"],
        "candidate",
        "physical portion ledger staged from signed source",
        [source_id, _summary(result)],
    )


def closeout(db, data):
    """Close one production batch under exactly one pre-approved contract.

    ``contract`` is chosen by the caller and is never switched once a closeout
    has been recorded for the meal, even if that closeout was ``open``.
    """
    from .store import decide

    state = ingest(db, "closeout", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    meal_id = data["meal_id"]
    contract = data["contract"]
    _require(contract in CONTRACTS, f"unknown closeout contract {contract}")
    prior = db.execute(
        "SELECT * FROM closeout WHERE meal_id=? ORDER BY id DESC LIMIT 1", (meal_id,)
    ).fetchone()
    if prior and prior["contract"] != contract:
        raise ValueError(
            f"meal {meal_id} already closed under contract {prior['contract']}; "
            "contract may not be changed after the fact"
        )
    decision = _evaluate_contract(db, meal_id, contract)
    closeout_id = f"{meal_id}:{contract}"
    db.execute(
        "INSERT INTO closeout(closeout_id,meal_id,contract,status,produced_units,"
        "served_units,destroyed_units,stock_units,unknown_units,unverified_units,"
        "verified_servable_units,pending_units,reason,evidence) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            closeout_id, meal_id, contract, decision["status"],
            decision["totals"]["produced"], decision["totals"]["served"],
            decision["totals"]["destroyed"], decision["totals"]["stock"],
            decision["totals"]["unknown"], decision["unverified_units"],
            decision["verified_servable_units"],
            0 if decision["settled"] else decision["totals"]["unknown"],
            decision["reason"], canonical(decision),
        ),
    )
    decide(
        db, "closeout", meal_id, decision["status"], decision["reason"],
        [data["source_id"], contract, decision["unknown_portions"]],
    )
    return decision


def _evaluate_contract(db, meal_id, contract):
    """Pure evaluation of one contract; does not write anything."""
    result = reconcile(db, meal_id)
    blockers = []
    if result["invalid_destruction_portions"]:
        blockers.append("destruction lacks two distinct signatures or an itemised quantity")
    if result["conflicts"]:
        blockers.append("a portion claims two terminal outcomes")
    if result["unverified_units"] > 1e-9:
        blockers.append(
            "portions passed a station held by a later independent inspection "
            "and cannot be counted as verified servable"
        )
    unknown_portions = list(result["unknown_portions"])
    signed = set(result["signed_discrepancies"])
    unsigned_unknown = [p for p in unknown_portions if p not in signed]
    conserved = abs(
        result["produced_units"]
        - (
            result["served_units"]
            + result["destroyed_units"]
            + result["stock_units"]
            + result["unknown_units"]
        )
    ) < 1e-9
    if not conserved:
        blockers.append("unit total does not conserve across the disposition chain")
    if contract == "portion":
        status = (
            "settled"
            if not unknown_portions and not blockers
            else "open"
        )
        reason = (
            "every produced portion reaches a signed outlet receipt, dual-signed "
            "destruction or countable stock with a conserving, acyclic chain"
            if status == "settled"
            else "portion-level contract cannot settle with unknown or unverifiable portions"
        )
    else:
        if len(unknown_portions) > 1:
            blockers.append("more than one unknown portion exceeds the limit contract")
        if unsigned_unknown:
            blockers.append(
                "unknown portions without two distinct duty-manager discrepancy signatures"
            )
        status = "settled" if not blockers else "open"
        reason = (
            "at most one unknown portion with a dual-signed count discrepancy"
            if status == "settled"
            else "limit contract cannot settle: " + "; ".join(blockers)
        )
    return {
        "meal": meal_id,
        "contract": contract,
        "status": status,
        "settled": status == "settled",
        "reason": reason,
        "totals": _summary(result),
        "conserved": conserved,
        "verified_servable_units": result["verified_servable_units"],
        "unverified_units": result["unverified_units"],
        "unknown_portions": unknown_portions,
        "accountable_units": result["unknown_units"],
        "blockers": blockers,
    }


def preview(db, meal_id):
    """Both pre-approved contracts' decisions for the same account (read-only)."""
    return {contract: _evaluate_contract(db, meal_id, contract) for contract in CONTRACTS}


def servable_units(db, meal_id):
    """Units that are physically delivered and not tainted by a held station."""
    result = reconcile(db, meal_id)
    return {
        "verified_servable_units": result["verified_servable_units"],
        "unverified_units": result["unverified_units"],
        "served_units": result["served_units"],
        "destroyed_units": result["destroyed_units"],
        "unknown_units": result["unknown_units"],
    }
