"""Private Sheets projection into one deduplicated collector tab."""
import copy, json, os, re, unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

TAB='COLLECTOR_NEW'
TABS={'all':TAB}
HEADERS=['Hotel','Email','Website','Profession','Collection status','Source evidence','Collected UTC','Source','Review decision','Your notes','Stable ID']
_GENERIC={'hotel','hotels','best','western','plus','spa','resort','gasthof','restaurant','cafe','landhotel','wellnesshotel','parkhotel','kurhotel','zur','zum','der','die','das','am','an','im','in','post'}
_LEGAL=re.compile(r'\b(gmbh|co|kg|egbr|ek|mbh|ag|und|and)\b',re.I)

def _norm_name(v):
    t=unicodedata.normalize('NFKD',str(v or '').casefold())
    t=''.join(c for c in t if not unicodedata.combining(c))
    t=_LEGAL.sub(' ',t.replace('&',' '))
    return ' '.join(re.sub(r'[^a-z0-9]+',' ',t).split())

def _distinctive(v): return [t for t in v.split() if t and t not in _GENERIC]

def _same_hotel(a,b):
    a,b=_norm_name(a),_norm_name(b)
    if not a or not b:return False
    short,long=(a,b) if len(a)<=len(b) else (b,a)
    return bool(_distinctive(short)) and (a==b or (' '+short+' ') in (' '+long+' '))

def _stamp(r): return float(r.get('first_seen',r.get('last_seen',0)) or 0)
def _score(r): return 6*len(r.get('emails',[]) or [])+5*bool(r.get('website'))+2*bool(r.get('evidence'))

def merge_entries(state):
    from identity import canonicalize
    from verification import publishable
    result={}
    for cid, record in canonicalize(state).items():
        if publishable(record):
            record=copy.deepcopy(record)
            record['_aliases']=record.get('aliases',[cid])
            result[cid]=record
    return result

def projection(key,record):
    ts=datetime.fromtimestamp(record.get('first_seen',record['last_seen']),timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    status='Needs review' if record.get('status')=='NEEDS_REVIEW' else 'Possible match — unconfirmed'
    evidence=record.get('evidence') or 'Hotelfach is listed; 2027 intake and email acceptance are not verified'
    return [record['name'],'; '.join(record.get('emails',[])),record.get('website',''),record.get('profession',''),status,evidence,ts,record.get('source',''),'Not reviewed','',record.get('collection_id',key)]

def plan(tab,entries,existing):
    if not existing or existing[0]!=HEADERS:raise ValueError('Master tab headers changed; stop rather than overwrite')
    pos={}
    for rownum,row in enumerate(existing[1:],4):
        if not any(row):continue
        if len(row)<11 or not row[10] or row[10] in pos:raise ValueError('Missing or duplicate row identity; manual review needed')
        pos[row[10]]=rownum
    next_row=len(existing)+3;changes=[]
    for key,record in entries.items():
        row=projection(key,record)
        if row[10] in pos:
            n=pos[row[10]];changes.append({'range':f"'{tab}'!A{n}:H{n}",'values':[row[:8]]})
        else:
            changes.append({'range':f"'{tab}'!A{next_row}:K{next_row}",'values':[row]});next_row+=1
    return changes,next_row-1

def compose_rows(entries,existing):
    if not existing or existing[0]!=HEADERS:raise ValueError('Master tab headers changed; stop rather than overwrite')
    human={};positions={}
    for pos,row in enumerate(existing[1:]):
        if not any(row):continue
        if len(row)<11 or not row[10] or row[10] in human:raise ValueError('Invalid collector row identity')
        human[row[10]]=(row[8] if len(row)>8 and row[8] else 'Not reviewed',row[9] if len(row)>9 else '')
        positions[row[10]]=pos
    def order(item):
        key,record=item;aliases=record.get('_aliases',[record.get('collection_id',key)])
        old=[positions[a] for a in aliases if a in positions]
        return (min(old) if old else 10**9,_stamp(record),_norm_name(record.get('name')))
    rows=[]
    for key,record in sorted(entries.items(),key=order):
        row=projection(key,record);decisions=[];notes=[]
        for alias in record.get('_aliases',[row[10]]):
            if alias not in human:continue
            decision,note=human[alias]
            if decision and decision!='Not reviewed' and decision not in decisions:decisions.append(decision)
            if note and note not in notes:notes.append(note)
        if len(decisions)>1:raise ValueError('Conflicting manual review decisions across merged duplicates')
        row[8]=decisions[0] if decisions else 'Not reviewed';row[9]=' | '.join(notes);rows.append(row)
    return rows

def main():
    from production import main as production_main
    return production_main(sync_only=True)

if __name__=='__main__':
    try:main()
    except Exception:
        print('MASTER_SYNC_STOPPED; credentials and spreadsheet content suppressed');raise SystemExit(2)
