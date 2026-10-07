#!/usr/bin/env python3
"""Conservative continuous enricher for the Hotel Center Ausbildung Master Sheet."""
import argparse, hashlib, html, json, os, re, time, urllib.parse, urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from html.parser import HTMLParser
from github_state import GitHubState, Conflict

DEFAULT_SHEETS=["COLLECTOR_NEW","RESEARCH_RESULTS","OFFICIAL_HOTEL","MAP_RESULTS","PREMIUM_MANUAL","HIDDEN_AUSBILDUNG","JOB_PORTAL"]
HEADER_ROWS={"COLLECTOR_NEW":3,"RESEARCH_RESULTS":2,"OFFICIAL_HOTEL":2,"MAP_RESULTS":2,"PREMIUM_MANUAL":1,"HIDDEN_AUSBILDUNG":1,"JOB_PORTAL":2}
ALIASES={"hotel":{"hotel","hotel name"},"email":{"email","e-mail","mail"},"website":{"website","homepage","web"},"offer":{"offer link","direct ausbildung link","ausbildung link","job link"},"status":{"package","collection status","status"}}
FINAL=("packaged","queued","sent","rejected","duplicate","do not repeat")
CAREER=("ausbildung","azubi","karriere","career","jobs","stellen","bewerbung","hotelfach","gastgewerbe","auszubild")
CONTACT=("kontakt","contact","impressum","personal","hr","recruiting")
HOTEL_TRAINING=("hotelfachmann","hotelfachfrau","hotelfachleute","hotelfachkraft")
EMAIL_PRIORITY=[("ausbildung",100),("azubi",98),("personal",94),("hr",92),("humanresources",92),("bewerbung",90),("recruit",88),("karriere",86),("career",86),("jobs",82),("info",55),("kontakt",52),("contact",50),("rezeption",35),("reservation",20)]
BAD_LOCALS={"noreply","no-reply","donotreply","do-not-reply","privacy","datenschutz","abuse","webmaster"}
BLOCKED={"facebook.com","instagram.com","linkedin.com","youtube.com","tiktok.com","x.com","twitter.com","booking.com","tripadvisor.com","holidaycheck.de","hrs.de","expedia.de","expedia.com","indeed.com","indeed.de","stepstone.de","meinestadt.de","hotelcareer.de","hogapage.de","arbeitsagentur.de","ausbildung.de","azubiyo.de"}
GENERIC={"gmail.com","outlook.com","hotmail.com","yahoo.com","gmx.de","web.de"}
EMAIL_RE=re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
BOT="HotelCenterEnricher/1.0 (+courteous employer-site verifier)"

@dataclass
class Candidate:
    value:str; score:int; source:str; evidence:str

@dataclass
class Row:
    sheet:str; row:int; hotel:str; email:str; website:str; offer:str; status:str; cols:dict

def norm(v): return " ".join(str(v or "").replace("\u00a0"," ").strip().casefold().split())

def clean_url(v):
    v=str(v or "").strip()
    if not v:return ""
    if v.startswith("//"):v="https:"+v
    if not re.match(r"^https?://",v,re.I):v="https://"+v.lstrip("/")
    p=urllib.parse.urlsplit(v)
    return urllib.parse.urlunsplit((p.scheme,p.netloc,p.path.rstrip("/") or "/",p.query,""))

def host(v):
    try:h=(urllib.parse.urlsplit(clean_url(v)).hostname or "").lower().strip(".")
    except Exception:return ""
    return h[4:] if h.startswith("www.") else h

def base_domain(v):
    h=host(v) if "://" in str(v) else str(v or "").lower().strip(".")
    if h.startswith("www."):h=h[4:]
    p=[x for x in h.split(".") if x]
    if len(p)<=2:return h
    if p[-2] in {"co","com","org","net"} and len(p[-1])==2:return ".".join(p[-3:])
    return ".".join(p[-2:])

def same_domain(a,b):
    da=base_domain(host(a) if "://" in str(a) else a); db=base_domain(host(b) if "://" in str(b) else b)
    return bool(da and db and da==db)

def email_domain(email):
    e=norm(email); return e.rsplit("@",1)[1] if "@" in e else ""

