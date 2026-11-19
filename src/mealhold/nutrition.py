"""Record a basic recipe protein estimate against a signed meal batch."""

from .store import ingest, decide


def run(db, data):
    state = ingest(db, 'nutrition', data)
    if state != 'new':
        return {'source_id': data['source_id'], 'status': state}
    meal_id = str(data['meal_id'])
    meal = db.execute('SELECT persons FROM meal_batch WHERE id=?', (meal_id,)).fetchone()
    if not meal:
        raise ValueError('meal batch required')
    items = data.get('items', [])
    if not items:
        raise ValueError('nutrition items required')
    total = 0.0
    for item in items:
        lot_id = str(item['lot_id'])
        if not db.execute('SELECT 1 FROM ingredient_lot WHERE meal_id=? AND id=?',
                          (meal_id, lot_id)).fetchone():
            raise ValueError('ingredient lot not in meal')
        amount = float(item['amount_g'])
        protein = float(item['protein_per_100g'])
        if amount < 0 or protein < 0:
            raise ValueError('negative nutrient input')
        total += amount * protein / 100.0
        db.execute('INSERT INTO nutrition_item VALUES (?,?,?,?,?)',
                   (meal_id, lot_id, amount, protein, data['source_id']))
    per_person = total / meal['persons']
    target = float(data['target_g'])
    status = 'adequate' if per_person >= target else 'below_target'
    db.execute('INSERT INTO nutrition_case VALUES (?,?,?,?,?,?) ON CONFLICT(meal_id) '
               'DO UPDATE SET protein_total_g=excluded.protein_total_g,'
               'protein_per_person_g=excluded.protein_per_person_g,target_g=excluded.target_g,'
               'status=excluded.status,source_id=excluded.source_id',
               (meal_id, total, per_person, target, status, data['source_id']))
    decide(db, 'nutrition', meal_id, status, f'{per_person:.3f}g/person vs {target:.3f}g',
           [data['source_id']])
    return {'meal': meal_id, 'status': status, 'protein_per_person_g': per_person}
