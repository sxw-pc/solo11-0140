"""Supplier declarations, composite-ingredient composition, substitution certificates and label sign-off.

Evidence model:
- supplier_declaration: per-lot allergen conclusions (present / may_contact / absent), one signer each.
- composition_link: parent product -> child lot with amount and declaration source; optional cleaning proof.
- substitution_cert: dual-signed proof that a replacement product inherits only the listed
  child components and allergen categories from the product it replaces.

Labels are derived per meal from the lots of the *current* served menu version, expanded layer by
layer through composition links. The same child lot reached via two paths is one evidence source,
not two. Opposite valid declarations or composition cycles force the label into ``review``.
"""

import json

from .store import decide, ingest

# The two predetermined real delivery commissions.
COMMISSIONS = {
    "school-window": {
        "kind": "warning",
        "description": "校内窗口普通警示标签：证据不完整时明示可能接触即可发布",
    },
    "sesame-zero-contact": {
        "kind": "zero-contact",
        "allergen": "sesame",
        "description": "对外供餐芝麻零接触：所有实际子成分须明确不含芝麻且有清洗证明",
    },
}

LEVELS = {"absent": 0, "may_contact": 1, "present": 2}
LEVEL_NAMES = {v: k for k, v in LEVELS.items()}


def run(db, data):
    """Ingest one direct supplier declaration for an ingredient lot."""
    state = ingest(db, "supplier", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    lot = data["lot_id"]
    present = sorted(set(data.get("present", [])))
    may_contact = sorted(set(data.get("may_contact", [])))
    absent = sorted(set(data.get("absent", [])))
    if not data.get("signer"):
        raise ValueError("supplier signer required")
    db.execute(
        "INSERT INTO supplier_declaration(lot_id,issued_at,present,may_contact,absent,signer,source_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (lot, int(data["issued_at"]), json.dumps(present), json.dumps(may_contact),
         json.dumps(absent), data["signer"], data["source_id"]),
    )
    refresh_all(db)
    meals = [m for m in _meal_ids(db) if lot in analyze(db, m)["nodes"]]
    return {"lot": lot, "meals_updated": meals}


def composition_run(db, data):
    """Ingest composition evidence: parent product, child lot, amount, declaration source."""
    state = ingest(db, "composition", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    links = data.get("links", [])
    if not links:
        raise ValueError("composition links required")
    for link in links:
        for field in ("parent", "child", "amount_g", "declared_by"):
            if link.get(field) in (None, ""):
                raise ValueError(f"composition link missing {field}")
        db.execute(
            "INSERT INTO composition_link(parent,child,component,amount_g,declared_by,issued_at,cleaned,source_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (link["parent"], link["child"], link.get("component") or link["child"],
             float(link["amount_g"]), link["declared_by"], int(link.get("issued_at", 0)),
             int(bool(link.get("cleaned"))), data["source_id"]),
        )
    refresh_all(db)
    return {"source_id": data["source_id"], "links": len(links)}


def substitution_run(db, data):
    """Ingest a substitution certificate scoped to child components and allergen categories.

    A certificate is valid only with two different signers; an invalid certificate is recorded
    as evidence but inherits nothing.
    """
    state = ingest(db, "substitution", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    for field in ("from_product", "to_product"):
        if not data.get(field):
            raise ValueError(f"substitution certificate missing {field}")
    components = sorted(set(data.get("covers_components", [])))
    allergens = sorted(set(data.get("covers_allergens", [])))
    signer_a, signer_b = data.get("signer_a"), data.get("signer_b")
    valid = bool(signer_a and signer_b and signer_a != signer_b and components and allergens)
    db.execute(
        "INSERT INTO substitution_cert(from_product,to_product,components,allergens,signer_a,signer_b,"
        "issued_at,valid,source_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (data["from_product"], data["to_product"], json.dumps(components), json.dumps(allergens),
         signer_a, signer_b, int(data.get("issued_at", 0)), int(valid), data["source_id"]),
    )
    decide(db, "substitution", data["source_id"], "valid" if valid else "invalid",
           "dual sign-off plus component and allergen scope required",
           [data["source_id"]])
    refresh_all(db)
    return {"source_id": data["source_id"], "valid": valid}