def blocked(url):
    h=host(url); return any(h==d or h.endswith("."+d) for d in BLOCKED)

def finalized(status): return any(x in norm(status) for x in FINAL)

def col_letter(n):
    out=""
    while n:n,r=divmod(n-1,26);out=chr(65+r)+out
    return out

def score_email(email,website=""):
    e=norm(email)
    if "@" not in e:return -999
    local,dom=e.rsplit("@",1)
    if local in BAD_LOCALS:return -500
    compact=re.sub(r"[^a-z0-9]","",local)
    score=max([pts for token,pts in EMAIL_PRIORITY if token in compact] or [65 if "." in local or "-" in local else 45])
    if website and same_domain(dom,website):score+=20
    return score

def score_offer(url,text,website):
    t=norm(text);score=0
    if any(x in t for x in HOTEL_TRAINING):score+=60
    if any(x in t for x in ("ausbildung","ausbildungsplatz","auszubild","azubi")):score+=25
    if any(x in url.lower() for x in CAREER):score+=10
    if website and same_domain(url,website):score+=15
    return score

def priority(r):
    p=0
    if not r.email:p+=100
    elif score_email(r.email,r.website)<80:p+=45
    if not r.website:p+=80
    if not r.offer:p+=35
    if r.sheet=="COLLECTOR_NEW":p+=25
    return p

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.links=[];self.href="";self.label=[];self.text=[];self.ignore=0
    def handle_starttag(self,tag,attrs):
        if tag in {"script","style","noscript","svg"}:self.ignore+=1
        if tag=="a":self.href=dict(attrs).get("href","") or "";self.label=[]
    def handle_endtag(self,tag):
        if tag in {"script","style","noscript","svg"} and self.ignore:self.ignore-=1
        if tag=="a":self.links.append((self.href," ".join(self.label)));self.href="";self.label=[]
    def handle_data(self,data):
        if self.ignore:return
        t=" ".join(data.split())
        if t:self.text.append(t);self.label.append(t) if self.href else None

def parse(raw):
    p=Parser()
    try:p.feed(raw)
    except Exception:pass
    return p

class Fetcher:
    def __init__(self,timeout,delay,max_host):
        self.timeout=timeout;self.delay=delay;self.max_host=max_host;self.last=0;self.count=defaultdict(int);self.cache={}
    def get(self,url):
        url=clean_url(url)
        if not url or blocked(url):return url,""
        if url in self.cache:return self.cache[url]
        h=host(url)
        if self.count[h]>=self.max_host:return url,""
        wait=self.delay-(time.time()-self.last)
        if wait>0:time.sleep(wait)
        req=urllib.request.Request(url,headers={"User-Agent":BOT,"Accept":"text/html,application/xhtml+xml"})
        try:
            self.last=time.time();self.count[h]+=1
            with urllib.request.urlopen(req,timeout=self.timeout) as resp:
                ct=(resp.headers.get("Content-Type") or "").lower()
                if "html" not in ct and "text" not in ct:out=(resp.geturl(),"")
                else:
                    raw=resp.read(2500000);charset=resp.headers.get_content_charset() or "utf-8"
                    out=(resp.geturl(),raw.decode(charset,errors="replace"))
        except Exception:out=(url,"")
        self.cache[url]=out;return out

class Serper:
    def __init__(self,key,timeout):self.key=key.strip();self.timeout=timeout
    def search(self,q,n=8):
        if not self.key:return []
        req=urllib.request.Request("https://google.serper.dev/search",data=json.dumps({"q":q,"gl":"de","hl":"de","num":min(max(n,1),20)}).encode(),method="POST",headers={"X-API-KEY":self.key,"Content-Type":"application/json","User-Agent":BOT})
        try:
            with urllib.request.urlopen(req,timeout=self.timeout) as resp:return json.load(resp).get("organic",[]) or []
        except Exception:return []

def relevant_links(base,raw,limit=25):
    p=parse(raw);scored=[]
    for href,label in p.links:
        href=html.unescape(href.strip())
        if not href or href.startswith(("mailto:","tel:","javascript:","#")):continue
        u=clean_url(urllib.parse.urljoin(base,href))
        if not same_domain(u,base):continue
        hay=norm(label+" "+u);sc=sum(8 for x in CAREER if x in hay)+sum(3 for x in CONTACT if x in hay)
        if sc:scored.append((sc,u))
    out=[];seen=set()
    for _,u in sorted(scored,reverse=True):
        if u not in seen:seen.add(u);out.append(u)
        if len(out)>=limit:break
    return out

