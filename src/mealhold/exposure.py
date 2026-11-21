"""Meal exposure attribution and the two notification contracts.

A ticket is linked to a served portion only when pickup time window, serving
counter and portion-number evidence (sourced scan or manual delivery claims)
all agree.  Attributions are one-to-one: a portion never feeds two tickets
and a ticket never receives two ordinary portions.  The engine enumerates
every complete (maximum-cardinality) attribution compatible with the
evidence and classifies each anonymous ticket as necessarily, possibly or
impossibly exposed to a recalled batch; unidentifiable tickets stay unknown
instead of being guessed by import order, and tickets with contradictory
claims stay pending as identity conflicts (the earliest scan is never
picked arbitrarily).

The confirmed contract notifies only queues exposed in every feasible
attribution and lists the rest as pending; the precautionary contract
reminds necessary and possible queues with an explicit uncertainty note.
Notifications carry same-day one-time queue codes only.  Old notification
versions, menu versions and scan sources are never rewritten: new evidence
(late recall, recall lift, disambiguating scan) produces a new version that
upgrades or revokes, and replaying the same source emits nothing.
"""
import hashlib,json
from collections import defaultdict
from .store import ingest,decide

MAX_ATTRIBUTIONS=50000
SEVERITY={'impossible':0,'unknown':1,'possible':2,'conflict':3,'necessary':4}
CONTRACTS=('confirmed','precautionary')

def _overlap(a_from,a_to,b_from,b_to):
    return a_from<b_to and b_from<a_to

