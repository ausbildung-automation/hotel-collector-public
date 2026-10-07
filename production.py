"""One sequential collect -> verify -> checkpoint -> atomic Sheet projection."""
import copy
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, quote
from collector_common import validate, canonical, keys, digest, norm, Deferred, SourceError
from collector_engine import parse_source, source_ready, source_success, source_deferred
from collector_http import CourteousHTTP
from github_state import GitHubState
from identity import canonicalize, upsert, same, host, tokens
from verification import Page, verify_page, best_email, publishable, quality, LINK_WORDS, ATS, portal, same_site
from discovery import Structured, Search, search_query, parse_discovery, push_urls, expand_links, FAMILIES

TAB='COLLECTOR_NEW'
HEADERS=['Hotel','Email','Website','Profession','Collection status','Source evidence','Collected UTC','Source','Review decision','Your notes','Stable ID']

class Sheets:
    def __init__(self):
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        info=json.loads(os.environ['GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON'])
        if info.get('type')!='service_account' or info.get('token_uri')!='https://oauth2.googleapis.com/token':raise ValueError('Invalid service account')
        self.session=AuthorizedSession(service_account.Credentials.from_service_account_info(info,scopes=['https://www.googleapis.com/auth/spreadsheets']))
        sid=os.environ.get('GOOGLE_SHEETS_ID','')
        if sid!='1qBViUSJvnnkagG_2XPlxSgeHQOJA4MDm0_gFKitrxcI':raise ValueError('Unexpected Sheet')
        self.base='https://sheets.googleapis.com/v4/spreadsheets/'+sid
        self.props={x['properties']['title']:x['properties'] for x in self.req('GET','?fields=sheets.properties')['sheets']}
        if TAB not in self.props or 'COLLECTOR_DUPLICATE_REVIEW' in self.props:raise ValueError('Unexpected collector tabs')
    def req(self,method,route,body=None):
        r=self.session.request(method,self.base+route,json=body,timeout=45)
        if not r.ok:raise RuntimeError('Sheets request failed; content suppressed')
        return r.json()
    def values(self,tab,start=1,end=None,cols='K'):
        if tab not in self.props:raise ValueError('Required history tab missing')
        end=end or self.props[tab]['gridProperties']['rowCount']
        return self.req('GET','/values/'+quote(f"'{tab}'!A{start}:{cols}{end}",safe='')).get('values',[])
    def collector(self):
        rows=self.values(TAB,3)
        if not rows or rows[0]!=HEADERS:raise ValueError('Collector headers changed')
        ids=[r[10] for r in rows[1:] if any(r) and len(r)>10]
        if len(ids)!=len(set(ids)) or any(any(r) and (len(r)<11 or not r[10]) for r in rows[1:]):raise ValueError('Invalid collector IDs')
        return rows

def ingest_sheet(state,rows):
    if rows[0]!=HEADERS:raise ValueError('Unexpected headers')
    canonicalize(state)
    humans=state.setdefault('human_fields',{})
    for raw in rows[1:]:
        if not any(raw):continue
        row=(raw+['']*11)[:11];cid=row[10]
        if not cid:raise ValueError('Missing identity')
        humans[cid]={'decision':row[8],'notes':row[9]}
        record={'name':row[0],'emails':re.findall(r'[^\s;]+@[^\s;]+',row[1]),'website':row[2],
                'profession':row[3],'evidence':row[5],'source':row[7],'collection_id':cid,'first_seen':time.time(),'last_seen':time.time(),'aliases':[cid]}
        # Keep corrected Sheet values as candidates; never treat free-text claims as verification.
        target=state['aliases'].get(cid,cid)
        if target in state['entities']:
            record['first_seen']=state['entities'][target].get('first_seen',record['first_seen'])
        canonical_id,_=upsert(state,record)
        state['entities'][canonical_id]['website']=row[2] or state['entities'][canonical_id].get('website','')
        state['entities'][canonical_id]['was_visible']=True
    state.setdefault('migration',{}).setdefault('original_sheet',copy.deepcopy(rows))
    state['migration'].setdefault('original_ids',[r[10] for r in rows[1:] if len(r)>10])
    state['migration']['version']=3