def emails_from(raw,source,website):
    p=parse(raw);found=set()
    for href,_ in p.links:
        if href.lower().startswith("mailto:"):
            e=href[7:].split("?",1)[0].strip().lower()
            if EMAIL_RE.fullmatch(e):found.add(e)
    txt=" ".join(p.text);found.update(e.lower() for e in EMAIL_RE.findall(txt))
    deob=re.sub(r"\s*(?:\[at\]|\(at\)|\sat\s)\s*","@",txt,flags=re.I)
    deob=re.sub(r"\s*(?:\[dot\]|\(dot\)|\sdot\s)\s*",".",deob,flags=re.I)
    found.update(e.lower() for e in EMAIL_RE.findall(deob))
    out=[]
    for e in found:
        sc=score_email(e,website)
        if sc<0:continue
        if website and not same_domain(email_domain(e),website):sc-=25
        out.append(Candidate(e,sc,source,"email found on verified employer site"))
    return sorted(out,key=lambda x:x.score,reverse=True)

def crawl(website,fetcher,max_pages):
    final,raw=fetcher.get(website)
    if not raw:return None,None,final or clean_url(website)
    canonical=final or clean_url(website);q=deque([canonical]+relevant_links(canonical,raw,18));seen=set();best_e=best_o=None
    while q and len(seen)<max_pages:
        u=q.popleft()
        if u in seen:continue
        seen.add(u);fu,body=fetcher.get(u)
        if not body:continue
        for c in emails_from(body,fu,canonical):
            if best_e is None or c.score>best_e.score:best_e=c
        txt=" ".join(parse(body).text);sc=score_offer(fu,txt,canonical)
        if sc>=85:
            c=Candidate(fu,sc,fu,"official page contains Ausbildung + Hotelfach evidence")
            if best_o is None or c.score>best_o.score:best_o=c
        if any(x in norm(txt[:25000]) for x in CAREER):
            for nxt in relevant_links(fu,body,10):
                if nxt not in seen and nxt not in q:q.append(nxt)
    return best_e,best_o,canonical

def derive_website(r,fetcher):
    guesses=[];d=email_domain(r.email)
    if d and d not in GENERIC:guesses+=["https://"+d,"https://www."+d]
    if r.offer and not blocked(r.offer):
        p=urllib.parse.urlsplit(clean_url(r.offer))
        if p.scheme and p.netloc:guesses.append(f"{p.scheme}://{p.netloc}")
    for g in guesses:
        final,raw=fetcher.get(g)
        if raw and not blocked(final):return Candidate(final,75,g,"verified from existing email/offer domain")
    return None

def search_website(name,search,fetcher):
    if not search.key:return None
    tokens={x for x in re.findall(r"[a-z0-9äöüß]+",norm(name)) if len(x)>=4 and x not in {"hotel","hotels","gmbh","resort"}}
    best=None
    for item in search.search(f'"{name}" offizielle Website Hotel Deutschland',8):
        u=clean_url(item.get("link",""))
        if not u or blocked(u):continue
        fu,raw=fetcher.get(u)
        if not raw or blocked(fu):continue
        txt=norm(" ".join(parse(raw).text)[:120000]);hh=host(fu)
        sc=55+min(30,sum(8 for t in tokens if t in txt))+min(18,sum(6 for t in tokens if t in hh))
        if sc>=70:
            c=Candidate(fu,sc,u,"search result verified against hotel identity")
            if best is None or c.score>best.score:best=c
    return best

def header_map(row):
    out={}
    for i,v in enumerate(row,1):
        n=norm(v)
        for key,aliases in ALIASES.items():
            if n in aliases and key not in out:out[key]=i
    return out

def getv(row,col):return str(row[col-1]).strip() if col and col-1<len(row) else ""

