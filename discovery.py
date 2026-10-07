"""Persistent nationwide search rotation and bounded, source-backed discovery."""
import json
import os
import re
import time
from html.parser import HTMLParser
from urllib import request, error
from urllib.parse import urljoin, urlsplit
from collector_common import canonical, Deferred, digest
from identity import host
from verification import Page, professions, portal, TRAINING, LINK_WORDS

REGIONS=['Baden-Württemberg','Bayern','Berlin','Brandenburg','Bremen','Hamburg','Hessen','Mecklenburg-Vorpommern','Niedersachsen','Nordrhein-Westfalen','Rheinland-Pfalz','Saarland','Sachsen','Sachsen-Anhalt','Schleswig-Holstein','Thüringen']
QUERIES=['Ausbildung Hotelfachmann Hotelfachfrau 2027 Hotel','Auszubildende Hotelfach Hotel Karriere','Ausbildung Hotelmanagement Hotel 2027','Ausbildung Restaurants Veranstaltungsgastronomie Hotel','Fachkraft Gastronomie Hotel Ausbildung','Koch Köchin Ausbildung Hotel 2027','Fachkraft Küche Hotel Ausbildung','Azubi Hotel Ausbildungsbeginn 2027']
FAMILIES=[('employer',''),('association','DEHOGA Ausbildungsbetriebe'),('chamber','IHK Ausbildungsbetrieb Hotel'),('hospitality_portal','site:hotelcareer.de'),('regional_portal','site:ausbildungskompass.de'),('public_employment','site:arbeitsagentur.de')]

def search_query(cursor):
    # Rotate region every query and occupation each complete nationwide pass.
    region=REGIONS[(cursor//len(FAMILIES))%len(REGIONS)]
    occupation=QUERIES[(cursor//(len(REGIONS)*len(FAMILIES)))%len(QUERIES)]
    family,term=FAMILIES[cursor%len(FAMILIES)]
    return family, f'{occupation} {region} {term}'.strip()

class Search:
    def __init__(self, client, state, key=None):
        self.client=client;self.state=state;self.key=key if key is not None else os.getenv('SERPER_API_KEY','')
    def search(self, query):
        if not self.key:return []
        c=self.client;url='https://google.serper.dev/search';c.allowed.add('google.serper.dev')
        # Documented API, not a website crawl; shares run/host budgets and cooldowns.
        c.reserve(url)
        req=request.Request(url,data=json.dumps({'q':query,'gl':'de','hl':'de','num':10}).encode(),method='POST',headers={'X-API-KEY':self.key,'Content-Type':'application/json','User-Agent':'HotelDirectoryCollector/3.0'})
        try:
            with request.urlopen(req,timeout=25) as response:
                result=json.load(response)
            return result.get('organic',[])[:10]
        except error.HTTPError as exc:
            c.pause(url,exc.code,exc.headers);raise Deferred('SEARCH_HTTP_'+str(exc.code)) from None
        except (error.URLError,TimeoutError,OSError,ValueError):
            c.pause(url,0);raise Deferred('SEARCH_NETWORK') from None

class Structured(HTMLParser):
    def __init__(self, raw):
        super().__init__();self.inside=False;self.buffer='';self.items=[];self.feed(raw)
    def handle_starttag(self,tag,attrs):
        if tag=='script' and dict(attrs).get('type','').lower()=='application/ld+json':self.inside=True;self.buffer=''
    def handle_data(self,data):
        if self.inside:self.buffer+=data
    def handle_endtag(self,tag):
        if tag=='script' and self.inside:
            self.inside=False
            try:self.items.extend(self.walk(json.loads(self.buffer)))
            except (ValueError,TypeError):pass
    def walk(self,obj):
        if isinstance(obj,list):
            for x in obj:yield from self.walk(x)
        elif isinstance(obj,dict):
            yield obj
            for x in obj.values():
                if isinstance(x,(dict,list)):yield from self.walk(x)

def parse_discovery(url,raw,now):
    page=Page(raw);out=[]
    for obj in Structured(raw).items:
        typ=obj.get('@type','')
        if typ!='JobPosting':continue
        desc=Page(str(obj.get('description',''))).text
        title=str(obj.get('title',''))
        ps=professions(title+' '+desc)
        org=obj.get('hiringOrganization',{})
        if not ps or not isinstance(org,dict) or not org.get('name'):continue
        locations=obj.get('jobLocation',{})
        if isinstance(locations,dict):locations=[locations]
        # A chain posting without a physical location is unresolved; never explode vague locations.
        if not isinstance(locations,list):continue
        for loc in locations:
            address=loc.get('address',{}) if isinstance(loc,dict) else {}
            if not isinstance(address,dict):continue
            country=address.get('addressCountry','DE')
            if isinstance(country,dict):country=country.get('name','')
            if country not in ('DE','Deutschland','Germany'):continue
            website=org.get('url') or org.get('sameAs') or ('' if portal(url) else url)
            if not isinstance(website,str):website=''
            out.append({'name':str(org['name']),'city':str(address.get('addressLocality','')),'street':str(address.get('streetAddress','')),'postcode':str(address.get('postalCode','')),
                        'website':website,'emails':[],'source':url,'profession':'; '.join(ps),'evidence':'Discovery: structured employer/job listing; official verification pending','last_seen':now,'first_seen':now})
    # Direct employer pages can identify their property in title/H1. Identity still verified downstream.
    if not out and not portal(url) and professions(page.text):
        heading=' '.join(page.headings)
        pieces=re.split(r'\s[|–—]\s|\s-\s',heading)
        names=[p.strip() for p in pieces if re.search(r'hotel|resort|gasthof|hostel',p,re.I) and not TRAINING.search(p) and 5<len(p)<130]
        if names:
            out.append({'name':names[-1],'city':'','website':url,'emails':[],'source':url,'profession':'; '.join(professions(page.text)),
                        'evidence':'Discovery: employer page; identity and recipient verification pending','first_seen':now,'last_seen':now})
    return out

def push_urls(state,urls,family='employer'):
    queue=state.setdefault('discovery_queue',[])
    known={x['url'] for x in queue}
    visited=state.setdefault('discovery_visited',{})
    now=time.time()
    for url in urls:
        try:canonical(url)
        except (ValueError,TypeError):continue
        if url in known or visited.get(url,0)>now:continue
        queue.append({'url':url,'family':family,'queued_at':now});known.add(url)

def expand_links(state,url,raw,family):
    page=Page(raw);links=[]
    for href,label in page.links:
        target=urljoin(url,href)
        if not target.startswith('https://') or not LINK_WORDS.search(label+' '+target):continue
        if re.search(r'facebook|instagram|linkedin|youtube|twitter|tiktok',host(target)):continue
        if portal(url) and host(target)!=host(url):
            if re.search(r'website|homepage|arbeitgeber|unternehmen|karriere',label,re.I):links.append(target)
        elif host(target)==host(url):links.append(target)
    push_urls(state,links[:12],family)