def ingest_history(state, registry, do_not_repeat):
    history=[]
    for rows,kind in ((registry,'registry'),(do_not_repeat,'do_not_repeat')):
        for row in rows[1:]:
            if not row or not row[0]:continue
            row=row+['']*8
            if kind=='registry':record={'name':row[1],'domain':row[2],'emails':[row[3]] if row[3] else [],'city':row[5],'history_id':row[0]}
            else:record={'name':row[0],'city':row[1],'domain':row[2],'history_id':digest('|'.join(str(x) for x in row[:5]))}
            if record['name']:history.append(record)
    state['historical_entities']=history
    state['history_refreshed_at']=time.time()

def check_history(state,record):
    record.pop('historical_match',None);record.pop('history_risk',None)
    for previous in state.get('historical_entities',[]):
        if same(previous,record):
            record['historical_match']=previous.get('history_id','confirmed');return
    # Old hashed snapshots remain intact. Name+domain or name+email corroborate history.
    identity=keys(record); matched=identity & set(state.get('history_keys',{}))
    kinds={x[:2] for x in matched}
    if tokens(record.get('name','')) and 'c:' in kinds:
        record['historical_match']='legacy_name_and_city'
    elif 'n:' in kinds and kinds & {'d:','e:','u:'}:
        # Hash-only history cannot establish which chain location was contacted.
        different_locations=any(norm(h.get('name',''))==norm(record.get('name','')) and h.get('city') and record.get('city') and norm(h['city'])!=norm(record['city']) for h in state.get('historical_entities',[]))
        if not different_locations:record['history_risk']='legacy_identity_requires_location_check'
    elif kinds & {'e:','p:'} and not record.get('city'):
        record['history_risk']='shared_recipient_without_location'

def adopt(state,lead,report):
    cid,new=upsert(state,lead)
    record=state['entities'][cid]
    check_history(state,record)
    report['raw_leads']+=1
    if new and not record.get('historical_match'):report['new_identities']+=1
    else:report['duplicates_suppressed']+=1
    return record

def error(report,reason):
    report['errors'][reason]=report['errors'].get(reason,0)+1

def fetch(client,url,refresh=24):
    canonical(url)
    client.allowed.add(urlsplit(url).hostname)
    if url in client.cache:return client.cache[url]
    return client.get(url,refresh_hours=refresh)

