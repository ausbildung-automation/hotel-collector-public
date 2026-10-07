"""Location-aware identity matching. Shared recipients/domains are never identities."""
import copy
import re
from urllib.parse import urlsplit
from collector_common import norm, digest

GENERIC = set('hotel hotels resort spa wellness landhotel parkhotel gasthof restaurant post hof zur zum der die das am an im in und co gmbh kg ag mbh ohg best western plus'.split())
LEGAL = re.compile(r'\b(?:gmbh|co|kg|ag|mbh|ohg|hotelges|gesellschaft|egbr)\b')

def name(value):
    return ' '.join(LEGAL.sub(' ', norm(value)).split())

def tokens(value):
    return set(name(value).split()) - GENERIC

def host(url):
    try:
        return (urlsplit(url if '://' in url else 'https://' + url).hostname or '').lower().removeprefix('www.')
    except ValueError:
        return ''

def location_url(record):
    url = record.get('location_url', '')
    return url.rstrip('/').casefold() if url else ''

def same(a, b):
    """Require a location signal; generic names require an address/property URL."""
    for field in ('city', 'postcode', 'street'):
        x, y = norm(a.get(field, '')), norm(b.get(field, ''))
        if x and y and x != y:
            return False
    na, nb = name(a.get('name', '')), name(b.get('name', ''))
    if not na or not nb:
        return False
    ta, tb = tokens(na), tokens(nb)
    names = na == nb or (bool(ta and tb) and (ta <= tb or tb <= ta))
    address = bool(a.get('street') and b.get('street') and norm(a['street']) == norm(b['street']) and
                   ((a.get('postcode') and a.get('postcode') == b.get('postcode')) or
                    (a.get('city') and norm(a['city']) == norm(b.get('city', '')))))
    prop = bool(location_url(a) and location_url(a) == location_url(b))
    if not ta or not tb:
        return names and (address or prop)
    city = bool(a.get('city') and b.get('city') and norm(a['city']) == norm(b['city']))
    site = bool(host(a.get('website', '') or a.get('domain', '')) and
                host(a.get('website', '') or a.get('domain', '')) == host(b.get('website', '') or b.get('domain', '')))
    # Subset names need two corroborating signals. Equal distinctive names need one.
    return names and (address or prop or (na == nb and city) or (city and site))

def merge(a, b):
    """Preserve old identity, all aliases/evidence and never downgrade verified email."""
    out = copy.deepcopy(a)
    for field in ('city', 'postcode', 'street', 'website', 'location_url', 'name'):
        if not out.get(field) and b.get(field):
            out[field] = b[field]
    for field in ('emails', 'sources', 'aliases', 'evidence_urls', 'names'):
        values = list(out.get(field, [])) + list(b.get(field, []))
        if field == 'aliases':
            values += [x.get('collection_id') for x in (a, b)]
        if field == 'names':
            values += [x.get('name') for x in (a, b)]
        out[field] = list(dict.fromkeys(x for x in values if x))
    out['profession'] = '; '.join(dict.fromkeys(p.strip() for r in (a, b) for p in r.get('profession', '').split(';') if p.strip()))
    out['evidence'] = ' | '.join(dict.fromkeys(r.get('evidence', '') for r in (a, b) if r.get('evidence')))
    out['email_evidence'] = list(out.get('email_evidence', []))
    for ev in b.get('email_evidence', []):
        if ev not in out['email_evidence']:
            out['email_evidence'].append(copy.deepcopy(ev))
    out['training_evidence'] = list(out.get('training_evidence', []))
    for ev in b.get('training_evidence', []):
        if ev not in out['training_evidence']:
            out['training_evidence'].append(copy.deepcopy(ev))
    out['last_seen'] = max(a.get('last_seen', 0), b.get('last_seen', 0))
    for field in ('historical_match', 'history_risk'):
        if b.get(field): out[field] = b[field]
    return out

def canonicalize(state):
    entities = state.setdefault('entities', {})
    alias_index = state.setdefault('aliases', {})
    for section in ('records', 'review_candidates'):
        for key, raw in state.get(section, {}).items():
            if key in alias_index:
                continue
            record = copy.deepcopy(raw)
            record.setdefault('collection_id', key)
            record.setdefault('first_seen', record.get('last_seen', 0))
            record.setdefault('aliases', [key])
            matches = [cid for cid, other in entities.items() if same(record, other)]
            cid = matches[0] if len(matches) == 1 else key
            if cid in entities:
                entities[cid] = merge(entities[cid], record)
            else:
                entities[cid] = record
            for alias in entities[cid].get('aliases', []) + [key]:
                alias_index[alias] = cid
    return entities

def upsert(state, record):
    entities = state.setdefault('entities', {})
    record = copy.deepcopy(record)
    key = record.get('collection_id') or digest(name(record['name']) + '|' + norm(record.get('city', '')) + '|' + record.get('website', ''))
    aliases = state.setdefault('aliases', {})
    if key in aliases:
        cid = aliases[key]
    else:
        matches = [cid for cid, old in entities.items() if same(old, record)]
        cid = matches[0] if len(matches) == 1 else key
    new = cid not in entities
    record['collection_id'] = cid
    record.setdefault('aliases', [key])
    entities[cid] = record if new else merge(entities[cid], record)
    for alias in entities[cid].get('aliases', []) + [key]: aliases[alias] = cid
    return cid, new
