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
CREATE TABLE IF NOT EXISTS nutrition_case(meal_id TEXT PRIMARY KEY,protein_total_g REAL,protein_per_person_g REAL,target_g REAL,status TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS nutrition_item(meal_id TEXT,lot_id TEXT,amount_g REAL,protein_per_100g REAL,source_id TEXT,PRIMARY KEY(meal_id,lot_id,source_id));
CREATE TABLE IF NOT EXISTS observation_window(id INTEGER PRIMARY KEY,meal_id TEXT,starts INTEGER,ends INTEGER,source_id TEXT,UNIQUE(meal_id,starts,ends));
CREATE TABLE IF NOT EXISTS complaint_report(report_id TEXT PRIMARY KEY,meal_id TEXT,reporter_alias TEXT,household TEXT,symptoms_start INTEGER,symptoms_end INTEGER,reported_at INTEGER,verification TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS identity_credential(id INTEGER PRIMARY KEY,kind TEXT,alias_a TEXT,alias_b TEXT,household TEXT,report_id TEXT,meal_id TEXT,source_id TEXT,UNIQUE(kind,alias_a,alias_b,household,report_id,meal_id,source_id));
CREATE TABLE IF NOT EXISTS time_verification(id INTEGER PRIMARY KEY,report_id TEXT,symptoms_start INTEGER,symptoms_end INTEGER,source_id TEXT,UNIQUE(report_id,symptoms_start,symptoms_end,source_id));
CREATE TABLE IF NOT EXISTS complaint_case(meal_id TEXT PRIMARY KEY,report_count INTEGER,status TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS complaint_alert(id INTEGER PRIMARY KEY,meal_id TEXT,version INTEGER,report_count INTEGER,persons_min INTEGER,persons_max INTEGER,households_min INTEGER,households_max INTEGER,formal_households_min INTEGER,sentinel_status TEXT,formal_status TEXT,case_status TEXT,excluded TEXT,explanations TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS complaint_explanation(id INTEGER PRIMARY KEY,meal_id TEXT,version INTEGER,rank INTEGER,persons TEXT);
"""

def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def ingest(db,system,data):
    raw=json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':'))
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