class Sheets:
    def __init__(self,sid,info):
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        creds=service_account.Credentials.from_service_account_info(info,scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.s=AuthorizedSession(creds);self.base="https://sheets.googleapis.com/v4/spreadsheets/"+urllib.parse.quote(sid,safe="")
        meta=self.req("GET","?fields=sheets.properties");self.props={x["properties"]["title"]:x["properties"] for x in meta["sheets"]}
    def req(self,method,route,body=None):
        r=self.s.request(method,self.base+route,json=body,timeout=45)
        if not r.ok:raise RuntimeError("Sheets request failed; response suppressed")
        return r.json()
    def values(self,a1):return self.req("GET","/values/"+urllib.parse.quote(a1,safe="")).get("values",[])
    def batch(self,data):
        for i in range(0,len(data),300):self.req("POST","/values:batchUpdate",{"valueInputOption":"RAW","data":data[i:i+300]})

def load_rows(sheet,names):
    out=[]
    for name in names:
        props=sheet.props.get(name)
        if not props:continue
        vals=sheet.values(f"'{name}'!A1:K{props['gridProperties']['rowCount']}");hr=HEADER_ROWS.get(name,1)
        if len(vals)<hr:continue
        cols=header_map(vals[hr-1])
        if "hotel" not in cols:continue
        for rn in range(hr+1,len(vals)+1):
            row=vals[rn-1];hotel=getv(row,cols["hotel"])
            if not hotel or "batch" in norm(hotel):continue
            out.append(Row(name,rn,hotel,getv(row,cols.get("email")),getv(row,cols.get("website")),getv(row,cols.get("offer")),getv(row,cols.get("status")),cols))
    return out

def recipient_map(sheet,rows):
    out=defaultdict(set)
    for r in rows:
        if r.email:out[norm(r.email)].add(norm(r.hotel))
    props=sheet.props.get("_Registry")
    if props:
        vals=sheet.values(f"'_Registry'!A1:L{props['gridProperties']['rowCount']}")
        if vals:
            heads={norm(v):i for i,v in enumerate(vals[0])};ei=heads.get("normalized_email");hi=heads.get("hotel_name")
            if ei is not None:
                for row in vals[1:]:
                    e=norm(row[ei]) if ei<len(row) else "";h=norm(row[hi]) if hi is not None and hi<len(row) else ""
                    if e:out[e].add(h)
    return out

def load_state():
    repo=os.environ.get("PRIVATE_ENRICHER_REPO","ausbildung-automation/hotel-collector-state");branch=os.environ.get("PRIVATE_ENRICHER_BRANCH","main");path=os.environ.get("PRIVATE_ENRICHER_STATE_PATH","state/enricher_state.json")
    gh=GitHubState(repo,branch,os.environ["PRIVATE_COLLECTOR_TOKEN"]);state,sha=gh.read(path)
    if state.get("version")!=1 or not isinstance(state.get("rows"),dict):raise ValueError("Malformed enricher state")
    state.setdefault("history",[]);state["_path"]=path;return gh,state,sha

def save_state(gh,state,sha):
    path=state.pop("_path");state["last_run_utc"]=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
    try:gh.write(path,state,sha)
    except Conflict as exc:raise RuntimeError("Enricher state changed concurrently; stopping safely") from exc

def run(args):
    info=json.loads(os.environ["GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON"]);sheet=Sheets(os.environ["GOOGLE_SHEETS_ID"],info)
    gh,state,state_sha=load_state();rows=load_rows(sheet,args.sheets);recips=recipient_map(sheet,rows)
    search_key=os.environ.get("SERPER_API_KEY","").strip()
    candidates=[r for r in rows if priority(r)>0 and (args.include_finalized or not finalized(r.status)) and (search_key or r.website or r.email or r.offer)]
    candidates.sort(key=priority,reverse=True)
    fetcher=Fetcher(args.timeout,args.delay,args.max_requests_per_host);search=Serper(search_key,args.timeout)
    changes=[];processed=changed_rows=changed_cells=0;started=time.time()
    for r in candidates:
        if processed>=args.max_rows or time.time()-started>=args.max_seconds:break
        key=hashlib.sha256(f"{r.sheet}|{r.row}|{norm(r.hotel)}".encode()).hexdigest()[:24];si=state["rows"].setdefault(key,{})
        sig=hashlib.sha256(f"{r.email}|{r.website}|{r.offer}|{r.status}".encode()).hexdigest()
        if not args.force and si.get("sig")==sig and si.get("last_result")=="no_change" and time.time()-float(si.get("epoch",0))<args.cooldown_hours*3600:continue
        processed+=1;website_c=None;working=clean_url(r.website)
        if not working:
            website_c=derive_website(r,fetcher) or search_website(r.hotel,search,fetcher)
            if website_c:working=website_c.value
        best_e=best_o=None
        if working:
            best_e,best_o,canonical=crawl(working,fetcher,args.max_pages_per_hotel)
            if canonical:working=canonical
        row_changes=[]
        if not r.website and website_c and "website" in r.cols:
            changes.append({"range":f"'{r.sheet}'!{col_letter(r.cols['website'])}{r.row}","values":[[website_c.value]]});row_changes.append({"field":"website","new":website_c.value,"score":website_c.score});r.website=website_c.value;changed_cells+=1
        if best_e and "email" in r.cols:
            old=score_email(r.email,working);improve=(not r.email and best_e.score>=55) or (r.email and best_e.score>=old+12)
            owners=recips.get(norm(best_e.value),set());conflict=bool(owners and any(h and h!=norm(r.hotel) for h in owners));domain_ok=not working or same_domain(email_domain(best_e.value),working)
            if improve and not conflict and (domain_ok or best_e.score>=95):
                changes.append({"range":f"'{r.sheet}'!{col_letter(r.cols['email'])}{r.row}","values":[[best_e.value]]});row_changes.append({"field":"email","old":r.email,"new":best_e.value,"score":best_e.score});recips[norm(best_e.value)].add(norm(r.hotel));r.email=best_e.value;changed_cells+=1
        if not r.offer and best_o and best_o.score>=85 and "offer" in r.cols:
            changes.append({"range":f"'{r.sheet}'!{col_letter(r.cols['offer'])}{r.row}","values":[[best_o.value]]});row_changes.append({"field":"offer","new":best_o.value,"score":best_o.score});r.offer=best_o.value;changed_cells+=1
        if row_changes:changed_rows+=1
        si.update({"sig":hashlib.sha256(f"{r.email}|{r.website}|{r.offer}|{r.status}".encode()).hexdigest(),"epoch":time.time(),"last_result":"changed" if row_changes else "no_change"})
        state["history"].append({"ts":int(time.time()),"sheet":r.sheet,"row":r.row,"hotel":r.hotel,"changes":row_changes})
    if args.apply and changes:sheet.batch(changes)
    state["history"]=state["history"][-200:];state["last_summary"]={"mode":"APPLY" if args.apply else "DRY_RUN","processed_rows":processed,"changed_rows":changed_rows,"changed_cells":changed_cells,"search_enabled":bool(search.key),"timestamp":int(time.time())}
    save_state(gh,state,state_sha);print(json.dumps(state["last_summary"],ensure_ascii=False));return 0

def main():
    p=argparse.ArgumentParser();p.add_argument("--sheets",nargs="+",default=DEFAULT_SHEETS);p.add_argument("--max-rows",type=int,default=int(os.environ.get("ENRICH_MAX_ROWS","20")));p.add_argument("--max-seconds",type=int,default=int(os.environ.get("ENRICH_MAX_SECONDS","900")));p.add_argument("--max-pages-per-hotel",type=int,default=int(os.environ.get("ENRICH_MAX_PAGES","7")));p.add_argument("--max-requests-per-host",type=int,default=int(os.environ.get("ENRICH_MAX_HOST_REQUESTS","5")));p.add_argument("--delay",type=float,default=float(os.environ.get("ENRICH_DELAY","1.0")));p.add_argument("--timeout",type=int,default=int(os.environ.get("ENRICH_TIMEOUT","15")));p.add_argument("--cooldown-hours",type=int,default=int(os.environ.get("ENRICH_COOLDOWN_HOURS","168")));p.add_argument("--apply",action="store_true");p.add_argument("--include-finalized",action="store_true");p.add_argument("--force",action="store_true");return run(p.parse_args())

if __name__=="__main__":raise SystemExit(main())