def refresh_all(db):
    """Supplier evidence changed: re-decide serving safety and rebuild every label."""
    from .meal import reassess_serving
    for meal_id in _meal_ids(db):
        reassess_serving(db, meal_id)
        label(db, meal_id)


def _meal_ids(db):
    return [r["id"] for r in db.execute("SELECT id FROM meal_batch ORDER BY id").fetchall()]


def _current_lots(db, meal_id):
    meal = db.execute("SELECT * FROM meal_batch WHERE id=?", (meal_id,)).fetchone()
    if not meal:
        return None, db.execute(
            "SELECT * FROM ingredient_lot WHERE meal_id=?", (meal_id,)).fetchall()
    return meal, db.execute(
        "SELECT * FROM ingredient_lot WHERE meal_id=? AND menu_version=?",
        (meal_id, meal["menu_version"])).fetchall()


def _latest_per_signer(db, lot):
    rows = db.execute(
        "SELECT * FROM supplier_declaration WHERE lot_id=? ORDER BY issued_at DESC,id DESC",
        (lot,)).fetchall()
    seen, kept = set(), []
    for row in rows:
        if row["signer"] in seen:
            continue
        seen.add(row["signer"])
        kept.append(row)
    return kept


def _declaration_levels(row):
    out = {}
    for field, level in (("present", 2), ("may_contact", 1), ("absent", 0)):
        for allergen in json.loads(row[field]):
            out.setdefault(allergen, set()).add(level)
    return out


def _expand(db, roots):
    """Expand composition layer by layer. Returns (nodes, cycles, inherited).

    nodes: lot -> {"paths": [...], "children": [link rows]}
    inherited: child lot -> [{"from_lot", "allergens", "cert"}] scoped inheritance via valid certs.
    A lot already on the current path marks a cycle; a lot already expanded only gains a path.
    """
    nodes, cycles, inherited = {}, [], {}

    def visit(lot, path):
        if lot in path:
            cycles.append(path + [lot])
            return
        node = nodes.setdefault(lot, {"paths": [], "children": []})
        node["paths"].append(path + [lot])
        if len(node["paths"]) > 1 or node["children"]:
            return  # already expanded: same lot via another path is one source
        links = db.execute(
            "SELECT * FROM composition_link WHERE parent=? ORDER BY id", (lot,)).fetchall()
        for link in links:
            node["children"].append(dict(link))
            visit(link["child"], path + [lot])
        certs = db.execute(
            "SELECT * FROM substitution_cert WHERE to_product=? AND valid=1 ORDER BY id",
            (lot,)).fetchall()
        for cert in certs:
            components = json.loads(cert["components"])
            allergens = json.loads(cert["allergens"])
            for component in components:
                old_links = db.execute(
                    "SELECT * FROM composition_link WHERE parent=? AND component=? ORDER BY id",
                    (cert["from_product"], component)).fetchall()
                own = db.execute(
                    "SELECT * FROM composition_link WHERE parent=? AND component=? ORDER BY id",
                    (lot, component)).fetchall()
                for old in old_links:
                    target = own[0]["child"] if own else old["child"]
                    inherited.setdefault(target, []).append(
                        {"from_lot": old["child"], "allergens": allergens,
                         "cert": cert["source_id"]})
                    if not own:
                        visit(old["child"], path + [lot])

    for root in roots:
        visit(root, [])
    return nodes, cycles, inherited


