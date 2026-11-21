"""Evidence-first SQLite store for school meal retains."""
import hashlib,json,sqlite3
from pathlib import Path

SCHEMA="""
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,system TEXT,source_id TEXT,digest TEXT,payload TEXT,UNIQUE(system,source_id,digest));
CREATE TABLE IF NOT EXISTS source_conflict(id INTEGER PRIMARY KEY,system TEXT,source_id TEXT,digest TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY,system TEXT,object_id TEXT,status TEXT,reason TEXT,evidence TEXT,previous INTEGER);
CREATE TABLE IF NOT EXISTS meal_batch(id TEXT PRIMARY KEY,served_at INTEGER,persons INTEGER,menu_version INTEGER,signed_at INTEGER,status TEXT,allergen TEXT);
CREATE TABLE IF NOT EXISTS ingredient_lot(id TEXT PRIMARY KEY,meal_id TEXT,allergen TEXT,recalled INTEGER,cleaned INTEGER,source_id TEXT);
CREATE TABLE IF NOT EXISTS sample_seal(id TEXT PRIMARY KEY,meal_id TEXT,cabinet TEXT,collected_at INTEGER,gross_g INTEGER,tare_g INTEGER,seal TEXT,status TEXT,version INTEGER);
CREATE TABLE IF NOT EXISTS custody_event(id INTEGER PRIMARY KEY,sample_id TEXT,at INTEGER,from_person TEXT,to_person TEXT,seal_ok INTEGER,signer_a TEXT,signer_b TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS temperature_window(id INTEGER PRIMARY KEY,cabinet TEXT,starts INTEGER,ends INTEGER,temp_c REAL,sensor TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS cold_case(sample_id TEXT PRIMARY KEY,status TEXT,reason TEXT,version INTEGER);
CREATE TABLE IF NOT EXISTS disposal_event(id INTEGER PRIMARY KEY,sample_id TEXT,at INTEGER,signer_a TEXT,signer_b TEXT,status TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS line_inspection(id INTEGER PRIMARY KEY,line_id TEXT,meal_id TEXT,station TEXT,observed_at INTEGER,allergen_hint TEXT,inspected_ok INTEGER,clean_stamp TEXT,inspector_a TEXT,inspector_b TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS line_case(meal_id TEXT PRIMARY KEY,line_id TEXT,status TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS line_station_case(meal_id TEXT,station TEXT,status TEXT,observed_at INTEGER,reason TEXT,source_id TEXT,PRIMARY KEY(meal_id,station));

-- Physical portion disposition ledger -------------------------------------
CREATE TABLE IF NOT EXISTS meal_portion(
    portion_id TEXT PRIMARY KEY,
    meal_id TEXT,
    produced_at INTEGER,
    station TEXT,
    mass_g REAL,
    units REAL,
    produced_by TEXT,
    origin TEXT,
    source_id TEXT,
    source_digest TEXT);
CREATE TABLE IF NOT EXISTS portion_event(
    id INTEGER PRIMARY KEY,
    portion_id TEXT,
    meal_id TEXT,
    kind TEXT,
    at INTEGER,
    station TEXT,
    container TEXT,
    mass_g REAL,
    actor TEXT,
    source_id TEXT,
    source_digest TEXT);
CREATE TABLE IF NOT EXISTS portion_edge(
    parent_id TEXT,
    child_id TEXT,
    kind TEXT,
    mass_g REAL,
    units REAL,
    event_at INTEGER,
    source_id TEXT,
    source_digest TEXT,
    PRIMARY KEY(parent_id,child_id));
CREATE TABLE IF NOT EXISTS serving_receipt(
    receipt_id TEXT PRIMARY KEY,
    portion_id TEXT,
    meal_id TEXT,
    outlet TEXT,
    at INTEGER,
    signer TEXT,
    signature TEXT,
    source_id TEXT,
    source_digest TEXT);
CREATE TABLE IF NOT EXISTS destruction(
    disposal_id TEXT PRIMARY KEY,
    meal_id TEXT,
    at INTEGER,
    signer_a TEXT,
    signer_b TEXT,
    declared_qty INTEGER,
    reason TEXT,
    source_id TEXT,
    source_digest TEXT);
CREATE TABLE IF NOT EXISTS destruction_item(
    disposal_id TEXT,
    portion_id TEXT,
    PRIMARY KEY(disposal_id,portion_id));
CREATE TABLE IF NOT EXISTS stock_count(
    stock_id TEXT PRIMARY KEY,
    meal_id TEXT,
    at INTEGER,
    counted_by TEXT,
    note TEXT,
    source_id TEXT,
    source_digest TEXT);
CREATE TABLE IF NOT EXISTS stock_item(
    stock_id TEXT,
    portion_id TEXT,
    PRIMARY KEY(stock_id,portion_id));
CREATE TABLE IF NOT EXISTS count_discrepancy(
    discrepancy_id TEXT PRIMARY KEY,
    meal_id TEXT,
    portion_id TEXT,
    at INTEGER,
    signer_a TEXT,
    signer_b TEXT,
    note TEXT,
    source_id TEXT,
    source_digest TEXT);
CREATE TABLE IF NOT EXISTS closeout(
    id INTEGER PRIMARY KEY,
    closeout_id TEXT,
    meal_id TEXT,
    contract TEXT,
    status TEXT,
    produced_units REAL,
    served_units REAL,
    destroyed_units REAL,
    stock_units REAL,
    unknown_units REAL,
    unverified_units REAL,
    verified_servable_units REAL,
    pending_units REAL,
    reason TEXT,
    evidence TEXT);
"""

def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def canonical(data):
    return json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def payload_digest(data):
    return hashlib.sha256(canonical(data).encode()).hexdigest()
def ingest(db,system,data):
    raw=canonical(data)
    digest=hashlib.sha256(raw.encode()).hexdigest();sid=data['source_id']
    old=db.execute('SELECT digest FROM source WHERE system=? AND source_id=?',(system,sid)).fetchall()
    if any(r['digest']!=digest for r in old):
        db.execute('INSERT INTO source_conflict(system,source_id,digest,payload) VALUES(?,?,?,?)',(system,sid,digest,raw))
        decide(db,system,sid,'conflict','source ID changed bytes',[digest]);return 'conflict'
    if old:return 'duplicate'
    db.execute('INSERT INTO source(system,source_id,digest,payload) VALUES(?,?,?,?)',(system,sid,digest,raw));return 'new'
def decide(db,system,key,status,reason,evidence):
    ev=json.dumps(evidence,sort_keys=True,ensure_ascii=False)
    old=db.execute('SELECT * FROM decision WHERE system=? AND object_id=? ORDER BY id DESC LIMIT 1',(system,key)).fetchone()
    if old and (old['status'],old['reason'],old['evidence'])==(status,reason,ev):return old['id']
    cur=db.execute('INSERT INTO decision(system,object_id,status,reason,evidence,previous) VALUES(?,?,?,?,?,?)',
                   (system,key,status,reason,ev,old['id'] if old else None))
    return cur.lastrowid
