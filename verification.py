"""Evidence-gated publication; exact strings from fetched pages only."""
import re
import time
from urllib.parse import urljoin, urlsplit, unquote
from html.parser import HTMLParser
from collector_common import EMAIL, norm
from identity import name, tokens, host

PROFESSIONS = [
    (r'hotelfach(?:mann|frau|leute|kraft)', 'Hotelfachmann/-frau'),
    (r'hotelmanagement', 'Kaufmann/-frau für Hotelmanagement'),
    (r'restaurants?\s*(?:und|&)\s*veranstaltungsgastronomie', 'Fachmann/-frau für Restaurants und Veranstaltungsgastronomie'),
    (r'fachkraft\s+(?:für\s+)?gastronomie', 'Fachkraft für Gastronomie'),
    (r'\b(?:koch|köchin|koechin)\b', 'Koch/Köchin'),
    (r'fachkraft\s+(?:für\s+)?küche', 'Fachkraft Küche'),
]
TRAINING = re.compile(r'ausbildung|auszubild|azubi|lehrstelle', re.I)
LINK_WORDS = re.compile(r'ausbildung|azubi|karriere|career|jobs|stellen|bewerbung|personal|team|kontakt|contact|impressum', re.I)
ATS = ('personio.de', 'softgarden.io', 'onlyfy.jobs', 'recruitee.com', 'dvinci.de', 'successfactors.eu', 'myworkdayjobs.com', 'talent-soft.com')
PORTALS = ('hotelcareer.de', 'hogapage.de', 'ausbildung.de', 'azubiyo.de', 'ausbildungskompass.de', 'arbeitsagentur.de', 'dehoga-bayern.de', 'linkedin.com', 'indeed.com', 'stepstone.de', 'booking.com', 'tripadvisor.com')
BAD = ('noreply', 'no-reply', 'datenschutz', 'privacy', 'webmaster', 'abuse', 'presse', 'marketing', 'support')