def analyze(db, meal_id):
    """Resolve every allergen conclusion reachable from the meal's current served version."""
    meal, lot_rows = _current_lots(db, meal_id)
    version = meal["menu_version"] if meal else None
    roots = [r["id"] for r in lot_rows]
    nodes, cycles, inherited = _expand(db, roots)

    lots = {}
    conflicts = []
    for lot, node in nodes.items():
        entries = []  # (allergen, level, source_id, via)
        for row in _latest_per_signer(db, lot):
            for allergen, levels in _declaration_levels(row).items():
                for level in levels:
                    entries.append((allergen, level, row["source_id"], None))
        for inh in inherited.get(lot, []):
            for row in _latest_per_signer(db, inh["from_lot"]):
                levels = _declaration_levels(row)
                for allergen in inh["allergens"]:
                    for level in levels.get(allergen, set()):
                        entries.append((allergen, level, row["source_id"], inh["cert"]))
        per_allergen = {}
        for allergen, level, source, via in entries:
            slot = per_allergen.setdefault(allergen, {"levels": set(), "sources": [], "via": []})
            slot["levels"].add(level)
            if source not in slot["sources"]:
                slot["sources"].append(source)
            if via and via not in slot["via"]:
                slot["via"].append(via)
        effective = {}
        for allergen, slot in per_allergen.items():
            levels = slot["levels"]
            if 0 in levels and levels & {1, 2}:
                conflicts.append({"type": "declaration", "lot": lot, "allergen": allergen,
                                  "sources": sorted(slot["sources"])})
                continue  # no trustworthy conclusion while signers disagree
            effective[allergen] = max(levels)
        lots[lot] = {"paths": node["paths"], "children": node["children"],
                     "effective": effective, "per_allergen": per_allergen}

    for cycle in cycles:
        conflicts.append({"type": "cycle", "path": cycle})

    present, may_contact, absent_all = set(), set(), set()
    tracked = set()
    leaves = []
    missing = []
    for lot, info in lots.items():
        tracked.update(info["effective"])
        tracked.update(a for a, s in info["per_allergen"].items())
        is_leaf = not info["children"]
        if is_leaf and not info["effective"] and not info["per_allergen"]:
            missing.append(lot)
        if is_leaf:
            leaves.append(lot)
        for allergen, level in info["effective"].items():
            if level == 2:
                present.add(allergen)
            elif level == 1:
                may_contact.add(allergen)
    may_contact -= present
    for allergen in tracked - present - may_contact:
        # proven absent only when every leaf carries an explicit absence
        leaf_levels = [lots[lot]["effective"].get(allergen) for lot in leaves]
        if leaves and all(level == 0 for level in leaf_levels):
            absent_all.add(allergen)
    unknown = tracked - present - may_contact - absent_all

    cleaned = {}
    for lot in lots:
        row = db.execute("SELECT cleaned FROM ingredient_lot WHERE id=?", (lot,)).fetchone()
        link = db.execute(
            "SELECT 1 FROM composition_link WHERE child=? AND cleaned=1 LIMIT 1", (lot,)).fetchone()
        cleaned[lot] = bool((row and row["cleaned"]) or link)

    return {
        "meal": meal_id, "menu_version": version, "roots": roots, "nodes": lots,
        "leaves": sorted(leaves), "missing": sorted(missing), "conflicts": conflicts,
        "present": sorted(present), "may_contact": sorted(may_contact),
        "absent": sorted(absent_all), "unknown": sorted(unknown), "cleaned": cleaned,
    }


def label(db, meal_id):
    """Rebuild the draft label for one meal from current evidence."""
    a = analyze(db, meal_id)
    if a["conflicts"]:
        status = "review"
        scope = "; ".join(
            f"{c['type']}:{c.get('lot') or c.get('path')}"
            + (f"/{c['allergen']}" if c["type"] == "declaration" else "")
            for c in a["conflicts"])
        reason = f"conflicting evidence requires review ({scope})"
    elif not a["roots"] or a["missing"]:
        status = "pending"
        reason = "missing lot declaration"
    else:
        status = "draft"
        reason = "all direct lot declarations available"
    detail = {
        "leaves": [
            {"lot": lot,
             "present": sorted(x for x, l in a["nodes"][lot]["effective"].items() if l == 2),
             "may_contact": sorted(x for x, l in a["nodes"][lot]["effective"].items() if l == 1),
             "absent": sorted(x for x, l in a["nodes"][lot]["effective"].items() if l == 0),
             "cleaned": a["cleaned"][lot],
             "paths": a["nodes"][lot]["paths"]}
            for lot in a["leaves"]
        ],
        "conflicts": a["conflicts"],
        "menu_version": a["menu_version"],
    }
    db.execute(
        "INSERT INTO meal_label(meal_id,status,present,may_contact,unknown,reason,menu_version,detail) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(meal_id) DO UPDATE SET status=excluded.status,present=excluded.present,"
        "may_contact=excluded.may_contact,unknown=excluded.unknown,reason=excluded.reason,"
        "menu_version=excluded.menu_version,detail=excluded.detail",
        (meal_id, status, json.dumps(a["present"]), json.dumps(a["may_contact"]),
         json.dumps(a["unknown"]), reason, a["menu_version"], json.dumps(detail, sort_keys=True)),
    )
    db.execute("DELETE FROM label_trace WHERE meal_id=?", (meal_id,))
    for lot, info in a["nodes"].items():
        for allergen, slot in info["per_allergen"].items():
            if allergen not in info["effective"]:
                state = "conflict"
            else:
                state = LEVEL_NAMES[info["effective"][allergen]]
            db.execute(
                "INSERT INTO label_trace(meal_id,menu_version,allergen,state,lot_id,path,source_id,via) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (meal_id, a["menu_version"], allergen, state, lot,
                 json.dumps(info["paths"]), ",".join(slot["sources"]),
                 ",".join(slot["via"]) or None),
            )
    evidence = sorted({s for info in a["nodes"].values() for s in
                       (src for slot in info["per_allergen"].values() for src in slot["sources"])})
    decide(db, "label", meal_id, status, reason, evidence)
    return {"meal": meal_id, "status": status,
            "present": a["present"], "may_contact": a["may_contact"]}


