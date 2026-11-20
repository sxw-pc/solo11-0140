"""Investigation alerts for post-meal illness reports.

Every report keeps its own claimed meal batch, one-time daily reporter
alias, household code, estimated symptom interval, report time and the
independent verification source.  Identity credentials and late time
checks arrive as independent evidence:

* a trusted same-person credential merges duplicate accounts into one
  case, while sharing a household never merges genuinely different
  people;
* contradictory credentials keep every feasible person/household
  explanation instead of letting the earliest report or the majority
  side decide the identity;
* a symptom interval fully inside a pre-registered observation window
  is definitely related, merely intersecting is possibly related, and a
  late time verification can reclassify a report either way;
* reports of adjacent meal sessions keep their own claims and are never
  borrowed to fill another session's case.

Two pre-registered contracts are evaluated per meal case:

* sentinel contract - issues a temporary check notice once at least
  three distinct persons possibly fall inside the window, noting any
  undecided identity or onset time; it never declares a cluster;
* formal cluster contract - signs an investigation order only when
  every feasible explanation keeps at least two households with one
  definite in-window, ticket-verified case each.

Each alert version records the feasible bounds of persons and
independent households plus the excluded duplicate sources; older
versions stay traceable.  Neither contract declares the dish harmful
and neither rewrites the meal_batch serving or recall state.
"""
import itertools
import json

from .store import ingest, decide

SENTINEL_MIN_PERSONS = 3
FORMAL_MIN_HOUSEHOLDS = 2


def run(db, data):
    state = ingest(db, 'complaint', data)
    if state != 'new':
        return {'source_id': data['source_id'], 'status': state}
    for item in data.get('reports', []):
        _store_report(db, item, data['source_id'])
    for cred in data.get('identity_credentials', []):
        _store_credential(db, cred)
    for check in data.get('time_verifications', []):
        _store_time_check(db, check)
    meals = [row['meal_id'] for row in
             db.execute('SELECT DISTINCT meal_id FROM complaint_report')]
    summaries = {meal_id: _recompute(db, meal_id, data['source_id'])
                 for meal_id in sorted(meals)}
    if not summaries:
        return {'source_id': data['source_id'], 'status': 'recorded', 'affected': []}
    primary = str(data['meal_id']) if data.get('meal_id') in summaries else sorted(summaries)[0]
    result = dict(summaries[primary])
    result['affected'] = sorted(summaries)
    return result


def _store_report(db, item, source_id):
    meal_id = str(item['meal_id'])
    if not db.execute('SELECT 1 FROM meal_batch WHERE id=?', (meal_id,)).fetchone():
        raise ValueError('meal batch required')
    start, end = int(item['symptoms_start']), int(item['symptoms_end'])
    reported = int(item['reported_at'])
    if end < start:
        raise ValueError('invalid symptom interval')
    if reported < start:
        raise ValueError('invalid symptom chronology')
    # report_id is the report identity: a repeated report keeps the
    # original claim instead of being rewritten or double counted.
    db.execute('INSERT OR IGNORE INTO complaint_report(report_id,meal_id,reporter_alias,'
               'household,symptoms_start,symptoms_end,reported_at,verification,source_id) '
               'VALUES(?,?,?,?,?,?,?,?,?)',
               (str(item['report_id']), meal_id, str(item['reporter_alias']),
                str(item.get('household', '')), start, end, reported,
                str(item.get('verification', '')), source_id))


def _store_credential(db, cred):
    kind = cred['kind']
    source = str(cred.get('source', ''))
    if kind in ('same_person', 'distinct_person'):
        alias_a, alias_b = sorted(str(a) for a in cred['aliases'])
        row = (kind, alias_a, alias_b, '', '', '', source)
    elif kind == 'household':
        row = (kind, str(cred['alias']), '', str(cred['household']), '', '', source)
    elif kind == 'ticket_check':
        row = (kind, '', '', '', str(cred['report_id']), str(cred['meal_id']), source)
    else:
        raise ValueError('unknown credential kind')
    db.execute('INSERT OR IGNORE INTO identity_credential(kind,alias_a,alias_b,household,'
               'report_id,meal_id,source_id) VALUES(?,?,?,?,?,?,?)', row)


def _store_time_check(db, check):
    start, end = int(check['symptoms_start']), int(check['symptoms_end'])
    if end < start:
        raise ValueError('invalid symptom interval')
    db.execute('INSERT OR IGNORE INTO time_verification(report_id,symptoms_start,'
               'symptoms_end,source_id) VALUES(?,?,?,?)',
               (str(check['report_id']), start, end, str(check.get('source', ''))))


