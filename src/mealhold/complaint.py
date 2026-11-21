"""Two-contract investigation alerts for post-meal illness reports.

The baseline counted raw reports per meal and raised a single alert.  This
version keeps every original claim, merges accounts only through trusted
same-person credentials (a shared household never merges people), keeps every
feasible identity interpretation when credentials contradict each other, and
classifies symptom intervals against the observation window registered with
the meal: fully inside is definite, partial overlap is possible, and a late
time verification can reclassify either way.

Two independent response contracts are evaluated per meal:

* the fast sentinel contract issues a temporary verification notice once at
  least three different people may fall inside the window in every feasible
  interpretation; the notice flags undetermined identity or time and never
  declares an independent cluster;
* the formal cluster contract signs an investigation order only when every
  feasible interpretation still has at least two different households, each
  with one definitely in-window person whose meal claim passed an independent
  ticket check.

Every reassessment is stored as a new alert version with people and household
bounds plus the excluded duplicate sources; old versions stay traceable.  The
alert never declares the meal harmful and never rewrites meal_batch.
"""

import json
from itertools import combinations

from .store import decide, ingest

_RELATION_RANK = {'unrelated': 0, 'possible': 1, 'definite': 2}
_MAX_CREDENTIALS = 16


def _relation(frm, to, window):
    """Classify a symptom interval against the registered observation window."""
    if window is None:
        return 'possible'
    if to < window['starts'] or frm > window['ends']:
        return 'unrelated'
    if window['starts'] <= frm and to <= window['ends']:
        return 'definite'
    return 'possible'


def _interval(item):
    frm = item.get('symptoms_from')
    to = item.get('symptoms_to')
    point = item.get('symptoms_at')
    if frm is None:
        frm = point
    if to is None:
        to = point
    if frm is None or to is None:
        raise ValueError('symptom interval required')
    return int(frm), int(to)


def _ingest_reports(db, data):
    meals = set()
    for item in data.get('reports', []):
        meal_id = str(item.get('meal_id') or data.get('meal_id') or '')
        if not meal_id:
            raise ValueError('meal claim required')
        meal = db.execute('SELECT served_at FROM meal_batch WHERE id=?', (meal_id,)).fetchone()
        if not meal:
            raise ValueError('meal batch required')
        frm, to = _interval(item)
        reported = int(item['reported_at'])
        if frm > to or to < meal['served_at'] or reported < frm:
            raise ValueError('invalid symptom chronology')
        row = (str(item['report_id']), meal_id,
               str(item.get('alias') or item['report_id']),
               str(item.get('household') or ''), frm, to, reported,
               str(item.get('verify_source') or ''), data['source_id'])
        old = db.execute('SELECT * FROM complaint_report WHERE report_id=?', (row[0],)).fetchone()
        if old:
            if tuple(old) != row:
                raise ValueError('report id already filed with different content')
        else:
            db.execute('INSERT INTO complaint_report VALUES (?,?,?,?,?,?,?,?,?)', row)
        meals.add(meal_id)
    return meals


def _ingest_checks(db, data):
    meals = set()
    for chk in data.get('time_checks', []):
        rid = str(chk['report_id'])
        rep = db.execute('SELECT meal_id FROM complaint_report WHERE report_id=?', (rid,)).fetchone()
        if not rep:
            raise ValueError('time check for unknown report')
        frm, to = int(chk['symptoms_from']), int(chk['symptoms_to'])
        if frm > to:
            raise ValueError('invalid verified interval')
        db.execute('INSERT INTO complaint_time_check VALUES (?,?,?,?,?) '
                   'ON CONFLICT(report_id) DO UPDATE SET symptoms_from=excluded.symptoms_from,'
                   'symptoms_to=excluded.symptoms_to,verify_source=excluded.verify_source,'
                   'source_id=excluded.source_id',
                   (rid, frm, to, str(chk.get('verify_source') or ''), data['source_id']))
        meals.add(rep['meal_id'])
    for chk in data.get('ticket_checks', []):
        rid, meal_id = str(chk['report_id']), str(chk['meal_id'])
        if not db.execute('SELECT 1 FROM complaint_report WHERE report_id=?', (rid,)).fetchone():
            raise ValueError('ticket check for unknown report')
        db.execute('INSERT INTO complaint_ticket_check VALUES (?,?,?,?,?) '
                   'ON CONFLICT(report_id,meal_id) DO UPDATE SET verified=excluded.verified,'
                   'verify_source=excluded.verify_source,source_id=excluded.source_id',
                   (rid, meal_id, int(bool(chk.get('verified'))),
                    str(chk.get('verify_source') or ''), data['source_id']))
        meals.add(meal_id)
    return meals


