import copy
import hashlib
import re
import time
import unicodedata
from urllib import parse

BOT = 'HotelDirectoryCollector/2.0 (+courteous public directory collector)'
EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
HOTEL_WORDS = re.compile(r'\b(hotel|gasthof|resort|hostel|pension|brauerei|landhotel|wirtshaus|klinik|hof|gmbh|kg|ohg)\b', re.I)

class Deferred(Exception):
    pass

class SourceError(Exception):
    pass
def norm(value):
    value = re.sub(r'^(?:[rq]?\d+[_\s-]+)+', '', str(value), flags=re.I)
    value = unicodedata.normalize('NFKD', value.casefold().replace('&', ' und '))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in value if not unicodedata.combining(c))).split())


def digest(value):
    return hashlib.sha256(value.casefold().strip().encode()).hexdigest()


def canonical(url):
    p = parse.urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Only public HTTPS URLs allowed')
    host = p.hostname.lower().removeprefix('www.')
    if host in {'localhost', 'api.github.com'} or ':' in host or re.fullmatch(r'[0-9.]+', host):
        raise ValueError('Invalid source host')
    return parse.urlunsplit(('https', host, p.path.rstrip('/') or '/', p.query, ''))


def keys(lead):
    result = {'n:' + digest(norm(lead['name']))}
    if lead.get('city'):
        result.add('c:' + digest(norm(lead['name']) + '|' + norm(lead['city'])))
    for email in lead.get('emails', []):
        result.add('e:' + digest(email))
        result.add('p:' + digest(email)[:16])
    if lead.get('website'):
        url = canonical(lead['website'])
        result.add('u:' + digest(url))
        result.add('d:' + digest(parse.urlsplit(url).hostname))
    if lead.get('domain'):
        result.add('d:' + digest(lead['domain'].lower().removeprefix('www.')))
    return result


def blank():
    return {
        'version': 1,
        'history_keys': {},
        'records': {},
        'index': {},
        'hosts': {},
        'pages': {},
        'pending': [],
        'review_candidates': {},
        'source_meta': {},
    }


def validate(state):
    if state.get('version') != 1 or not all(isinstance(state.get(k), dict) for k in ('history_keys', 'records', 'index', 'hosts', 'pages')) or not isinstance(state.get('pending'), list):
        raise ValueError('Missing or malformed collection history; refusing empty fallback')
    state.setdefault('review_candidates', {})
    state.setdefault('source_meta', {})
    if not isinstance(state['review_candidates'], dict) or not isinstance(state['source_meta'], dict):
        raise ValueError('Malformed collection review/source metadata')
    return state


def clean_lead(lead):
    lead = {k: copy.deepcopy(v) for k, v in lead.items() if not str(k).startswith('_')}
    lead['name'] = str(lead.get('name', '')).strip()
    lead['city'] = str(lead.get('city', '')).strip()
    lead['website'] = str(lead.get('website', '')).strip()
    lead['source'] = str(lead.get('source', '')).strip()
    lead['emails'] = sorted({e.strip().lower() for e in lead.get('emails', []) if EMAIL.fullmatch(e.strip())})
    return lead


def add(state, lead, historical=False):
    lead = clean_lead(lead)
    if not norm(lead['name']):
        return 'INVALID'
    identity = keys(lead)
    if historical:
        for key in identity:
            state['history_keys'][key] = 'MASTER_COLLECTED'
        return 'IMPORTED'

    def review(match):
        candidate_id = digest(norm(lead['name']) + '|' + norm(lead.get('city', '')) + '|' + lead.get('website', ''))
        state['review_candidates'].setdefault(candidate_id, {**lead, 'status': 'ALREADY_COLLECTED_REVIEW', 'matched_key': match})
        return 'ALREADY_COLLECTED_REVIEW'

    # City-qualified name and property URL are strong; shared email/domain alone needs review.
    for key in sorted(identity):
        if key in state['history_keys']:
            return review(key)
        if key in state['index']:
            existing = state['records'][state['index'][key]]
            if key.startswith('c:') or (key.startswith('u:') and norm(existing['name']) == norm(lead['name'])) or (key.startswith('n:') and norm(existing.get('city', '')) == norm(lead.get('city', ''))):
                existing['last_seen'] = lead.get('last_seen', time.time())
                existing['sources'] = sorted(set(existing.get('sources', []) + [lead['source']]))
                return 'DUPLICATE'
            return review(key)

    key = digest(norm(lead['name']) + '|' + norm(lead.get('city', '')) + '|' + lead.get('website', ''))
    lead.update(status='NEEDS_REVIEW', sources=[lead['source']], first_seen=time.time(), last_seen=time.time(), collection_id=key)
    state['records'][key] = lead
    for identity_key in identity:
        state['index'][identity_key] = key
    return 'NEW'