def _relation(db, report, windows):
    checks = db.execute('SELECT DISTINCT symptoms_start,symptoms_end FROM '
                        'time_verification WHERE report_id=?',
                        (report['report_id'],)).fetchall()
    if len(checks) > 1:
        return 'possible'  # conflicting late checks keep the onset undecided
    if checks:
        start, end = checks[0]['symptoms_start'], checks[0]['symptoms_end']
    else:
        start, end = report['symptoms_start'], report['symptoms_end']
    if any(s <= start and end <= e for s, e in windows):
        return 'definite'
    if any(start <= e and end >= s for s, e in windows):
        return 'possible'
    return 'unrelated'


def _components(aliases, edges):
    parent = {a: a for a in aliases}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    groups = {}
    for a in aliases:
        groups.setdefault(find(a), set()).add(a)
    return frozenset(frozenset(g) for g in groups.values())


def _path_edges(edges, a, b):
    adj = {}
    for x, y in edges:
        adj.setdefault(x, []).append(y)
        adj.setdefault(y, []).append(x)
    seen, queue = {a}, [(a, [])]
    for node, path in queue:
        if node == b:
            return path
        for nxt in adj.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, path + [tuple(sorted((node, nxt)))]))
    return []


def _partitions(aliases, same, distinct):
    """All feasible person partitions kept from contradictory credentials."""
    same, distinct = set(same), set(distinct)
    comps = _components(aliases, same)
    member = {a: c for c in comps for a in c}
    for edge in sorted(distinct):
        a, b = edge
        if member.get(a) == member.get(b) and a in member:
            results = list(_partitions(aliases, same, distinct - {edge}))
            for cut in _path_edges(same, a, b):
                if cut in same:
                    results.extend(_partitions(aliases, same - {cut}, distinct))
            seen, out = set(), []
            for part in results:
                if part not in seen:
                    seen.add(part)
                    out.append(part)
            return out
    return [_components(aliases, same)]