def _ingest_credentials(db, data):
    aliases = set()
    for cred in data.get('credentials', []):
        cid = str(cred['credential_id'])
        if db.execute('SELECT 1 FROM identity_credential WHERE credential_id=?', (cid,)).fetchone():
            raise ValueError('credential id already filed')
        db.execute('INSERT INTO identity_credential VALUES (?,?,?)',
                   (cid, str(cred.get('issuer') or ''), data['source_id']))
        for link in cred.get('links', []):
            alias = str(link['alias'])
            db.execute('INSERT INTO identity_link VALUES (?,?,?,?)',
                       (cid, alias, str(link['person']), str(link.get('household') or '')))
            aliases.add(alias)
    meals = set()
    for alias in aliases:
        for row in db.execute('SELECT DISTINCT meal_id FROM complaint_report WHERE alias=?', (alias,)):
            meals.add(row['meal_id'])
    return meals


def _meals_of(db, data):
    meals = set()
    for item in data.get('reports', []):
        meal_id = item.get('meal_id') or data.get('meal_id')
        if meal_id:
            meals.add(str(meal_id))
    for chk in data.get('ticket_checks', []):
        meals.add(str(chk['meal_id']))
    for chk in data.get('time_checks', []):
        row = db.execute('SELECT meal_id FROM complaint_report WHERE report_id=?',
                         (str(chk['report_id']),)).fetchone()
        if row:
            meals.add(row['meal_id'])
    for cred in data.get('credentials', []):
        for link in cred.get('links', []):
            for row in db.execute('SELECT DISTINCT meal_id FROM complaint_report WHERE alias=?',
                                  (str(link['alias']),)):
                meals.add(row['meal_id'])
    return meals


def _load_credentials(db):
    creds = []
    for row in db.execute('SELECT * FROM identity_credential ORDER BY credential_id'):
        links = [(link['alias'], link['person'], link['household'])
                 for link in db.execute('SELECT * FROM identity_link WHERE credential_id=? '
                                        'ORDER BY alias', (row['credential_id'],))]
        creds.append({'credential_id': row['credential_id'], 'links': links})
    return creds


def _consistent(creds):
    alias_person, alias_household, person_household = {}, {}, {}
    for cred in creds:
        for alias, person, household in cred['links']:
            if alias_person.get(alias, person) != person:
                return False
            if alias_household.get(alias, household) != household:
                return False
            if person_household.get(person, household) != household:
                return False
            alias_person[alias] = person
            alias_household[alias] = household
            person_household[person] = household
    return True


def _worlds(creds):
    """Every maximal consistent credential subset.

    Contradictory credentials are never resolved by arrival order or majority
    vote: each maximal consistent subset stays alive as one feasible identity
    interpretation until evidence rules it out.
    """
    if len(creds) > _MAX_CREDENTIALS:
        raise ValueError('too many identity credentials to enumerate')
    maximal = []
    for size in range(len(creds), -1, -1):
        for combo in combinations(range(len(creds)), size):
            if any(set(combo) <= kept for kept in maximal):
                continue
            if _consistent([creds[i] for i in combo]):
                maximal.append(set(combo))
    return [[creds[i] for i in sorted(kept)] for kept in maximal]


