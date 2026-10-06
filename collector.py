"""Low-request hotel discovery. No SMTP, browser impersonation or block bypass."""
from __future__ import annotations
import argparse, copy, hashlib, json, os, random, re, time, unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib import error, parse, request, robotparser

BOT = 'HotelDirectoryCollector/1.0'
EMAIL = re.compile(r'[A-Z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}', re.I)

def norm(value):
    value = re.sub(r'^(?:[rq]?\d+[_\s-]+)+', '', str(value), flags=re.I)
    value = unicodedata.normalize('NFKD', value.casefold().replace('&', ' und '))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in value if not unicodedata.combining(c))).split())

def digest(value):
    return hashlib.sha256(value.casefold().strip().encode()).hexdigest()

def canonical(url):
    p = parse.urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None,443):
        raise ValueError('Only public HTTPS URLs allowed')
    host = p.hostname.lower().removeprefix('www.')
    if host in {'localhost','api.github.com'} or ':' in host or re.fullmatch(r'[0-9.]+',host):
        raise ValueError('Invalid source host')
    return parse.urlunsplit(('https',host,p.path.rstrip('/') or '/',p.query,''))

def keys(lead):
    result = {'n:' + digest(norm(lead['name']))}
    if lead.get('city'):
        result.add('c:' + digest(norm(lead['name']) + '|' + norm(lead['city'])))
    for email in lead.get('emails',[]):
        result.add('e:' + digest(email));result.add('p:' + digest(email)[:16])
    if lead.get('website'):
        url = canonical(lead['website'])
        result.add('u:' + digest(url));result.add('d:' + digest(parse.urlsplit(url).hostname))
    if lead.get('domain'):
        result.add('d:' + digest(lead['domain'].lower().removeprefix('www.')))
    return result

def blank():
    return {'version':1,'history_keys':{},'records':{},'index':{},'hosts':{},'pages':{},'pending':[],'review_candidates':{}}

def validate(state):
    if state.get('version') != 1 or not all(isinstance(state.get(k),dict) for k in ('history_keys','records','index','hosts','pages')) or not isinstance(state.get('pending'),list):
        raise ValueError('Missing or malformed collection history; refusing empty fallback')
    return state

def add(state, lead, historical=False):
    lead = copy.deepcopy(lead)
    lead['name'] = lead['name'].strip()
    if not norm(lead['name']):
        return 'INVALID'
    lead['emails'] = sorted({e.strip().lower() for e in lead.get('emails',[]) if EMAIL.fullmatch(e.strip())})
    identity = keys(lead)
    if historical:
        for key in identity:
            state['history_keys'][key] = 'MASTER_COLLECTED'
        return 'IMPORTED'
    def review(match):
        candidate_id=digest(norm(lead['name'])+'|'+norm(lead.get('city',''))+'|'+lead.get('website',''))
        state.setdefault('review_candidates',{}).setdefault(candidate_id,{**lead,'status':'ALREADY_COLLECTED_REVIEW','matched_key':match})
        return 'ALREADY_COLLECTED_REVIEW'
    # City-qualified name and property URL are strong; shared email/domain alone needs review.
    for key in sorted(identity):
        if key in state['history_keys']:
            return review(key)
        if key in state['index']:
            existing = state['records'][state['index'][key]]
            if key.startswith('c:') or (key.startswith('u:') and norm(existing['name']) == norm(lead['name'])) or (key.startswith('n:') and norm(existing.get('city','')) == norm(lead.get('city',''))):
                existing['last_seen'] = lead.get('last_seen',time.time())
                existing['sources'] = sorted(set(existing['sources'] + [lead['source']]))
                return 'DUPLICATE'
            return review(key)
    key = digest(norm(lead['name']) + '|' + norm(lead.get('city','')) + '|' + lead.get('website',''))
    lead.update(status='NEEDS_REVIEW',sources=[lead['source']],first_seen=time.time(),last_seen=time.time(),collection_id=key)
    state['records'][key] = lead
    for identity_key in identity:
        state['index'][identity_key] = key
    return 'NEW'

