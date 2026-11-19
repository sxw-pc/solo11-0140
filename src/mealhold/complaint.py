"""Count offline illness reports associated with one meal batch."""

from .store import ingest, decide


def run(db, data):
    state = ingest(db, 'complaint', data)
    if state != 'new':
        return {'source_id': data['source_id'], 'status': state}
    meal_id = str(data['meal_id'])
    meal = db.execute('SELECT served_at FROM meal_batch WHERE id=?', (meal_id,)).fetchone()
    if not meal:
        raise ValueError('meal batch required')
    for item in data.get('reports', []):
        reported = int(item['reported_at'])
        symptoms = int(item['symptoms_at'])
        if symptoms < meal['served_at'] or reported < symptoms:
            raise ValueError('invalid symptom chronology')
        db.execute('INSERT INTO complaint_report VALUES (?,?,?,?,?,?)',
                   (str(item['report_id']), meal_id, reported, symptoms,
                    str(item.get('household', '')), data['source_id']))
    count = db.execute('SELECT COUNT(*) FROM complaint_report WHERE meal_id=?',
                       (meal_id,)).fetchone()[0]
    status = 'alert' if count >= 3 else 'watch'
    db.execute('INSERT INTO complaint_case VALUES (?,?,?,?) ON CONFLICT(meal_id) '
               'DO UPDATE SET report_count=excluded.report_count,status=excluded.status,'
               'source_id=excluded.source_id',
               (meal_id, count, status, data['source_id']))
    decide(db, 'complaint', meal_id, status, f'{count} reports', [data['source_id']])
    return {'meal': meal_id, 'reports': count, 'status': status}