def evaluate(db, meal_id, source_id):
    reports = db.execute('SELECT * FROM complaint_report WHERE meal_id=? ORDER BY report_id',
                         (meal_id,)).fetchall()
    window = db.execute('SELECT starts,ends FROM observation_window WHERE meal_id=?',
                        (meal_id,)).fetchone()
    checks = {row['report_id']: row for row in db.execute('SELECT * FROM complaint_time_check')}
    tickets = {(row['report_id'], row['meal_id']): row['verified']
               for row in db.execute('SELECT * FROM complaint_ticket_check')}
    creds = _load_credentials(db)
    worlds = _worlds(creds)

    relation = {}
    for rep in reports:
        chk = checks.get(rep['report_id'])
        frm, to = ((chk['symptoms_from'], chk['symptoms_to']) if chk
                   else (rep['symptoms_from'], rep['symptoms_to']))
        relation[rep['report_id']] = _relation(frm, to, window)

    aliases = sorted({rep['alias'] for rep in reports})
    views = []
    for world in worlds:
        mapping = {}
        for cred in world:
            for alias, person, household in cred['links']:
                mapping[alias] = (person, household)
        persons = {}
        for rep in reports:
            person, household = mapping.get(rep['alias'], (rep['alias'], ''))
            household = household or rep['household']
            entry = persons.setdefault(person, {'households': set(), 'aliases': set(), 'reports': []})
            entry['households'].add(household)
            entry['aliases'].add(rep['alias'])
            entry['reports'].append(rep['report_id'])
        for entry in persons.values():
            entry['relation'] = max((relation[rid] for rid in entry['reports']),
                                    key=_RELATION_RANK.__getitem__)
        relevant = [person for person, entry in persons.items() if entry['relation'] != 'unrelated']
        qualified = set()
        for person, entry in persons.items():
            if entry['relation'] != 'definite':
                continue
            if any(relation[rid] == 'definite' and tickets.get((rid, meal_id))
                   for rid in entry['reports']):
                qualified |= entry['households']
        views.append({'credentials': sorted(cred['credential_id'] for cred in world),
                      'mapping': mapping, 'persons': persons,
                      'relevant': relevant, 'qualified': qualified})

    people = [len(view['persons']) for view in views]
    households = [len({hh for entry in view['persons'].values() for hh in entry['households']})
                  for view in views]
    relevant_counts = [len(view['relevant']) for view in views]
    qualified_counts = [len(view['qualified']) for view in views]
    sentinel = all(count >= 3 for count in relevant_counts)
    cluster = all(count >= 2 for count in qualified_counts)

    covered = {alias for cred in creds for alias, _, _ in cred['links']}
    caveats = []
    if len(views) > 1 or any(alias not in covered for alias in aliases):
        caveats.append('identity_unresolved')
    if any(entry['relation'] == 'possible' for view in views for entry in view['persons'].values()):
        caveats.append('time_unverified')
    if window is None:
        caveats.append('window_unregistered')

    signatures = {alias: tuple(view['mapping'].get(alias, (alias, ''))[0] for view in views)
                  for alias in aliases}
    groups = {}
    for alias in aliases:
        groups.setdefault(signatures[alias], []).append(alias)
    merged = []
    for group in groups.values():
        if len(group) > 1:
            merged.append({
                'aliases': sorted(group),
                'reports': sorted(rep['report_id'] for rep in reports if rep['alias'] in group),
                'persons': sorted({view['mapping'][alias][0] for alias in group
                                   for view in views if alias in view['mapping']}),
            })
    replayed = sorted({row['source_id'] for row in db.execute(
        "SELECT DISTINCT source_id FROM complaint_exclusion WHERE meal_id=? AND kind='replay'",
        (meal_id,))})

    detail = {
        'window': [window['starts'], window['ends']] if window else None,
        'relations': {rid: relation[rid] for rid in sorted(relation)},
        'worlds': [{
            'credentials': view['credentials'],
            'relevant_persons': len(view['relevant']),
            'qualified_households': sorted(view['qualified']),
            'persons': {person: {'households': sorted(entry['households']),
                                 'aliases': sorted(entry['aliases']),
                                 'reports': sorted(entry['reports']),
                                 'relation': entry['relation']}
                        for person, entry in sorted(view['persons'].items())},
        } for view in views],
    }

    sentinel_status = 'notice' if sentinel else 'hold'
    cluster_status = 'signed' if cluster else 'hold'
    notice = None
    if sentinel:
        notice = ('临时核查通知：该餐次有至少三名不同报告人可能落入登记观察窗，需临时现场核查；'
                  '身份或时刻尚未完全核定时不得升级，本通知不构成独立聚集认定。')
    order = None
    if cluster:
        order = ('正式调查单：所有可行身份与时刻解释中均存在至少两个独立家庭，'
                 '各有一名确定落入观察窗且餐次票据经独立核对的报告人。')

    summary = {
        'meal': meal_id,
        'reports': len(reports),
        'status': 'cluster' if cluster else 'sentinel' if sentinel else 'watch',
        'people': [min(people), max(people)],
        'households': [min(households), max(households)],
        'sentinel': {'status': sentinel_status, 'declaration': 'temporary_verification_only',
                     'caveats': caveats, 'notice': notice},
        'cluster': {'status': cluster_status, 'order': order},
        'excluded': {'merged_accounts': merged, 'replayed_sources': replayed},
        'detail': detail,
    }

    excluded_json = json.dumps(summary['excluded'], ensure_ascii=False, sort_keys=True)
    detail_json = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    signature = (summary['people'][0], summary['people'][1],
                 summary['households'][0], summary['households'][1],
                 sentinel_status, cluster_status, excluded_json, detail_json)
    last = db.execute('SELECT * FROM complaint_alert WHERE meal_id=? ORDER BY version DESC LIMIT 1',
                      (meal_id,)).fetchone()
    if last and (last['people_min'], last['people_max'], last['households_min'],
                 last['households_max'], last['sentinel'], last['cluster'],
                 last['excluded'], last['detail']) == signature:
        version = last['version']
    else:
        version = last['version'] + 1 if last else 1
        cur = db.execute(
            'INSERT INTO complaint_alert(meal_id,version,people_min,people_max,households_min,'
            'households_max,sentinel,cluster,excluded,detail,source_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (meal_id, version, *signature[:6], excluded_json, detail_json, source_id))
        for index, view in enumerate(views):
            for alias in aliases:
                person, household = view['mapping'].get(alias, (alias, ''))
                if not household:
                    household = next(rep['household'] for rep in reports if rep['alias'] == alias)
                db.execute('INSERT INTO alert_world VALUES (?,?,?,?,?)',
                           (cur.lastrowid, index, alias, person, household))
    summary['version'] = version

    db.execute('INSERT INTO complaint_case VALUES (?,?,?,?) ON CONFLICT(meal_id) DO UPDATE SET '
               'report_count=excluded.report_count,status=excluded.status,source_id=excluded.source_id',
               (meal_id, len(reports), summary['status'], source_id))
    evidence = [source_id]
    decide(db, 'complaint-sentinel', meal_id, sentinel_status,
           f'{min(relevant_counts)}..{max(relevant_counts)} related persons across '
           f'{len(views)} interpretation(s); caveats {caveats}', evidence)
    decide(db, 'complaint-cluster', meal_id, cluster_status,
           f'{min(qualified_counts)}..{max(qualified_counts)} ticket-verified households with '
           f'definite in-window cases across {len(views)} interpretation(s)', evidence)
    decide(db, 'complaint', meal_id, summary['status'],
           f'{len(reports)} reports; people {summary["people"][0]}..{summary["people"][1]}; '
           f'households {summary["households"][0]}..{summary["households"][1]}', evidence)
    return summary


def run(db, data):
    state = ingest(db, 'complaint', data)
    if state != 'new':
        if state == 'duplicate':
            for meal_id in _meals_of(db, data):
                db.execute('INSERT INTO complaint_exclusion(meal_id,kind,detail,source_id) '
                           'VALUES (?,?,?,?)',
                           (meal_id, 'replay', 'identical payload replayed; not recounted',
                            data['source_id']))
        return {'source_id': data['source_id'], 'status': state}
    if not any(key in data for key in ('reports', 'credentials', 'time_checks', 'ticket_checks')):
        raise ValueError('complaint payload required')
    meals = set()
    if 'reports' in data:
        meals |= _ingest_reports(db, data)
    if 'credentials' in data:
        meals |= _ingest_credentials(db, data)
    if 'time_checks' in data or 'ticket_checks' in data:
        meals |= _ingest_checks(db, data)
    summaries = {meal_id: evaluate(db, meal_id, data['source_id']) for meal_id in sorted(meals)}
    if 'reports' in data and len(summaries) == 1:
        return next(iter(summaries.values()))
    return {'source_id': data['source_id'], 'status': 'recorded', 'meals': summaries}