class DirectoryParser(HTMLParser):
    """DEHOGA-style h3 cards; never borrow an email from a neighbouring card."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards=[];self.card=None;self.heading=False;self.skip=0;self.anchor=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in {'script','style'}: self.skip+=1
        if tag=='h3':
            if self.card: self.cards.append(self.card)
            self.card={'name':'','text':[],'links':[]};self.heading=True
        if tag=='a' and self.card:
            self.anchor=[attrs.get('href',''),'']
    def handle_endtag(self,tag):
        if tag in {'script','style'}: self.skip=max(0,self.skip-1)
        if tag=='h3': self.heading=False
        if tag=='a' and self.anchor and self.card:
            self.card['links'].append(self.anchor);self.anchor=None
    def handle_data(self,data):
        if self.skip or not self.card: return
        if self.heading: self.card['name']+=data
        else: self.card['text'].append(data)
        if self.anchor: self.anchor[1]+=data
    def leads(self,source):
        cards=self.cards + ([self.card] if self.card else [])
        for card in cards:
            text=' '.join(card['text'])
            if not re.search(r'Hotelfach(?:frau|mann|leute)',text,re.I): continue
            websites=[href for href,label in card['links'] if norm(label)=='website' and href.startswith('https://')]
            if len(set(websites)) != 1 or not card['name'].strip(): continue
            emails=EMAIL.findall(text)
            for href,_ in card['links']:
                if href.lower().startswith('mailto:'): emails+=EMAIL.findall(parse.unquote(href[7:].split('?')[0]))
            city_match=re.search(r'\b\d{5}\s+([^\n]+?)\s+(?:Ausbildungsberuf|Ansprechpartner)',text)
            yield {'name':card['name'].strip(),'city':city_match.group(1).strip() if city_match else '',
                'website':canonical(websites[0]),'emails':sorted(set(emails)),
                'source':source,'evidence':'Directory lists Hotelfach training; current intake and email acceptance require review',
                'profession':'Hotelfachmann/-frau','last_seen':time.time()}

class Deferred(Exception):
    pass

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None

class CourteousHTTP:
    def __init__(self,state,allowed_hosts,opener=None,clock=time.time,sleeper=time.sleep,rng=random.uniform,max_requests=12):
        self.state=state;self.allowed=set(allowed_hosts);self.opener=opener or request.build_opener(NoRedirect()).open
        self.clock=clock;self.sleep=sleeper;self.rng=rng;self.requests=0;self.max_requests=max_requests
    def host(self,url):
        canonical(url);host=parse.urlsplit(url).hostname.lower()
        if host not in self.allowed: raise Deferred('SOURCE_NOT_ALLOWLISTED')
        return host
    def pause(self,url,status,headers=None):
        host=self.host(url);s=self.state['hosts'].setdefault(host,{})
        failures=s.get('failures',0)+1;s['failures']=failures
        seconds=min(86400,900*2**min(failures-1,6))
        if status in (401,403): seconds=max(seconds,86400)
        value=(headers or {}).get('Retry-After','')
        if value:
            try: seconds=max(seconds,float(value))
            except ValueError:
                try: seconds=max(seconds,parsedate_to_datetime(value).timestamp()-self.clock())
                except (ValueError,TypeError,OverflowError): pass
        s['until']=self.clock()+seconds;s['last_status']=status
    def raw(self,url,headers=None):
        host=self.host(url);s=self.state['hosts'].setdefault(host,{})
        if s.get('until',0)>self.clock(): raise Deferred('HOST_COOLDOWN')
        if self.requests>=self.max_requests: raise Deferred('RUN_REQUEST_LIMIT')
        delay=max(15,s.get('delay',15)) + self.rng(0,10)
        remaining=s.get('last_request',0)+delay-self.clock()
        if remaining>60: raise Deferred('LONG_CRAWL_DELAY')
        if remaining>0: self.sleep(remaining)
        self.requests+=1;s['last_request']=self.clock()
        try:
            req=request.Request(url,headers={'User-Agent':BOT,'Accept':'text/html,text/plain;q=0.9',**(headers or {})})
            with self.opener(req,timeout=25) as response:
                body=response.read(6*1024*1024+1)
                if len(body)>6*1024*1024: raise Deferred('PAGE_TOO_LARGE')
                return body,response.headers
        except error.HTTPError as e:
            if e.code in (304,404,410): raise
            self.pause(url,e.code,e.headers);raise Deferred('HTTP_'+str(e.code)) from None
        except (error.URLError,TimeoutError,OSError):
            self.pause(url,0);raise Deferred('NETWORK_COOLDOWN') from None
    def robots(self,url):
        host=self.host(url);s=self.state['hosts'].setdefault(host,{})
        if s.get('robots_until',0)<=self.clock():
            robots_url=parse.urlunsplit(('https',host,'/robots.txt','',''))
            try: body,_=self.raw(robots_url)
            except error.HTTPError as e:
                if e.code in (404,410): body=b'User-agent: *\nDisallow:\n'
                else: raise Deferred('ROBOTS_UNAVAILABLE') from None
            s['robots']=body.decode('utf-8','replace')[:512000];s['robots_until']=self.clock()+86400
        rp=robotparser.RobotFileParser();rp.parse(s['robots'].splitlines())
        if not rp.can_fetch(BOT,url): raise Deferred('ROBOTS_DISALLOW')
        rate=rp.request_rate(BOT)
        s['delay']=max(15,rp.crawl_delay(BOT) or 0,rate.seconds/rate.requests if rate and rate.requests else 0)
    def get(self,url):
        self.robots(url)
        page=self.state['pages'].setdefault(url,{})
        if page.get('checked_at') and page['checked_at']+72*3600>self.clock(): raise Deferred('CACHED_72H')
        headers={}
        if page.get('etag'): headers['If-None-Match']=page['etag']
        if page.get('modified'): headers['If-Modified-Since']=page['modified']
        try: body,received=self.raw(url,headers)
        except error.HTTPError as e:
            page['checked_at']=self.clock()
            if e.code==304: raise Deferred('UNCHANGED') from None
            raise Deferred('REMOVED_'+str(e.code)) from None
        text=body.decode('utf-8','replace')
        if re.search(r'(cf-chl-|g-recaptcha|hcaptcha|verify you are human|access denied)',text,re.I):
            self.pause(url,403);raise Deferred('CHALLENGE_STOP')
        if 'html' not in received.get('Content-Type','text/html').lower(): raise Deferred('NON_HTML')
        page.update(checked_at=self.clock(),etag=received.get('ETag',''),modified=received.get('Last-Modified',''))
        self.state['hosts'][self.host(url)]['failures']=0
        return text

def run(state,config,client):
    report={'new':0,'already_collected':0,'deferred':0,'requests':0}
    budget=config['max_new_per_run']
    for source in config['sources']:
        if state['pending']: break
        try:
            html=client.get(source['url']);parser=DirectoryParser();parser.feed(html)
            leads=list(parser.leads(source['url']))
            if not leads:
                # Parser/source drift must not become a successful empty collection.
                state['pages'][source['url']]['checked_at']=0
                report['deferred']+=1;continue
            state['pending'].extend(leads)
        except Deferred: report['deferred']+=1
    processed=0
    while state['pending'] and report['new']<budget and processed<2000:
        candidate=state['pending'][0]
        result=add(state,candidate)
        state['pending'].pop(0);processed+=1
        if result=='NEW': report['new']+=1
        else: report['already_collected']+=1
    report['requests']=client.requests;report['remaining']=len(state['pending'])
    return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',default='config.json');p.add_argument('--state',default='state.json');p.add_argument('--persist',action='store_true');p.add_argument('--remote',action='store_true');args=p.parse_args()
    config=json.loads(Path(args.config).read_text())
    backend=None
    if args.remote:
        from github_state import GitHubState
        backend=GitHubState(config['private_repo'],config['state_branch'],os.getenv('PRIVATE_COLLECTOR_TOKEN',''))
        state,sha=backend.read(config['state_path'])
    else: state=json.loads(Path(args.state).read_text())
    validate(state)
    if args.remote and not args.persist: raise ValueError('Remote run must persist cooldowns and cursor')
    client=CourteousHTTP(state,[parse.urlsplit(s['url']).hostname for s in config['sources']],max_requests=config['max_requests_per_run'])
    try: report=run(state,config,client)
    finally:
        if args.persist:
            if backend: backend.write(config['state_path'],state,sha)
            else:
                path=Path(args.state);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(state,ensure_ascii=False));tmp.replace(path)
    # Only aggregate counts in public logs; candidates/history never printed or uploaded as artifacts.
    print(json.dumps(report))

if __name__=='__main__':
    try: main()
    except Exception as e:
        print('STOPPED: '+type(e).__name__+'; no email delivery is part of this collector')
        raise SystemExit(2)