def enrich(record,state,client,search,report,config):
    report['enrichment_attempts']+=1
    now=client.clock(); queue=record.setdefault('verify_queue',[])
    if not queue and record.get('website'):queue.append({'url':record['website'],'linked_from':''})
    if not record.get('website') or (record.get('attempts',0)>0 and not queue):
        if search.key:
            try:
                results=search.search('"'+record['name']+'" '+record.get('city','')+' offizielle Website Ausbildung Kontakt')
                for item in results[:3]:
                    u=item.get('link','')
                    if u.startswith('https://') and not portal(u):queue.append({'url':u,'linked_from':'','candidate_site':True})
            except Deferred as exc:error(report,str(exc))
    tried=set(); verified_before=bool(best_email(record,now))
    pages=0
    while queue and pages<config.get('pages_per_hotel',4):
        item=queue[0];url=item['url']
        if url in tried:queue.pop(0);continue
        tried.add(url)
        try:raw=fetch(client,url,24)
        except (Deferred,ValueError) as exc:
            reason=str(exc);error(report,reason)
            if reason in ('RUNTIME_LIMIT','RUN_REQUEST_LIMIT'):break
            if reason in ('HOST_REQUEST_LIMIT','HOST_COOLDOWN','LONG_CRAWL_DELAY'):break
            queue.pop(0);continue
        queue.pop(0);pages+=1
        # A search candidate must identify the hotel before adopting its site.
        if item.get('candidate_site'):
            from verification import exact_identity
            if not exact_identity(record,Page(raw).text):continue
            record['website']=url
        from verification import exact_identity
        if same_site(url,record.get('website','')):
            for obj in Structured(raw).items:
                address=obj.get('address',{})
                if isinstance(address,dict) and obj.get('name') and exact_identity(record,str(obj['name'])+' '+str(address.get('addressLocality',''))):
                    for field,source_field in [('city','addressLocality'),('postcode','postalCode'),('street','streetAddress')]:
                        if not record.get(field) and isinstance(address.get(source_field),str):record[field]=address[source_field]
                    record.setdefault('location_url',url)
        verify_page(record,url,raw,now,linked_from=item.get('linked_from',''))
        page=Page(raw)
        links=[]
        for href,label in page.links:
            u=urljoin(url,href)
            if not u.startswith('https://') or not LINK_WORDS.search(label+' '+u):continue
            same_domain=same_site(u,record.get('website',''))
            ats=any(host(u)==d or host(u).endswith('.'+d) for d in ATS)
            if same_domain or (same_site(url,record.get('website','')) and ats):
                links.append((0 if re.search(r'ausbildung|karriere|career|jobs',label+' '+u,re.I) else 1,u,url if ats else ''))
        known={x['url'] for x in queue}|tried
        for _,u,origin in sorted(links):
            if u not in known and len(queue)<16:
                queue.append({'url':u,'linked_from':origin});known.add(u)
    check_history(state,record)
    current=best_email(record,now)
    if current:record['selected_email']=current['email']
    if current and not verified_before:report['emails_verified']+=1
    record['attempts']=record.get('attempts',0)+1
    record['last_attempt']=now
    record['retry_after']=now+(7*86400 if publishable(record,now) else min(7*86400,3*3600*2**min(record['attempts']-1,5)))
    if not queue and record.get('website'):queue.append({'url':record['website'],'linked_from':''})

