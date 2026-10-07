"""Private Sheets projection into one deduplicated collector tab."""
import copy, json, os, re, unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

TAB='COLLECTOR_NEW'
TABS={'all':TAB}
HEADERS=['Hotel','Email','Website','Profession','Collection status','Source evidence','Collected UTC','Source','Review decision','Your notes','Stable ID']
_GENERIC={'hotel','hotels','best','western','plus','spa','resort','gasthof','restaurant','cafe','landhotel','wellnesshotel','parkhotel','kurhotel','zur','zum','der','die','das','am','an','im','in','post'}
_LEGAL=re.compile(r'\\b(gmbh|co|kg|egbr|ek|mbh|ag|und|and)\\b',re.I)

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
    items=[]
    for section in ('records','review_candidates'):
        for key,record in state.get(section,{}).items():items.append({'key':key,'record':record,'section':section})
    parent=list(range(len(items)))
    def find(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    def union(i,j):
        i,j=find(i),find(j)
        if i!=j:parent[j]=i
    buckets={}
    for i,item in enumerate(items):
        n=_norm_name(item['record'].get('name'))
        for token in set(_distinctive(n)):
            for j in buckets.get(token,[]):
                if _same_hotel(item['record'].get('name'),items[j]['record'].get('name')):union(i,j)
            buckets.setdefault(token,[]).append(i)
    groups={}
    for i,item in enumerate(items):groups.setdefault(find(i),[]).append(item)
    out={}
    for members in groups.values():
        ordered=sorted(members,key=lambda x:(_stamp(x['record']),str(x['key'])))
        canonical=ordered[0]
        richest=max(members,key=lambda x:(_score(x['record']),len(str(x['record'].get('name','')))))
        record=copy.deepcopy(richest['record'])
        emails=[];evidence=[];sources=[]
        for item in members:
            src=item['record']
            for email in src.get('emails',[]) or []:
                if email and email not in emails:emails.append(email)
            ev=str(src.get('evidence') or '').strip();so=str(src.get('source') or '').strip()
            if ev and ev not in evidence:evidence.append(ev)
            if so and so not in sources:sources.append(so)
        record['emails']=emails
        if not record.get('website'):record['website']=next((x['record'].get('website') for x in members if x['record'].get('website')),'')
        if not record.get('profession'):record['profession']=next((x['record'].get('profession') for x in members if x['record'].get('profession')),'')
        if evidence:record['evidence']=' | '.join(evidence)
        if sources:record['source']='; '.join(sources)
        first=[x['record'].get('first_seen') for x in members if x['record'].get('first_seen') is not None]
        last=[x['record'].get('last_seen') for x in members if x['record'].get('last_seen') is not None]
        if first:record['first_seen']=min(first)
        if last:record['last_seen']=max(last)
        record['status']='NEEDS_REVIEW' if any(x['section']=='records' or x['record'].get('status')=='NEEDS_REVIEW' for x in members) else 'POSSIBLE_MATCH'
        cid=canonical['record'].get('collection_id',canonical['key'])
        record['collection_id']=cid
        record['_aliases']=sorted({x['record'].get('collection_id',x['key']) for x in members})
        if cid in out:raise ValueError('Canonical collector identity collision')
        out[cid]=record
    return out

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
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    from github_state import GitHubState
    from collector import validate
    config=json.load(open('config.json'))
    state,_=GitHubState(config['private_repo'],config['state_branch'],os.environ['PRIVATE_COLLECTOR_TOKEN']).read(config['state_path'])
    validate(state)
    info=json.loads(os.environ['GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON'])
    if info.get('type')!='service_account' or info.get('token_uri')!='https://oauth2.googleapis.com/token':raise ValueError('Only Google service-account credentials supported')
    session=AuthorizedSession(service_account.Credentials.from_service_account_info(info,scopes=['https://www.googleapis.com/auth/spreadsheets']))
    base='https://sheets.googleapis.com/v4/spreadsheets/'+quote(os.environ['GOOGLE_SHEETS_ID'],safe='')
    def request(method,route,body=None):
        response=session.request(method,base+route,json=body,timeout=45)
        if not response.ok:raise RuntimeError('Sheets request failed; response content suppressed')
        return response.json()
    metadata=request('GET','?fields=sheets.properties');sheets={s['properties']['title']:s['properties'] for s in metadata['sheets']}
    if TAB not in sheets:raise ValueError('Single collector tab missing')
    props=sheets[TAB];end=props['gridProperties']['rowCount']
    existing=request('GET','/values/'+quote(f"'{TAB}'!A3:K{end}",safe='')).get('values',[])
    rows=compose_rows(merge_entries(state),existing);needed=len(rows)+3
    if needed>end:
        request('POST',':batchUpdate',{'requests':[{'updateSheetProperties':{'properties':{'sheetId':props['sheetId'],'gridProperties':{'rowCount':needed+100}},'fields':'gridProperties.rowCount'}}]});end=needed+100
    request('POST','/values/'+quote(f"'{TAB}'!A4:K{end}",safe='')+':clear',{})
    now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    changes=[{'range':f"'{TAB}'!A1",'values':[['Hotel collection results — deduplicated — single collector page']]},{'range':f"'{TAB}'!A2",'values':[[f'Last sync UTC: {now} — review only; no automatic sending.']]}]
    if rows:changes.append({'range':f"'{TAB}'!A4:K{len(rows)+3}",'values':rows})
    request('POST','/values:batchUpdate',{'valueInputOption':'RAW','data':changes})
    print(json.dumps({'master_sync':'success','collector_rows':len(rows)}))

if __name__=='__main__':
    try:main()
    except Exception:
        print('MASTER_SYNC_STOPPED; credentials and spreadsheet content suppressed');raise SystemExit(2)