def _recompute(db, meal_id, source_id):
    reports = [dict(r) for r in db.execute(
        'SELECT * FROM complaint_report WHERE meal_id=? ORDER BY report_id', (meal_id,))]
    windows = [(r['starts'], r['ends']) for r in db.execute(
        'SELECT starts,ends FROM observation_window WHERE meal_id=?', (meal_id,))]
    aliases = sorted({r['reporter_alias'] for r in reports})
    report_ids = sorted(r['report_id'] for r in reports)
    same, distinct, roster, tickets = set(), set(), {}, set()
    for cred in db.execute('SELECT * FROM identity_credential'):
        if cred['kind'] == 'same_person' and {cred['alias_a'], cred['alias_b']} <= set(aliases):
            same.add((cred['alias_a'], cred['alias_b']))
        elif cred['kind'] == 'distinct_person' and {cred['alias_a'], cred['alias_b']} <= set(aliases):
            distinct.add((cred['alias_a'], cred['alias_b']))
        elif cred['kind'] == 'household' and cred['alias_a'] in aliases:
            roster.setdefault(cred['alias_a'], set()).add(cred['household'])
        elif cred['kind'] == 'ticket_check' and cred['report_id'] in report_ids \
                and cred['meal_id'] == meal_id:
            tickets.add(cred['report_id'])
    relation = {r['report_id']: _relation(db, r, windows) for r in reports}
    partitions = _partitions(aliases, same, distinct)
    worlds = _worlds(partitions, reports, roster)
    counts = [_count_world(world, relation, tickets) for world in worlds]
    persons_min = min(c['persons'] for c in counts)
    persons_max = max(c['persons'] for c in counts)
    households_min = min(c['households'] for c in counts)
    households_max = max(c['households'] for c in counts)
    formal_min = min(c['formal'] for c in counts)
    excluded = _excluded_duplicates(partitions, aliases, reports)
    identity_undecided = len(worlds) > 1
    time_undecided = 'possible' in relation.values()
    sentinel = 'temporary_check' if persons_max >= SENTINEL_MIN_PERSONS else 'hold'
    formal = 'signed' if formal_min >= FORMAL_MIN_HOUSEHOLDS else 'wait'
    case = 'formal_signed' if formal == 'signed' else \
        'sentinel_notice' if sentinel == 'temporary_check' else 'watch'
    explanations = [_explain(world) for world in worlds]
    explanations_json = json.dumps(explanations, sort_keys=True, ensure_ascii=False)
    excluded_json = json.dumps(excluded, sort_keys=True, ensure_ascii=False)
    report_count = len(reports)
    reason = (f'{report_count} reports; persons {persons_min}-{persons_max}; '
              f'households {households_min}-{households_max}; '
              f'excluded duplicates {len(excluded)}')
    key = (report_count, persons_min, persons_max, households_min, households_max,
           formal_min, sentinel, formal, case, excluded_json, explanations_json)
    latest = db.execute('SELECT * FROM complaint_alert WHERE meal_id=? '
                        'ORDER BY version DESC LIMIT 1', (meal_id,)).fetchone()
    old_key = None if not latest else (
        latest['report_count'], latest['persons_min'], latest['persons_max'],
        latest['households_min'], latest['households_max'], latest['formal_households_min'],
        latest['sentinel_status'], latest['formal_status'], latest['case_status'],
        latest['excluded'], latest['explanations'])
    if key == old_key:
        version = latest['version']
    else:
        version = latest['version'] + 1 if latest else 1
        db.execute('INSERT INTO complaint_alert(meal_id,version,report_count,persons_min,'
                   'persons_max,households_min,households_max,formal_households_min,'
                   'sentinel_status,formal_status,case_status,excluded,explanations,reason) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (meal_id, version, *key, reason))
        for rank, explanation in enumerate(explanations):
            db.execute('INSERT INTO complaint_explanation(meal_id,version,rank,persons) '
                       'VALUES(?,?,?,?)',
                       (meal_id, version, rank,
                        json.dumps(explanation, sort_keys=True, ensure_ascii=False)))
    db.execute('INSERT INTO complaint_case VALUES (?,?,?,?) ON CONFLICT(meal_id) '
               'DO UPDATE SET report_count=excluded.report_count,status=excluded.status,'
               'source_id=excluded.source_id',
               (meal_id, report_count, case, source_id))
    s_reason = f'{persons_max} distinct persons possibly in window'
    if sentinel == 'temporary_check':
        s_reason += '; temporary check only, not a declared cluster'
        if identity_undecided:
            s_reason += '; identity or household undecided'
        if time_undecided:
            s_reason += '; onset time undecided'
    decide(db, 'complaint-sentinel', meal_id, sentinel, s_reason, report_ids)
    f_reason = (f'{formal_min} households with definite ticket-verified cases in every '
                f'explanation; investigation order only, no causation determined'
                if formal == 'signed' else
                f'definite ticket-verified households per explanation min {formal_min}, '
                f'need {FORMAL_MIN_HOUSEHOLDS}')
    decide(db, 'complaint-formal', meal_id, formal, f_reason, report_ids)
    decide(db, 'complaint', meal_id, case, reason, report_ids)
    return {'meal': meal_id, 'reports': report_count, 'status': case, 'version': version,
            'persons': {'min': persons_min, 'max': persons_max},
            'households': {'min': households_min, 'max': households_max},
            'sentinel': {'status': sentinel, 'identity_undecided': identity_undecided,
                         'time_undecided': time_undecided},
            'formal': {'status': formal, 'households_min': formal_min},
            'excluded_duplicates': excluded}


def _worlds(partitions, reports, roster):
    """Every feasible (person partition x household assignment) explanation."""
    worlds = []
    for partition in partitions:
        people = []
        for comp in sorted(partition, key=lambda c: sorted(c)):
            reps = [r for r in reports if r['reporter_alias'] in comp]
            candidates = {r['household'] for r in reps if r['household']}
            for alias in comp:
                candidates |= roster.get(alias, set())
            people.append((sorted(comp), sorted({r['report_id'] for r in reps}),
                           sorted(candidates) or ['']))
        for combo in itertools.product(*(c for _, _, c in people)):
            world = tuple((tuple(aliases), tuple(rep_ids), household)
                          for (aliases, rep_ids, _), household in zip(people, combo))
            worlds.append(world)
    seen, out = set(), []
    for world in worlds:
        if world not in seen:
            seen.add(world)
            out.append(world)
    return out


def _count_world(world, relation, tickets):
    persons = 0
    known_households, formal_households = set(), set()
    for aliases, rep_ids, household in world:
        relations = [relation[r] for r in rep_ids]
        if any(rel != 'unrelated' for rel in relations):
            persons += 1
            known_households.add(household)
        if any(rel == 'definite' and rep in tickets for rel, rep in
               zip(relations, rep_ids)) and household:
            formal_households.add(household)
    return {'persons': persons, 'households': len(known_households),
            'formal': len(formal_households)}


def _excluded_duplicates(partitions, aliases, reports):
    """Report groups merged into one person in every feasible explanation."""
    excluded = []
    seen = set()
    for alias in aliases:
        if alias in seen:
            continue
        group = {b for b in aliases
                 if all(any(alias in c and b in c for c in p) for p in partitions)}
        seen |= group
        reps = [r for r in reports if r['reporter_alias'] in group]
        if len(reps) >= 2:
            excluded.append({'aliases': sorted(group),
                             'reports': sorted(r['report_id'] for r in reps),
                             'sources': sorted({r['source_id'] for r in reps})})
    return excluded


def _explain(world):
    return [{'aliases': sorted(aliases), 'reports': sorted(rep_ids), 'household': household}
            for aliases, rep_ids, household in world]