def run_cycle(state,config,client,checkpoint=lambda:None):
    canonicalize(state)
    report={k:0 for k in ('requests','hosts','sources_attempted','raw_leads','new_identities','duplicates_suppressed','enrichment_attempts','emails_verified','pending','published','explicit_2027')}
    report['errors']={};report['families_attempted']=[]
    search=Search(client,state);report['search_enabled']=bool(search.key)
    started=client.clock();initial=client.requests
    if search.key:
        cursor=state.get('search_cursor',0);family,query=search_query(cursor)
        try:
            results=search.search(query)
            push_urls(state,[x.get('link','') for x in results],family)
            state['search_cursor']=cursor+1
            report['sources_attempted']+=1;report['families_attempted'].append(family)
        except Deferred as exc:error(report,str(exc))
    for lead in state.get('pending',[]):adopt(state,lead,report)
    state['pending']=[]
    # Reclassify legacy weak domain matches using the refreshed, location-aware history.
    for r in state['entities'].values():check_history(state,r)
    def due():
        return sorted((r for r in state['entities'].values() if not r.get('historical_match') and r.get('retry_after',0)<=client.clock()),
                      key=lambda r:(-quality(r),r.get('last_attempt',0),r.get('first_seen',0)))
    # Reserve meaningful discovery capacity, while spending most requests on verification.
    enrichment_ceiling=initial+int(client.max_requests*.55)
    for record in due():
        if client.requests>=enrichment_ceiling:break
        enrich(record,state,client,search,report,config);checkpoint()
        if client.deadline and client.clock()+35>=client.deadline:break
    # Fetch diversified sources fairly by least recently successful, not fixed name order.
    sources=sorted(config['sources'],key=lambda s:state.get('source_meta',{}).get(s['url'],{}).get('last_success',0))
    for source in sources:
        if client.requests>=client.max_requests*.8:break
        if not source_ready(state,source,client.clock()):continue
        family=source.get('family','employer')
        report['sources_attempted']+=1;report['families_attempted'].append(family)
        try:
            raw=fetch(client,source['url'],source.get('refresh_hours',24))
            leads=parse_source(source,raw)
            for lead in leads:
                record=adopt(state,lead,report)
                if source.get('adapter')=='single_opportunity':verify_page(record,source['url'],raw,client.clock())
            source_success(state,source,len(leads),client.clock())
            expand_links(state,source['url'],raw,family)
        except (Deferred,SourceError,ValueError) as exc:
            source_deferred(state,source,str(exc),client.clock());error(report,str(exc))
            if str(exc) in ('RUNTIME_LIMIT','RUN_REQUEST_LIMIT'):break
        checkpoint()
    # Search snippets are only URL pointers. All claims are checked on fetched sources.
    for _ in range(max(0,config.get('search_queries_per_run',2)-1)):
        if not search.key or client.requests>=client.max_requests*.9:break
        cursor=state.get('search_cursor',0);family,query=search_query(cursor)
        try:
            results=search.search(query)
            push_urls(state,[x.get('link','') for x in results],family)
            state['search_cursor']=cursor+1
            report['sources_attempted']+=1;report['families_attempted'].append(family)
        except Deferred as exc:error(report,str(exc));break
    queue=state.setdefault('discovery_queue',[])
    for _ in range(min(len(queue),config.get('discovery_pages_per_run',12))):
        if client.requests>=client.max_requests:break
        item=queue.pop(0);url=item['url']
        try:
            raw=fetch(client,url)
            leads=parse_discovery(url,raw,client.clock())
            for lead in leads:adopt(state,lead,report)
            expand_links(state,url,raw,item['family'])
            state.setdefault('discovery_visited',{})[url]=client.clock()+7*86400
            report['sources_attempted']+=1;report['families_attempted'].append(item['family'])
        except (Deferred,ValueError) as exc:
            reason=str(exc);error(report,reason)
            if reason in ('RUNTIME_LIMIT','RUN_REQUEST_LIMIT','HOST_REQUEST_LIMIT','HOST_COOLDOWN'):
                queue.append(item)
            else:state.setdefault('discovery_visited',{})[url]=client.clock()+7*86400
            if reason in ('RUNTIME_LIMIT','RUN_REQUEST_LIMIT'):break
        checkpoint()
    for record in due():
        if client.requests>=client.max_requests or (client.deadline and client.clock()+35>=client.deadline):break
        enrich(record,state,client,search,report,config);checkpoint()
    report['requests']=client.requests;report['hosts']=len(client.host_requests)
    report['published']=sum(publishable(r,client.clock()) for r in state['entities'].values())
    report['pending']=sum(not publishable(r,client.clock()) and not r.get('historical_match') for r in state['entities'].values())
    report['historical_suppressed']=sum(bool(r.get('historical_match')) for r in state['entities'].values())
    report['explicit_2027']=sum(publishable(r,client.clock()) and any(e['status']=='EXPLICIT_2027' for e in r.get('training_evidence',[])) for r in state['entities'].values())
    report['families_attempted']=sorted(set(report['families_attempted']))
    report['duration_seconds']=round(client.clock()-started,1)
    state['last_report']=report;state['last_run_at']=client.clock();checkpoint()
    return report

def rows_for(state,existing):
    humans=state.setdefault('human_fields',{})
    for r in existing[1:]:
        if len(r)>10:humans[r[10]]={'decision':r[8],'notes':r[9]}
    out=[]
    positions={r[10]:i for i,r in enumerate(existing[1:]) if len(r)>10}
    records=state['entities'].items()
    for cid,r in sorted(records,key=lambda item:min([positions.get(a,10**9) for a in item[1].get('aliases',[item[0]])] or [10**9])):
        if not publishable(r):continue
        ev=best_email(r);decisions=[];notes=[]
        for a in r.get('aliases',[cid]):
            human=humans.get(a,{})
            d=human.get('decision','')
            if d and d!='Not reviewed' and d not in decisions:decisions.append(d)
            n=human.get('notes','')
            if n and n not in notes:notes.append(n)
        if len(decisions)>1:raise ValueError('Conflicting human decisions; no write')
        training=r['training_evidence'];explicit=any(x['status']=='EXPLICIT_2027' for x in training)
        evidence='Email explicitly published: '+ev['url']+' | Training: '+'; '.join(dict.fromkeys(e['url'] for e in training))
        out.append([r['name'],ev['email'],r.get('website',''),r.get('profession',''),'EXPLICIT_2027' if explicit else 'CURRENT_AUSBILDUNG_NO_2027_DATE',evidence,
                    datetime.fromtimestamp(r.get('first_seen',time.time()),timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),r.get('source',''),decisions[0] if decisions else 'Not reviewed',' | '.join(notes),cid])
    if len({r[10] for r in out})!=len(out):raise ValueError('Identity collision')
    return out