class Page(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.parts=[]; self.headings=[]; self.links=[]; self.skip=0; self.heading=False; self.anchor=None
        self.feed(raw)
        self.text=' '.join(self.parts)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag in ('script','style','noscript','svg'): self.skip+=1
        if tag in ('title','h1'): self.heading=True
        if tag=='a' and not self.skip: self.anchor=[attrs.get('href',''),'']
        if tag in ('p','div','li','section','article','br'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script','style','noscript','svg'): self.skip=max(0,self.skip-1)
        if tag in ('title','h1'): self.heading=False
        if tag=='a' and self.anchor:
            self.links.append(tuple(self.anchor));self.anchor=None
        if tag in ('p','div','li','section','article'): self.parts.append('\n')
    def handle_data(self, data):
        if self.skip:return
        self.parts.append(data)
        if self.heading:self.headings.append(data)
        if self.anchor:self.anchor[1]+=data

def professions(text):
    # Training and occupation must be in the same local passage.
    result=[]
    for pattern,label in PROFESSIONS:
        for match in re.finditer(pattern,text,re.I):
            if TRAINING.search(text[max(0,match.start()-160):match.end()+160]):
                result.append(label); break
    return result

def exact_identity(record, text):
    target=name(record.get('name','')); body=name(text)
    distinctive=tokens(target)
    if not distinctive:
        return bool(target and target in body and record.get('street') and norm(record['street']) in norm(text))
    # Every distinctive token must occur; generic hotel words carry no identity weight.
    return distinctive <= set(body.split()) and (not record.get('city') or norm(record['city']) in norm(text) or target in body)

def role(email):
    local=email.split('@')[0].lower()
    if any(x in local for x in BAD):return -1
    if re.search(r'(?:^|[._-])hr(?:$|[._-])',local):return 90
    for words,score in [(('ausbildung','azubi'),100),(('talent','personal','recruit','bewerbung','career','karriere','jobs'),90),(('info','kontakt','contact'),55),(('rezeption','reception'),40),(('reservierung','reservation','booking'),30)]:
        if any(w in local for w in words):return score
    return 60

def portal(url):
    h=host(url)
    return any(h==d or h.endswith('.'+d) for d in PORTALS)

def same_site(a,b):
    a,b=host(a),host(b)
    return bool(a and b and (a==b or a.endswith('.'+b) or b.endswith('.'+a)))

def verify_page(record, url, raw, now=None, linked_from=''):
    now=time.time() if now is None else now
    page=Page(raw); text=page.text
    if portal(url):return []
    identity=exact_identity(record, text)
    same_domain=same_site(url,record.get('website',''))
    # A supplied URL is a candidate, not proof. Page identity must verify it.
    if not identity:return []
    single=exact_identity(record,' '.join(page.headings)) and not re.search(r'unsere\s+(?:hotels|standorte)|our\s+hotels', ' '.join(page.headings),re.I)
    ats=bool(linked_from and same_site(linked_from,record.get('website','')) and any(host(url).endswith('.'+d) or host(url)==d for d in ATS))
    if not same_domain and not ats:return []
    local_training = text if single else '\n'.join(text[max(0,m.start()-300):m.end()+300] for m in TRAINING.finditer(text) if exact_identity(record,text[max(0,m.start()-300):m.end()+300]))
    found=professions(local_training)
    if re.search(r'stelle.{0,30}(?:besetzt|vergeben)|bewerbungsfrist.{0,30}abgelaufen', local_training, re.I):
        found=[]
    record['training_evidence']=[e for e in record.get('training_evidence',[]) if e.get('url')!=url and now-e.get('verified_at',0)<=90*86400]
    if found:
        explicit=bool(re.search(r'(?:ausbildung|auszubild|ausbildungsbeginn|start|beginn)[^\n.]{0,100}\b2027\b|\b2027\b[^\n.]{0,100}(?:ausbildung|auszubild)',local_training,re.I))
        record.setdefault('training_evidence',[]).append({'url':url,'professions':found,'status':'EXPLICIT_2027' if explicit else 'CURRENT_AUSBILDUNG_NO_2027_DATE','verified_at':now,'hotel_identity':record['collection_id']})
        record['profession']='; '.join(dict.fromkeys([p.strip() for p in record.get('profession','').split(';') if p.strip()]+found))
    published=set(e.lower() for e in EMAIL.findall(text))
    for href,_ in page.links:
        if href.lower().startswith('mailto:'):published.update(e.lower() for e in EMAIL.findall(unquote(href[7:].split('?')[0])))
    accepted=[]
    for email in sorted(published):
        score=role(email)
        if score<0:continue
        # Multi-property pages require local recipient + exact property + application context.
        contexts=[text[max(0,m.start()-350):m.end()+350] for m in re.finditer(re.escape(email),text,re.I)]
        chain_ok=any(exact_identity(record,c) and re.search(r'bewerb|ausbildung|personal|recruit|kontakt',c,re.I) for c in contexts)
        if not single and not chain_ok:continue
        # Footer suppliers are not hotel recipients. Cross-domain addresses require explicit
        # application/contact context identifying this property, even on a single-hotel page.
        foreign=not same_site('https://'+email.rsplit('@',1)[1],record.get('website',''))
        bad_context=any(re.search(r'(?:webdesign|agentur|website by|webmaster|datenschutzbeauftrag)',c,re.I) for c in contexts)
        application=any(exact_identity(record,c) and re.search(r'bewerb|ausbildung|personal|recruit',c,re.I) for c in contexts)
        if bad_context or (foreign and not application):continue
        ev={'email':email,'url':url,'source_type':'official_ats' if ats else 'official_employer','hotel_identity':record['collection_id'],
            'verified_at':now,'confidence':'high','scope':'hotel_specific' if single else 'approved_chain_recruiting','role_score':score,
            'exact_published':True,'location_evidence':True,'policy_version':4}
        accepted.append(ev)
    record['email_evidence']=[e for e in record.get('email_evidence',[]) if e.get('url')!=url and now-e.get('verified_at',0)<=90*86400]
    known=record['email_evidence']
    for ev in accepted:
        known[:]=[old for old in known if (old['email'],old['url'])!=(ev['email'],ev['url'])]
        known.append(ev)
    if accepted:record['identity_verified']=True
    return accepted

def best_email(record, now=None):
    now=time.time() if now is None else now
    aliases=set(record.get('aliases',[])+[record.get('collection_id')])
    valid=[e for e in record.get('email_evidence',[]) if e.get('policy_version')==4 and e.get('exact_published') is True and EMAIL.fullmatch(e.get('email',''))
           and e.get('source_type') in ('official_employer','official_ats') and e.get('hotel_identity') in aliases and e.get('location_evidence')
           and e.get('url','').startswith('https://') and 0<=now-e.get('verified_at',0)<=90*86400 and role(e['email'])>=0]
    if not valid:return None
    # Prefer hotel-specific evidence. Never downgrade an existing verified choice.
    specific=[e for e in valid if e.get('scope')=='hotel_specific']
    options=specific or valid
    winner=max(options,key=lambda e:(role(e['email']),e['verified_at']))
    old=next((e for e in options if e['email']==record.get('selected_email')),None)
    if old and role(old['email'])>=role(winner['email']):return old
    return winner

def publishable(record, now=None):
    now=time.time() if now is None else now
    training=[e for e in record.get('training_evidence',[]) if e.get('professions') and 0<=now-e.get('verified_at',0)<=90*86400]
    return bool(best_email(record,now) and training and not record.get('historical_match') and not record.get('history_risk'))

def quality(record):
    return (40*bool(record.get('training_evidence')) + 30*any(e.get('status')=='EXPLICIT_2027' for e in record.get('training_evidence',[]))
            + 25*bool(re.search(r'hotel|resort|hostel|gasthof',record.get('name',''),re.I))
            + 15*('Hotelfach' in record.get('profession','')) + 10*bool(record.get('city')))
