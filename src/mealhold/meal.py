"""Meal version, recalled ingredients, allergen cleaning and serving scale."""
import math
from .store import ingest,decide

def required_grams(persons):
    return 125*math.ceil(persons/100)

def run(db,data):
    state=ingest(db,'meal',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    meal=data['meal'];mid=meal['id'];persons=int(meal['persons'])
    if persons<1:raise ValueError('persons must be positive')
    old=db.execute('SELECT * FROM meal_batch WHERE id=?',(mid,)).fetchone()
    version=int(meal['menu_version']);signed=int(meal['signed_at'])
    if old and signed<old['signed_at']:
        version=old['menu_version'];signed=old['signed_at']
        decide(db,'menu',mid,'stale','older menu cannot replace signed served version',[data['source_id']])
    else:decide(db,'menu',mid,'effective',f'menu version {version} effective',[data['source_id']])
    recalled=any(i.get('recalled',False) for i in data['ingredients'])
    contact=any(i.get('allergen') and not i.get('cleaned',False) for i in data['ingredients'])
    status='recalled' if recalled else 'allergen_hold' if contact else 'served'
    decide(db,'ingredient',mid,'recalled' if recalled else 'available','recalled lot blocks meal',[i['id'] for i in data['ingredients']])
    decide(db,'allergen',mid,'hold' if contact else 'clear','cleaning record needed for allergen line',[i['id'] for i in data['ingredients']])
    decide(db,'serving-scale',mid,'scaled',f'{persons} persons require {required_grams(persons)}g retained',[data['source_id']])
    db.execute('INSERT INTO meal_batch(id,served_at,persons,menu_version,signed_at,status,allergen) VALUES(?,?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET served_at=excluded.served_at,persons=excluded.persons,menu_version=excluded.menu_version,signed_at=excluded.signed_at,status=excluded.status,allergen=excluded.allergen',
               (mid,int(meal['served_at']),persons,version,signed,status,meal.get('allergen')))
    for item in data['ingredients']:
        db.execute('INSERT INTO ingredient_lot(id,meal_id,allergen,recalled,cleaned,source_id) VALUES(?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET recalled=excluded.recalled,cleaned=excluded.cleaned,source_id=excluded.source_id',
                   (item['id'],mid,item.get('allergen'),int(bool(item.get('recalled'))),int(bool(item.get('cleaned'))),data['source_id']))
    decide(db,'meal',mid,status,'batch state from ingredients and signed menu',[data['source_id']])
    from .sample import reconcile_meal
    reconcile_meal(db,mid)
    return {'meal':mid,'status':status,'menu_version':version,'required_grams':required_grams(persons)}


def registrations(db,data):
    """Register the outlets and duty managers that a physical ledger may cite.

    Registration records who is authorised to sign outlet receipts, dual-sign a
    destruction or co-sign a count discrepancy. It is evidence only: it never
    changes a menu version or a physical portion's disposition.
    """
    state=ingest(db,'registration',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    meal_id=data['meal_id']
    if not db.execute('SELECT 1 FROM meal_batch WHERE id=?',(meal_id,)).fetchone():
        raise ValueError('meal must be recorded before registering signers')
    for outlet in data.get('outlets',[]):
        decide(db,'outlet',outlet['id'],'registered',
               'outlet may sign its own serving receipts',
               [meal_id,outlet.get('name'),data['source_id']])
    for person in data.get('duty_managers',[]):
        decide(db,'duty-manager',person,'registered',
               'duty manager may dual-sign destruction or count discrepancies',
               [meal_id,data['source_id']])
    return {'meal':meal_id,'outlets':len(data.get('outlets',[])),
            'duty_managers':len(data.get('duty_managers',[]))}


def servable_check(db,meal_id):
    """Report physical serving totals alongside the independent holding rules.

    A station hold does not delete a portion's physical disposition, but a
    portion that passed the held station cannot be reported as verified
    servable. The signed menu and retain evidence are reported untouched.
    """
    from .disposition import servable_units
    menu=db.execute('SELECT * FROM meal_batch WHERE id=?',(meal_id,)).fetchone()
    if not menu:return {'meal':meal_id,'status':'missing'}
    line=db.execute('SELECT status,reason FROM line_case WHERE meal_id=?',(meal_id,)).fetchone()
    physical=servable_units(db,meal_id)
    return {'meal':meal_id,
            'menu_status':menu['status'],
            'menu_version':menu['menu_version'],
            'signed_persons':menu['persons'],
            'line_status':line['status'] if line else None,
            'line_reason':line['reason'] if line else None,
            'physical':physical,
            'verified_servable':physical['verified_servable_units'],
            'unverified':physical['unverified_units']}