def sync(sheet,state,checkpoint):
    existing=sheet.collector();ingest_sheet(state,existing)
    rows=rows_for(state,existing)
    # Archive latest human edits and unresolved rows before any mutation.
    state['last_sheet_snapshot']=copy.deepcopy(existing);checkpoint()
    if sheet.collector()!=existing:raise ValueError('Sheet edited concurrently; retry next cycle')
    sid=sheet.props[TAB]['sheetId'];end=max(len(rows),len(existing)-1)
    padded=rows+[['']*11 for _ in range(end-len(rows))]
    requests=[]
    if end+3>sheet.props[TAB]['gridProperties']['rowCount']:
        requests.append({'updateSheetProperties':{'properties':{'sheetId':sid,'gridProperties':{'rowCount':end+103}},'fields':'gridProperties.rowCount'}})
    if padded:
        requests.append({'updateCells':{'range':{'sheetId':sid,'startRowIndex':3,'endRowIndex':end+3,'startColumnIndex':0,'endColumnIndex':11},
            'rows':[{'values':[{'userEnteredValue':{'stringValue':str(v)}} for v in row]} for row in padded],'fields':'userEnteredValue'}})
    for column in (2,7):
        if end:
            requests.append({'repeatCell':{'range':{'sheetId':sid,'startRowIndex':3,'endRowIndex':end+3,'startColumnIndex':column,'endColumnIndex':column+1},'cell':{'userEnteredFormat':{'textFormat':{}}},'fields':'userEnteredFormat.textFormat.link'}})
    stamp=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    requests.append({'updateCells':{'range':{'sheetId':sid,'startRowIndex':1,'endRowIndex':2,'startColumnIndex':0,'endColumnIndex':1},'rows':[{'values':[{'userEnteredValue':{'stringValue':'Last sync UTC: '+stamp+' — verified email and training evidence; review only; no sending.'}}]}],'fields':'userEnteredValue'}})
    sheet.req('POST',':batchUpdate',{'requests':requests})
    after=sheet.collector()
    if after[1:]!=rows:raise ValueError('Sheet readback mismatch')
    state['last_sync']={'timestamp':stamp,'rows':len(rows),'blank_emails':0};checkpoint()
    return len(rows)

def main(sync_only=False):
    config=json.loads(Path('config.json').read_text())
    started=time.time()
    backend=GitHubState(config['private_repo'],config['state_branch'],os.environ['PRIVATE_COLLECTOR_TOKEN'])
    state,sha=backend.read(config['state_path']);validate(state)
    sheet=Sheets();existing=sheet.collector()
    ingest_sheet(state,existing)
    ingest_history(state,sheet.values('_Registry',cols='G'),sheet.values('Do_Not_Repeat',cols='E'))
    last_checkpoint=0
    def save(force=True):
        nonlocal sha,last_checkpoint
        if not force and time.time()-last_checkpoint<60:return
        result=backend.write(config['state_path'],state,sha)
        sha=result.get('content',{}).get('sha') or result.get('sha')
        if not sha:_,sha=backend.read(config['state_path'])
        last_checkpoint=time.time()
    save()
    client=CourteousHTTP(state,[],max_requests=config.get('max_requests_per_run',120),max_requests_per_host=config.get('max_requests_per_host_per_run',4),deadline=started+config.get('network_runtime_seconds',3000))
    if not sync_only:
        try:report=run_cycle(state,config,client,lambda:save(False))
        finally:save()
    rows=sync(sheet,state,save)
    report=state.get('last_report',{}).copy();report['visible_rows']=rows;report['master_sync']='success'
    print(json.dumps(report))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('PRODUCTION_STOPPED: '+type(exc).__name__+'; private content suppressed')
        raise SystemExit(2)