def sign(db, meal_id, commission):
    """Attempt to sign the current draft label under a predetermined delivery commission.

    Every attempt appends a new sign-off version; earlier signed versions are never rewritten,
    so evidence that arrives later cannot silently alter a past decision.
    """
    if commission not in COMMISSIONS:
        raise ValueError(f"unknown commission {commission!r}; expected one of {sorted(COMMISSIONS)}")
    spec = COMMISSIONS[commission]
    meal = db.execute("SELECT * FROM meal_batch WHERE id=?", (meal_id,)).fetchone()
    lab = db.execute("SELECT * FROM meal_label WHERE meal_id=?", (meal_id,)).fetchone()
    holds = []
    if not meal:
        holds.append("unknown meal")
    if not lab:
        holds.append("no label draft")
    elif meal:
        if lab["menu_version"] != meal["menu_version"]:
            holds.append(
                f"label drafted for menu version {lab['menu_version']} "
                f"but served version is {meal['menu_version']}")
        if lab["status"] == "review":
            holds.append(f"label under review: {lab['reason']}")
        elif lab["status"] == "pending":
            holds.append("label pending: missing lot declaration")
        if meal["status"] != "served":
            holds.append(f"meal not served ({meal['status']})")
        if not holds and spec["kind"] == "zero-contact":
            allergen = spec["allergen"]
            detail = json.loads(lab["detail"])
            for leaf in detail["leaves"]:
                if allergen in leaf["present"] or allergen in leaf["may_contact"]:
                    holds.append(f"{leaf['lot']} does not exclude {allergen}")
                elif allergen not in leaf["absent"]:
                    holds.append(f"{leaf['lot']} lacks explicit absence of {allergen}")
                if not leaf["cleaned"]:
                    holds.append(f"{leaf['lot']} lacks cleaning proof")
    decision = "held" if holds else "issued"
    reason = "; ".join(holds) if holds else (
        "ordinary warning label issued" if spec["kind"] == "warning"
        else f"zero-contact {spec['allergen']} label issued")
    row = db.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM label_signoff WHERE meal_id=? AND commission=?",
        (meal_id, commission)).fetchone()
    version = row["v"] + 1
    present = json.loads(lab["present"]) if lab else []
    may_contact = json.loads(lab["may_contact"]) if lab else []
    unknown = json.loads(lab["unknown"]) if lab else []
    evidence = [d["source_id"] for d in db.execute(
        "SELECT DISTINCT source_id FROM label_trace WHERE meal_id=? AND source_id!=''",
        (meal_id,)).fetchall()] if lab else []
    db.execute(
        "INSERT INTO label_signoff(meal_id,commission,version,decision,reason,present,may_contact,"
        "unknown,menu_version,evidence) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (meal_id, commission, version, decision, reason, json.dumps(present),
         json.dumps(may_contact), json.dumps(unknown),
         lab["menu_version"] if lab else None, json.dumps(sorted(evidence))),
    )
    decide(db, f"sign:{commission}", meal_id, decision, reason, evidence)
    return {"meal": meal_id, "commission": commission, "version": version,
            "decision": decision, "reason": reason,
            "present": present, "may_contact": may_contact, "unknown": unknown}


def sign_run(db, data):
    return sign(db, data["meal_id"], data["commission"])