def run(db,data):
    state=ingest(db,'exposure',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    for portion in data.get('portions',[]):
        if int(portion['served_to'])<=int(portion['served_from']):
            raise ValueError('portion serving window must be positive')
        db.execute('INSERT INTO serving_portion(id,meal_id,window,served_from,served_to,kind,source_id) VALUES(?,?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET meal_id=excluded.meal_id,window=excluded.window,served_from=excluded.served_from,served_to=excluded.served_to,kind=excluded.kind,source_id=excluded.source_id',
                   (portion['id'],portion['meal_id'],portion['window'],int(portion['served_from']),int(portion['served_to']),portion.get('kind','normal'),data['source_id']))
    for ticket in data.get('tickets',[]):
        if int(ticket['pickup_to'])<=int(ticket['pickup_from']):
            raise ValueError('ticket pickup window must be positive')
        db.execute('INSERT INTO meal_ticket(id,queue,window,pickup_from,pickup_to,source_id) VALUES(?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET queue=excluded.queue,window=excluded.window,pickup_from=excluded.pickup_from,pickup_to=excluded.pickup_to,source_id=excluded.source_id',
                   (ticket['id'],ticket['queue'],ticket['window'],int(ticket['pickup_from']),int(ticket['pickup_to']),data['source_id']))
    for claim in data.get('claims',[]):
        db.execute('INSERT OR IGNORE INTO delivery_claim(portion_id,ticket_id,at,kind,source_id) VALUES(?,?,?,?,?)',
                   (claim['portion_id'],claim['ticket_id'],int(claim['at']),claim.get('kind','scan'),claim.get('source_id',data['source_id'])))
    outcome=reconcile(db)
    return {'source_id':data['source_id'],'status':'new',**(outcome or {})}

def _max_cardinality(tickets,adj):
    match={}
    def augment(ticket,seen):
        for portion in adj[ticket]:
            if portion in seen:continue
            seen.add(portion)
            if portion not in match or augment(match[portion],seen):
                match[portion]=ticket
                return True
        return False
    total=0
    for ticket in tickets:
        if augment(ticket,set()):total+=1
    return total

def _enumerate_matchings(tickets,portions,adj,target):
    candidates={p:[t for t in tickets if p in adj[t]] for p in portions}
    order=sorted(portions,key=lambda p:len(candidates[p]))
    found=[];used=set();assigned={}
    def backtrack(i):
        if len(found)>=MAX_ATTRIBUTIONS:return
        if len(assigned)+len(order)-i<target:return
        if i==len(order):
            if len(assigned)==target:found.append(dict(assigned))
            return
        portion=order[i]
        backtrack(i+1)
        for ticket in candidates[portion]:
            if ticket in used:continue
            used.add(ticket);assigned[portion]=ticket
            backtrack(i+1)
            del assigned[portion];used.discard(ticket)
    backtrack(0)
    return found,len(found)>=MAX_ATTRIBUTIONS

def reconcile(db):
    tickets=[dict(r) for r in db.execute('SELECT * FROM meal_ticket ORDER BY id')]
    portions=[dict(r) for r in db.execute('SELECT * FROM serving_portion ORDER BY id')]
    if not tickets or not portions:return None
    claims=[dict(r) for r in db.execute('SELECT * FROM delivery_claim ORDER BY id')]
    recalled_meals={r['id'] for r in db.execute("SELECT id FROM meal_batch WHERE status='recalled'")}
    portion_by_id={p['id']:p for p in portions}
    compat={(t['id'],p['id']) for t in tickets for p in portions
            if t['window']==p['window'] and _overlap(t['pickup_from'],t['pickup_to'],p['served_from'],p['served_to'])}
    ticket_ids={t['id'] for t in tickets}
    valid=[c for c in claims if c['ticket_id'] in ticket_ids and c['portion_id'] in portion_by_id]
    by_portion=defaultdict(set);by_ticket=defaultdict(set)
    for c in valid:
        by_portion[c['portion_id']].add(c['ticket_id']);by_ticket[c['ticket_id']].add(c['portion_id'])
    conflicted=[c for c in valid if len(by_portion[c['portion_id']])>1 or len(by_ticket[c['ticket_id']])>1
                or (c['ticket_id'],c['portion_id']) not in compat]
    conflict_ids={c['id'] for c in conflicted}
    conflict_tickets={c['ticket_id'] for c in conflicted}
    conflict_reason=defaultdict(list)
    for c in conflicted:
        conflict_reason[c['ticket_id']].append(f"claim {c['kind']} {c['ticket_id']}~{c['portion_id']} source {c['source_id']}")
    forced={c['ticket_id']:c['portion_id'] for c in valid if c['id'] not in conflict_ids}
    free_tickets=[t['id'] for t in tickets if t['id'] not in forced]
    free_portions=[p['id'] for p in portions if p['id'] not in set(forced.values())]
    adj={t:[p for p in free_portions if (t,p) in compat] for t in free_tickets}
    target=_max_cardinality(free_tickets,adj)
    raw,truncated=_enumerate_matchings(free_tickets,free_portions,adj,target)
    matchings=[]
    for m in raw:
        full=dict(forced);full.update({t:p for p,t in m.items()});matchings.append(full)
    recalled_portions={p['id'] for p in portions if p['meal_id'] in recalled_meals}
    classes={}
    for t in tickets:
        tid=t['id']
        if tid in conflict_tickets:
            classes[tid]=('conflict','identity conflict: '+'; '.join(sorted(conflict_reason[tid])))
        elif not any((tid,p['id']) in compat for p in portions):
            classes[tid]=('unknown','no compatible portion evidence')
        else:
            hits=[m.get(tid) in recalled_portions for m in matchings]
            if all(hits):cat,reason='necessary','recalled portion in every feasible attribution'
            elif any(hits):cat,reason='possible','recalled portion in some feasible attributions'
            else:cat,reason='impossible','no feasible attribution links a recalled portion'
            if truncated and cat=='necessary':
                cat,reason='possible','attribution space truncated; cannot prove every feasible attribution'
            classes[tid]=(cat,reason)
    signature=hashlib.sha256(json.dumps({'matchings':sorted(tuple(sorted(m.items())) for m in matchings),
                                         'classes':sorted((t,c,r) for t,(c,r) in classes.items())},
                                        sort_keys=True).encode()).hexdigest()
    latest=db.execute('SELECT id,signature FROM attribution_run ORDER BY id DESC LIMIT 1').fetchone()
    if latest and latest['signature']==signature:
        return {'run_id':latest['id'],'changed':False,'classes':{t:c for t,(c,r) in classes.items()},'notifications':{}}
    run_id=db.execute('INSERT INTO attribution_run(signature,attributions,truncated) VALUES(?,?,?)',
                      (signature,len(matchings),int(truncated))).lastrowid
    for idx,m in enumerate(matchings):
        for tid,pid in sorted(m.items()):
            db.execute('INSERT INTO attribution(run_id,idx,ticket_id,portion_id,meal_id,recalled) VALUES(?,?,?,?,?,?)',
                       (run_id,idx,tid,pid,portion_by_id[pid]['meal_id'],int(pid in recalled_portions)))
    for t in tickets:
        cat,reason=classes[t['id']]
        db.execute('INSERT INTO exposure_class(run_id,ticket_id,queue,category,reason) VALUES(?,?,?,?,?)',
                   (run_id,t['id'],t['queue'],cat,reason))
        decide(db,'exposure',t['id'],cat,reason,sorted(recalled_meals))
    notifications={}
    for contract in CONTRACTS:
        version=_emit(db,contract,run_id,tickets,classes)
        if version is not None:notifications[contract]=version
    return {'run_id':run_id,'changed':True,'classes':{t:c for t,(c,r) in classes.items()},'notifications':notifications}

def _entries(contract,tickets,classes):
    worst={}
    for t in tickets:
        cat,reason=classes[t['id']]
        if t['queue'] not in worst or SEVERITY[cat]>SEVERITY[worst[t['queue']][0]]:
            worst[t['queue']]=(cat,reason)
    entries=[]
    for queue,(cat,reason) in sorted(worst.items()):
        if cat in ('impossible','unknown'):continue
        if contract=='confirmed':
            if cat=='necessary':entries.append((queue,'confirmed','necessary','exposed to a recalled batch in every feasible attribution'))
            elif cat=='possible':entries.append((queue,'pending','possible','attribution ambiguous: exposed only in some feasible attributions'))
            else:entries.append((queue,'pending','conflict',reason))
        else:
            if cat=='necessary':entries.append((queue,'reminder','necessary','exposed in every feasible attribution'))
            elif cat=='possible':entries.append((queue,'reminder','possible','uncertain: exposed only in some feasible attributions'))
            else:entries.append((queue,'pending','conflict',reason))
    return entries

def _emit(db,contract,run_id,tickets,classes):
    entries=_entries(contract,tickets,classes)
    last=db.execute('SELECT MAX(version) v FROM notification WHERE contract=?',(contract,)).fetchone()['v']
    prev=[]
    if last is not None:
        prev=[(r['queue'],r['kind'],r['certainty'],r['reason']) for r in
              db.execute("SELECT * FROM notification WHERE contract=? AND version=? AND kind!='revoked'",(contract,last))]
    if sorted(entries)==sorted(prev):return last
    prev_active={q:(k,c) for q,k,c,_ in prev}
    current_kinds=defaultdict(set)
    for q,k,_,_ in entries:current_kinds[q].add(k)
    revoked=[(q,'revoked',c,'superseded by newer evidence')
             for q,(k,c) in sorted(prev_active.items()) if k in ('confirmed','reminder') and k not in current_kinds.get(q,set())]
    if not entries and not revoked:return last
    version=(last or 0)+1
    for queue,kind,certainty,reason in sorted(entries)+revoked:
        db.execute('INSERT INTO notification(contract,version,queue,kind,certainty,reason,run_id) VALUES(?,?,?,?,?,?,?)',
                   (contract,version,queue,kind,certainty,reason,run_id))
    return version
