"""Courteous multi-source hotel Ausbildung discovery with persistent deduplication."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import random
import re
import time
import unicodedata
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib import error, parse, request, robotparser

BOT = 'HotelDirectoryCollector/2.0 (+courteous public directory collector)'
EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
HOTEL_WORDS = re.compile(r'\b(hotel|gasthof|resort|hostel|pension|brauerei|landhotel|wirtshaus|klinik|hof|gmbh|kg|ohg)\b', re.I)


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


class DirectoryParser(HTMLParser):
    """DEHOGA-style h3 cards; never borrow an email from a neighbouring card."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards = []
        self.card = None
        self.heading = False
        self.skip = 0
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style'}:
            self.skip += 1
        if tag == 'h3':
            if self.card:
                self.cards.append(self.card)
            self.card = {'name': '', 'text': [], 'links': []}
            self.heading = True
        if tag == 'a' and self.card:
            self.anchor = [attrs.get('href', ''), '']

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.skip = max(0, self.skip - 1)
        if tag == 'h3':
            self.heading = False
        if tag == 'a' and self.anchor and self.card:
            self.card['links'].append(self.anchor)
            self.anchor = None

    def handle_data(self, data):
        if self.skip or not self.card:
            return
        if self.heading:
            self.card['name'] += data
        else:
            self.card['text'].append(data)
        if self.anchor:
            self.anchor[1] += data

    def leads(self, source):
        cards = self.cards + ([self.card] if self.card else [])
        for card in cards:
            text = ' '.join(card['text'])
            if not re.search(r'Hotelfach(?:frau|mann|leute)', text, re.I):
                continue
            websites = []
            for href, label in card['links']:
                if norm(label) not in {'website', 'webseite', 'homepage'}:
                    continue
                joined = parse.urljoin(source, href)
                try:
                    websites.append(canonical(joined))
                except ValueError:
                    pass
            websites = sorted(set(websites))
            emails = EMAIL.findall(text)
            for href, _ in card['links']:
                if href.lower().startswith('mailto:'):
                    emails += EMAIL.findall(parse.unquote(href[7:].split('?')[0]))
            if not card['name'].strip() or (not websites and not emails):
                continue
            city_match = re.search(r'\b\d{5}\s+([^\n]+?)\s+(?:Ausbildungsberuf|Ansprechpartner)', text)
            yield {
                'name': card['name'].strip(),
                'city': city_match.group(1).strip() if city_match else '',
                'website': websites[0] if websites else '',
                'emails': sorted(set(emails)),
                'source': source,
                'evidence': 'Directory lists Hotelfach training; current intake and email acceptance require review',
                'profession': 'Hotelfachmann/-frau',
                'last_seen': time.time(),
            }


class TextCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.skip += 1
        if not self.skip and tag in {'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'br*KˆÙ[‹œ\Ë˜\[™
	×‰ÊB‚ˆYˆ[™WÙ[™YÊÙ[‹YÊN‚ˆYˆYÈ[ˆÉÜØÜš\	Ë	Üİ[IßN‚ˆÙ[‹œÚÚ\HX^
Ù[‹œÚÚ\HJBˆ[Yˆ›İÙ[‹œÚÚ\[™YÈ[ˆÉÜ	Ë	Ù]‰Ë	ÛIË	ÚIË	Ú‰Ë	ÚÉË	Ú	Ë	ÜÙXİ[Û‰Ë	Ø\XÛIßN‚ˆÙ[‹œ\Ë˜\[™
	×‰ÊB‚ˆYˆ[™WÙ]JÙ[‹]JN‚ˆYˆ›İÙ[‹œÚÚ\‚ˆÙ[‹œ\Ë˜\[™
]JB‚ˆYˆ^
Ù[ŠN‚ˆ™]\›ˆ	×‰Ëš›Ú[Š[™Kœİš\

H›Üˆ[™H[ˆ	ÉËš›Ú[ŠÙ[‹œ\ÊKœÜ][™\Ê
HYˆ[™Kœİš\

JB‚‚™Yˆ\™XİÛXYÊÛİ\˜ÙK[
N‚ˆ\œÙ\ˆH^ÛÛXİÜŠ
Bˆ\œÙ\‹™™YY
[
Bˆ^H\œÙ\‹^

Bˆ›ÛYH	È	Ëš›Ú[Š^œÜ]

JK˜Ø\ÙY›Û

Bˆ™\]Z\™YHÛİ\˜ÙK™Ù]
	Ü™\]Z\™YÜ˜\Ù\ÉË×JBˆYˆ›İ™\]Z\™YÜˆ[J	È	Ëš›Ú[ŠİŠ˜\ÙJKœÜ]

JK˜Ø\ÙY›Û

H›İ[ˆ›ÛY›Üˆ˜\ÙH[ˆ™\]Z\™Y
N‚ˆ˜Z\ÙHÛİ\˜ÙQ\œ›ÜŠ	Ô‘TURT‘QÕVÓRTÔÒS‘ÉÊBˆ^ÛYYHÛİ\˜ÙK™Ù]
	Ù^ÛYYÜ˜\Ù\ÉË×JBˆYˆ[J	È	Ëš›Ú[ŠİŠ˜\ÙJKœÜ]

JK˜Ø\ÙY›Û

H[ˆ›ÛY›Üˆ˜\ÙH[ˆ^ÛYY
N‚ˆ˜Z\ÙHÛİ\˜ÙQ\œ›ÜŠ	ÑVÓQQÕVÔ‘TÑS•	ÊBˆ[XZ[ÈHÜİŠJKœİš\

K›İÙ\Š
H›ÜˆH[ˆÛİ\˜ÙK™Ù]
	Ù[XZ[ÉË×JHYˆSPRS™[X]Ú
İŠJKœİš\

NWBˆYˆ›İ[XZ[È[™Ûİ\˜ÙK™Ù]
	Ø[İ×ÜYÙWÙ[XZ[ÉÊN‚ˆ[XZ[ÈHÛÜY
Ù]
SPRS™š[™[
^
JJBˆ]šY[˜ÙHHÛİ\˜ÙK™Ù]
	Ù]šY[˜ÙIÊHÜˆ	Ñ\™Xİ[\ŞY\ˆYÙHY™\\Ù\Èİ[˜XÚ˜Z[š[™ÎÈ[ZÙH]H™\]Z\™\È™]šY]ÉÂˆZY[Âˆ	Û˜[YIÎˆÛİ\˜ÙVÉÛ˜[YI×Kˆ	ØÚ]IÎˆÛİ\˜ÙK™Ù]
	ØÚ]IË	ÉÊKˆ	İÙXœÚ]IÎˆØ[›ÛšXØ[
Ûİ\˜ÙK™Ù]
	İÙXœÚ]IÊHÜˆÛİ\˜ÙVÉİ\›	×JKˆ	Ù[XZ[ÉÎˆ[XZ[Ëˆ	ÜÛİ\˜ÙIÎˆÛİ\˜ÙVÉİ\›	×Kˆ	Ù]šY[˜ÙIÎˆ]šY[˜ÙKˆ	Ü›Ù™\ÜÚ[Û‰Îˆ	Òİ[˜XÚX[›‹ËYœ˜]IËˆ	Û\İÜÙY[‰Îˆ[YK[YJ
KˆB‚‚˜Û\ÜÈ]\Øš[[™ÒÛÛ\\ÜÔ\œÙ\ŠS\œÙ\ŠN‚ˆˆˆ™\İYY™›Ü\œÙ\ˆ›ÜˆHÛX[]\Øš[[™ÜÚÛÛ\\ÜÈİ[˜XÚ\İ[™ÈYÙKˆˆˆ‚ˆÒÒTÓS‘TÈHÂˆ	Ø]\Øš[[™ÉË	Ü˜ZİZİ[IË	Ù™\šY[š›ØˆÈØÚ0ï\š›Ø‰Ë	Ş\Ø[[Y[˜\˜™Z]Z]™YÚ[Û˜[[ˆØÚ[[‰Ëˆ	Ù°íœ™\™°éYÙ\ˆ[\™Ü[™	Ë	Ø]\Øš[[™È[ˆZ[™Z]	Ë	Ü˜ZİZİ[H°ïˆZšÜ°éIË	Ù]Z[ÉË	Ú™]™]Ù\˜™[‰ËˆB‚ˆYˆ×Ú[š]×ÊÙ[ŠN‚ˆİ\\Š
K—×Ú[š]×ÊÛÛ™\ØÚ\œ™YœÏUYJBˆÙ[‹œÙXİ[ÛœÈH×BˆÙ[‹˜İ\œ™[H›Û™BˆÙ[‹š[—ÚXY[™ÈH˜[ÙBˆÙ[‹œÚÚ\H‚ˆYˆ[™WÜİ\YÊÙ[‹YË]œÊN‚ˆYˆYÈ[ˆÉÜØÜš\	Ë	Üİ[IßN‚ˆÙ[‹œÚÚ\
ÏHBˆ™]\›‚ˆYˆYÈ[ˆÉÚÉË	Ú	ßN‚ˆYˆÙ[‹˜İ\œ™[‚ˆÙ[‹œÙXİ[ÛœË˜\[™
Ù[‹˜İ\œ™[
BˆÙ[‹˜İ\œ™[HÉÚXY[™ÉÎˆ	ÉË	Û[™\ÉÎˆ×_BˆÙ[‹š[—ÚXY[™ÈHYBˆ[YˆÙ[‹˜İ\œ™[[™YÈ[ˆÉÜ	Ë	ÛIË	Ù]‰Ë	Øœ‰Ë	ØIßN‚ˆÙ[‹˜İ\œ™[ÉÛ[™\É×K˜\[™
	×‰ÊB‚ˆYˆ[™WÙ[™YÊÙ[‹YÊN‚ˆYˆYÈ[ˆÉÜØÜš\	Ë	Üİ[IßN‚ˆÙ[‹œÚÚ\HX^
Ù[‹œÚÚ\HJBˆ[YˆYÈ[ˆÉÚÉË	Ú	ßN‚ˆÙ[‹š[—ÚXY[™ÈH˜[ÙBˆ[YˆÙ[‹˜İ\œ™[[™YÈ[ˆÉÜ	Ë	ÛIË	Ù]‰Ë	ØIßN‚ˆÙ[‹˜İ\œ™[ÉÛ[™\É×K˜\[™
	×‰ÊB‚ˆYˆ[™WÙ]JÙ[‹]JN‚ˆYˆÙ[‹œÚÚ\Üˆ›İÙ[‹˜İ\œ™[‚ˆ™]\›‚ˆYˆÙ[‹š[—ÚXY[™Î‚ˆÙ[‹˜İ\œ™[ÉÚXY[™É×H
ÏH]Bˆ[ÙN‚ˆÙ[‹˜İ\œ™[ÉÛ[™\É×K˜\[™
]JB‚ˆYˆÛÜÙJÙ[ŠN‚ˆİ\\Š
K˜ÛÜÙJ
BˆYˆÙ[‹˜İ\œ™[‚ˆÙ[‹œÙXİ[ÛœË˜\[™
Ù[‹˜İ\œ™[
BˆÙ[‹˜İ\œ™[H›Û™B‚ˆYˆXYÊÙ[‹Ûİ\˜ÙJN‚ˆÙ[‹˜ÛÜÙJ
Bˆ›ÜˆÙXİ[Ûˆ[ˆÙ[‹œÙXİ[ÛœÎ‚ˆYˆ	Úİ[˜XÚ	È›İ[ˆ›Ü›JÙXİ[Û–ÙÚXY[™É×JN‚ˆÛÛ[YBˆ[™\ÈHÜ™KœİXŠ‰×ÊÉË	È	Ë[™JKœİš\

H›Üˆ[™H[ˆ	ÉËš›Ú[ŠÙXİ[Û–ÉÛ[™\É×JKœÜ][™\Ê
WBˆ[™\ÈHÛ[™H›Üˆ[™H[ˆ[™\ÈYˆ[™WBˆYˆ›İ[J™KœÙX\˜Ú
‰×]\Øš[[™×ÊÌŒ×‰Ë[™K™K’JH›Üˆ[™H[ˆ[™\ÊN‚ˆÛÛ[YBˆØ[™Y]\ÈH×Bˆ›Üˆ[™H[ˆ[™\Î‚ˆİÈH[™K˜Ø\ÙY›Û

BˆYˆİÈ[ˆÙ[‹”ÒÒTÓS‘TÈÜˆ™K™[X]Ú
‰Ğ]\Øš[[™×ÊÌŒÌŸIË[™K™K’JN‚ˆÛÛ[YBˆYˆ™K™[X]Ú
‰×
×
×
OÉË[™JHÜˆ™KœÙX\˜Ú
‰×—Í_W‰Ë[™JN‚ˆÛÛ[YBˆYˆ[Š[™JHˆLŒ‚ˆÛÛ[YBˆYˆÕSÕÓÔ‘ËœÙX\˜Ú
[™JN‚ˆØ[™Y]\Ë˜\[™
[™JBˆYˆ›İØ[™Y]\Î‚ˆÛÛ[YBˆ˜[YHHØ[™Y]\ÖÌBˆÚ]HH	ÉÂˆ›Üˆ[™H[ˆ[™\Î‚ˆHH™K›X]Ú
‰×Í_WÊÊŠÊI	Ë[™JBˆYˆN‚ˆÚ]HHK™Ü›İ\
JKœİš\

Bˆœ™XZÂˆZY[Âˆ	Û˜[YIÎˆ˜[YKˆ	ØÚ]IÎˆÚ]Kˆ	İÙXœÚ]IÎˆ	ÉËˆ	Ù[XZ[ÉÎˆ×Kˆ	ÜÛİ\˜ÙIÎˆÛİ\˜ÙVÉİ\›	×Kˆ	Ù]šY[˜ÙIÎˆ	ÓšXÚH]\Øš[[™È\™XİÜH\İÈİ[˜XÚX[›‹ËYœ˜]H]\Øš[[™ÈŒÎÈ[\ŞY\ˆ[XZ[™\]Z\™\È™]šY]ÉËˆ	Ü›Ù™\ÜÚ[Û‰Îˆ	Òİ[˜XÚX[›‹ËYœ˜]IËˆ	Û\İÜÙY[‰Îˆ[YK[YJ
KˆB‚‚˜Û\ÜÈY™\œ™Y
^Ù\[ÛŠN‚ˆ\ÜÂ‚‚˜Û\ÜÈÛİ\˜ÙQ\œ›ÜŠ^Ù\[ÛŠN‚ˆ\ÜÂ‚‚˜Û\ÜÈ›Ô™Y\™Xİ
™\]Y\İ’™Y\™Xİ[™\ŠN‚ˆYˆ™Y\™XİÜ™\]Y\İ
Ù[‹
˜\™ÜË
ŠšİØ\™ÜÊN‚ˆ™]\›ˆ›Û™B‚‚˜Û\ÜÈÛİ\[İ\Ò‚ˆYˆ×Ú[š]×ÊÙ[‹İ]K[İÙYÚÜİËÜ[™\S›Û™KÛØÚÏ][YK[YKÛY\\][YKœÛY\›™Ï\˜[™ÛK[šY›Ü›KX^Ü™\]Y\İÏLX^Ü™\]Y\İ×Ü\—ÚÜİM
N‚ˆÙ[‹œİ]HHİ]BˆÙ[‹˜[İÙYHÙ]
[İÙYÚÜİÊBˆÙ[‹›Ü[™\ˆHÜ[™\ˆÜˆ™\]Y\İ˜Z[ÛÜ[™\Š›Ô™Y\™Xİ

JK›Ü[‚ˆÙ[‹˜ÛØÚÈHÛØÚÂˆÙ[‹œÛY\HÛY\\‚ˆÙ[‹œ›™ÈH›™ÂˆÙ[‹œ™\]Y\İÈHˆÙ[‹›X^Ü™\]Y\İÈHX^Ü™\]Y\İÂˆÙ[‹›X^Ü™\]Y\İ×Ü\—ÚÜİHX^Ü™\]Y\İ×Ü\—ÚÜİˆÙ[‹šÜİÜ™\]Y\İÈHßB‚ˆYˆÜİ
Ù[‹\›
N‚ˆØ[›ÛšXØ[
\›
BˆÜİH\œÙK\›Ü]
\›
KšÜİ˜[YK›İÙ\Š
BˆYˆÜİ›İ[ˆÙ[‹˜[İÙY‚ˆ˜Z\ÙHY™\œ™Y
	ÔÓÕTÑWÓ“ÕĞSÕÓTÕQ	ÊBˆ™]\›ˆÜİ‚ˆYˆ]\ÙJÙ[‹\›İ]\ËXY\œÏS›Û™JN‚ˆÜİHÙ[‹šÜİ
\›
BˆÈHÙ[‹œİ]VÉÚÜİÉ×KœÙ]Y˜][
ÜİßJBˆ˜Z[\™\ÈHË™Ù]
	Ù˜Z[\™\ÉË
H
ÈBˆÖÉÙ˜Z[\™\É×HH˜Z[\™\ÂˆÙXÛÛ™ÈHZ[ŠÈ
ˆL
ˆˆ
ŠˆZ[Š˜Z[\™\ÈHKJJBˆYˆİ]\È[ˆ
KÊN‚ˆÙXÛÛ™ÈHX^
ÙXÛÛ™Ë
Bˆ˜[YHH
XY\œÈÜˆßJK™Ù]
	Ô™]KPY\‰Ë	ÉÊBˆYˆ˜[YN‚ˆN‚ˆÙXÛÛ™ÈHX^
ÙXÛÛ™Ë›Ø]
˜[YJJBˆ^Ù\˜[YQ\œ›Ü‚ˆN‚ˆÙXÛÛ™ÈHX^
ÙXÛÛ™Ë\œÙY]Wİ×Ù]][YJ˜[YJK[Y\İ[\

HHÙ[‹˜ÛØÚÊ
JBˆ^Ù\
˜[YQ\œ›Ü‹\Q\œ›Ü‹İ™\™›İÑ\œ›ÜŠN‚ˆ\ÜÂˆÖÉİ[[	×HHÙ[‹˜ÛØÚÊ
H
ÈÙXÛÛ™ÂˆÖÉÛ\İÜİ]\É×HHİ]\Â‚ˆYˆ˜]ÊÙ[‹\›XY\œÏS›Û™JN‚ˆÜİHÙ[‹šÜİ
\›
BˆÈHÙ[‹œİ]VÉÚÜİÉ×KœÙ]Y˜][
ÜİßJBˆYˆË™Ù]
	İ[[	Ë
HˆÙ[‹˜ÛØÚÊ
N‚ˆ˜Z\ÙHY™\œ™Y
	ÒÔÕĞÓÓÓÕÓ‰ÊBˆYˆÙ[‹œ™\]Y\İÈHÙ[‹›X^Ü™\]Y\İÎ‚ˆ˜Z\ÙHY™\œ™Y
	Ô•S—Ô‘TUQTÕÓSRU	ÊBˆYˆÙ[‹šÜİÜ™\]Y\İË™Ù]
Üİ
HHÙ[‹›X^Ü™\]Y\İ×Ü\—ÚÜİ‚ˆ˜Z\ÙHY™\œ™Y
	ÒÔÕÔ‘TUQTÕÓSRU	ÊBˆ[^HHX^
MKË™Ù]
	Ù[^IËMJJH
ÈÙ[‹œ›™ÊL
Bˆ™[XZ[š[™ÈHË™Ù]
	Û\İÜ™\]Y\İ	Ë
H
È[^HHÙ[‹˜ÛØÚÊ
BˆYˆ™[XZ[š[™ÈˆŒ‚ˆ˜Z\ÙHY™\œ™Y
	ÓÓ‘×ĞÔUÓÑSVIÊBˆYˆ™[XZ[š[™Èˆ‚ˆÙ[‹œÛY\
™[XZ[š[™ÊBˆÙ[‹œ™\]Y\İÈ
ÏHBˆÙ[‹šÜİÜ™\]Y\İÖÚÜİHHÙ[‹šÜİÜ™\]Y\İË™Ù]
Üİ
H
ÈBˆÖÉÛ\İÜ™\]Y\İ	×HHÙ[‹˜ÛØÚÊ
BˆN‚ˆ™\HH™\]Y\İ”™\]Y\İ
\›XY\œÏ^ÉÕ\Ù\‹PYÙ[	Îˆ“Õ	ĞXØÙ\	Îˆ	İ^Ú[^ÜZ[ÜOLIË
ŠŠ˜Y\œÈÜˆßJ_JBˆÚ]Ù[‹›Ü[™\Š™\K[Y[İ]LJH\È™\ÜÛœÙN‚ˆ›ÙHH™\ÜÛœÙKœ™XY
ˆ
ˆL
ˆL
ÈJBˆYˆ[Š›ÙJHˆˆ
ˆL
ˆL‚ˆ˜Z\ÙHY™\œ™Y
	ÔQÑWÕÓ×ÓT‘ÑIÊBˆ™]\›ˆ›ÙK™\ÜÛœÙKšXY\œÂˆ^Ù\\œ›Ü‹’\œ›Üˆ\È^Î‚ˆYˆ^Ë˜ÛÙH[ˆ
ÌL
N‚ˆ˜Z\ÙBˆÙ[‹œ]\ÙJ\›^Ë˜ÛÙK^ËšXY\œÊBˆ˜Z\ÙHY™\œ™Y
	ÒÉÈ
ÈİŠ^Ë˜ÛÙJJHœ›ÛH›Û™Bˆ^Ù\
\œ›Ü‹•T“\œ›Ü‹[Y[İ]\œ›Ü‹ÔÑ\œ›ÜŠN‚ˆÙ[‹œ]\ÙJ\›
Bˆ˜Z\ÙHY™\œ™Y
	Ó‘UÓÔ’×ĞÓÓÓÕÓ‰ÊHœ›ÛH›Û™B‚ˆYˆ›Ø›İÊÙ[‹\›
N‚ˆÜİHÙ[‹šÜİ
\›
BˆÈHÙ[‹œİ]VÉÚÜİÉ×KœÙ]Y˜][
ÜİßJBˆYˆË™Ù]
	Ü›Ø›İ×İ[[	Ë
HHÙ[‹˜ÛØÚÊ
N‚ˆ›Ø›İ×İ\›H\œÙK\›[œÜ]

	ÚÉËÜİ	ËÜ›Ø›İË	Ë	ÉË	ÉÊJBˆN‚ˆ›ÙKÈHÙ[‹œ˜]Ê›Ø›İ×İ\›
Bˆ^Ù\\œ›Ü‹’\œ›Üˆ\È^Î‚ˆYˆ^Ë˜ÛÙH[ˆ
L
N‚ˆ›ÙHH‰Õ\Ù\‹XYÙ[ˆ
—‘\Ø[İÎ—‰Âˆ[ÙN‚ˆ˜Z\ÙHY™\œ™Y
	Ô“Ğ“Õ×ÕSURSP“IÊHœ›ÛH›Û™BˆÖÉÜ›Ø›İÉ×HH›ÙK™XÛÙJ	İ]‹N	Ë	Ü™\XÙIÊVÎLLŒBˆÖÉÜ›Ø›İ×İ[[	×HHÙ[‹˜ÛØÚÊ
H
ÈˆœH›Ø›İ\œÙ\‹”›Ø›İš[T\œÙ\Š
Bˆœœ\œÙJÖÉÜ›Ø›İÉ×KœÜ][™\Ê
JBˆYˆ›İœ˜Ø[—Ù™]Ú
“Õ\›
N‚ˆ˜Z\ÙHY™\œ™Y
	Ô“Ğ“Õ×ÑTĞSÕÉÊBˆ˜]HHœœ™\]Y\İÜ˜]J“Õ
BˆÖÉÙ[^I×HHX^
MKœ˜Ü˜]ÛÙ[^J“Õ
HÜˆ˜]KœÙXÛÛ™ÈÈ˜]Kœ™\]Y\İÈYˆ˜]H[™˜]Kœ™\]Y\İÈ[ÙH
B‚ˆYˆÙ]
Ù[‹\›™Yœ™\ÚÚİ\œÏMÌŠN‚ˆÙ[‹œ›Ø›İÊ\›
BˆYÙHHÙ[‹œİ]VÉÜYÙ\É×KœÙ]Y˜][
\›ßJBˆYˆYÙK™Ù]
	ØÚXÚÙYØ]	ÊH[™YÙVÉØÚXÚÙYØ]	×H
ÈX^
K™Yœ™\ÚÚİ\œÊH
ˆÍŒˆÙ[‹˜ÛØÚÊ
N‚ˆ˜Z\ÙHY™\œ™Y
	ĞĞPÒQÑ”‘TÒ	ÊBˆXY\œÈHßBˆYˆYÙK™Ù]
	Ù]YÉÊN‚ˆXY\œÖÉÒY‹S›Û™KSX]Ú	×HHYÙVÉÙ]YÉ×BˆYˆYÙK™Ù]
	Û[ÙYšYY	ÊN‚ˆXY\œÖÉÒY‹S[ÙYšYYTÚ[˜ÙI×HHYÙVÉÛ[ÙYšYY	×BˆN‚ˆ›ÙK™XÙZ]™YHÙ[‹œ˜]Ê\›XY\œÊBˆ^Ù\\œ›Ü‹’\œ›Üˆ\È^Î‚ˆYÙVÉØÚXÚÙYØ]	×HHÙ[‹˜ÛØÚÊ
BˆYˆ^Ë˜ÛÙHOHÌ‚ˆ˜Z\ÙHY™\œ™Y
	ÕSÒS‘ÑQ	ÊHœ›ÛH›Û™Bˆ˜Z\ÙHY™\œ™Y
	Ô‘SSÕ‘QÉÈ
ÈİŠ^Ë˜ÛÙJJHœ›ÛH›Û™Bˆ^H›ÙK™XÛÙJ	İ]‹N	Ë	Ü™\XÙIÊBˆYˆ™KœÙX\˜Ú
‰ÊÙ‹XÚ_Ë\™XØ\Ú_Ø\Ú_™\šYH[İH\™H[X[ŸXØÙ\ÜÈ[šYY
IË^™K’JN‚ˆÙ[‹œ]\ÙJ\›ÊBˆ˜Z\ÙHY™\œ™Y
	ĞÒSS‘ÑWÔÕÔ	ÊBˆYˆ	Ú[	È›İ[ˆ™XÙZ]™Y™Ù]
	ĞÛÛ[U\IË	İ^Ú[	ÊK›İÙ\Š
N‚ˆ˜Z\ÙHY™\œ™Y
	Ó“Ó—ÒS	ÊBˆYÙK\]JÚXÚÙYØ]\Ù[‹˜ÛØÚÊ
K]YÏ\™XÙZ]™Y™Ù]
	ÑUYÉË	ÉÊK[ÙYšYY\™XÙZ]™Y™Ù]
	Ó\İS[ÙYšYY	Ë	ÉÊJBˆÙ[‹œİ]VÉÚÜİÉ×VÜÙ[‹šÜİ
\›
WVÉÙ˜Z[\™\É×HHˆ™]\›ˆ^‚‚™Yˆ\œÙWÜÛİ\˜ÙJÛİ\˜ÙK[
N‚ˆY\\ˆHÛİ\˜ÙK™Ù]
	ØY\\‰ÊBˆYˆY\\ˆOH	ÙZÙØWÚÉÎ‚ˆ\œÙ\ˆH\™XİÜT\œÙ\Š
Bˆ\œÙ\‹™™YY
[
Bˆ™]\›ˆ\İ
\œÙ\‹›XYÊÛİ\˜ÙVÉİ\›	×JJBˆYˆY\\ˆOH	ÜÚ[™ÛWÛÜÜ[š]IÎ‚ˆ™]\›ˆ\İ
\™XİÛXYÊÛİ\˜ÙK[
JBˆYˆY\\ˆOH	Ø]\Øš[[™ÜÚÛÛ\\ÜÉÎ‚ˆ\œÙ\ˆH]\Øš[[™ÒÛÛ\\ÜÔ\œÙ\Š
Bˆ\œÙ\‹™™YY
[
Bˆ™]\›ˆ\İ
\œÙ\‹›XYÊÛİ\˜ÙJJBˆ˜Z\ÙHÛİ\˜ÙQ\œ›ÜŠ	ÕS’Ó“ÕÓ—ĞQTT‰ÊB‚‚™Yˆ[™[™×Ùš[™Ù\œš[
XY
N‚ˆ™]\›ˆYÙ\İ
›Ü›JXY™Ù]
	Û˜[YIË	ÉÊJH
È	ß	È
È›Ü›JXY™Ù]
	ØÚ]IË	ÉÊJH
È	ß	È
ÈİŠXY™Ù]
	İÙXœÚ]IË	ÉÊJKœİš\

K˜Ø\ÙY›Û

JB‚‚™Yˆ[œ]Y]YJİ]KXYËÛİ\˜ÙKÛÛ™šYÊN‚ˆ^\İ[™ÈHÜ[™[™×Ùš[™Ù\œš[
][JH›Üˆ][H[ˆİ]VÉÜ[™[™É×_BˆYYHˆ›İÈH[YK[YJ
Bˆ›ÜˆXY[ˆXYÎ‚ˆœH[™[™×Ùš[™Ù\œš[
XY
BˆYˆœ[ˆ^\İ[™Î‚ˆÛÛ[YBˆYˆ[Šİ]VÉÜ[™[™É×JHHÛÛ™šYË™Ù]
	ÛX^Ü[™[™ÉËL
N‚ˆœ™XZÂˆXYHÛÜK™Y\ÛÜJXY
BˆXYÉ×Üš[Üš]I×HH[
Ûİ\˜ÙK™Ù]
	Üš[Üš]IËŒ
JBˆXYÉ×Ü]Y]YYØ]	×HH›İÂˆİ]VÉÜ[™[™É×K˜\[™
XY
Bˆ^\İ[™Ë˜Y
œ
BˆYY
ÏHBˆİ]VÉÜ[™[™É×KœÛÜ
Ù^O[[X™Hˆ
[
™Ù]
	×Üš[Üš]IËL
JK›Ø]
™Ù]
	×Ü]Y]YYØ]	Ë
JK›Ü›J™Ù]
	Û˜[YIË	ÉÊJJJBˆ™]\›ˆYY‚‚™YˆÛİ\˜ÙWÜ™XYJİ]KÛİ\˜ÙK›İÊN‚ˆY]HHİ]VÉÜÛİ\˜ÙWÛY]I×KœÙ]Y˜][
Ûİ\˜ÙVÉİ\›	×KßJBˆ™]\›ˆY]K™Ù]
	Ü™]WØY\‰Ë
HH›İÂ‚‚™YˆÛİ\˜ÙWÜİXØÙ\ÜÊİ]KÛİ\˜ÙKÛİ[›İÊN‚ˆY]HHİ]VÉÜÛİ\˜ÙWÛY]I×KœÙ]Y˜][
Ûİ\˜ÙVÉİ\›	×KßJBˆY]K\]J\İÜİXØÙ\ÜÏ[›İË\İØÛİ[XÛİ[˜Z[\™\ÏL™]WØY\L\İÜ™X\ÛÛIÓÒÉÊB‚‚™YˆÛİ\˜ÙWÙY™\œ™Y
İ]KÛİ\˜ÙK™X\ÛÛ‹›İÊN‚ˆY]HHİ]VÉÜÛİ\˜ÙWÛY]I×KœÙ]Y˜][
Ûİ\˜ÙVÉİ\›	×KßJBˆ^XİYHÉĞĞPÒQÑ”‘TÒ	Ë	ÕSÒS‘ÑQ	Ë	ÒÔÕĞÓÓÓÕÓ‰Ë	Ô•S—Ô‘TUQTÕÓSRU	Ë	ÒÔÕÔ‘TUQTÕÓSRU	ßBˆYˆ™X\ÛÛˆ[ˆ^XİY‚ˆY]VÉÛ\İÜ™X\ÛÛ‰×HH™X\ÛÛ‚ˆ™]\›‚ˆ˜Z[\™\ÈH[
Y]K™Ù]
	Ù˜Z[\™\ÉË
JH
ÈBˆY]VÉÙ˜Z[\™\É×HH˜Z[\™\ÂˆYˆ™X\ÛÛˆ[ˆÉÔ“Ğ“Õ×ÑTĞSÕÉË	Ô‘SSÕ‘QÍ	Ë	Ô‘SSÕ‘QÍL	ßN‚ˆ[^HHÈ
ˆˆ[Yˆ™X\ÛÛˆ[ˆÉĞÒSS‘ÑWÔÕÔ	Ë	ÒÍIË	ÒÍÉßN‚ˆ[^HHˆ[ÙN‚ˆ[^HHZ[ŠÈ
ˆÈ
ˆÍŒ
ˆ
ˆ
ŠˆZ[Š˜Z[\™\ÈHKJJJBˆY]VÉÜ™]WØY\‰×HH›İÈ
È[^BˆY]VÉÛ\İÜ™X\ÛÛ‰×HH™X\ÛÛ‚‚‚™Yˆ[Šİ]KÛÛ™šYËÛY[
N‚ˆ™\ÜHÉÛ™]ÉÎˆ	Ø[™XYWØÛÛXİY	Îˆ	Ú[˜[Y	Îˆ	ÜÛİ\˜Ù\×ÛÚÉÎˆ	ÜÛİ\˜Ù\×ÙY™\œ™Y	Îˆ	Ü™\]Y\İÉÎˆBˆYÙ]H[
ÛÛ™šYË™Ù]
	ÛX^Û™]×Ü\—Ü[‰ËL
JBˆ›İÈHÛY[˜ÛØÚÊ
B‚ˆ›ÜˆÛİ\˜ÙH[ˆÛÜY
ÛÛ™šYÖÉÜÛİ\˜Ù\É×KÙ^O[[X™HÎˆ
[
Ë™Ù]
	Üš[Üš]IËŒ
JKÖÉİ\›	×JJN‚ˆYˆÛY[œ™\]Y\İÈHÛY[›X^Ü™\]Y\İÎ‚ˆœ™XZÂˆYˆ›İÛİ\˜ÙWÜ™XYJİ]KÛİ\˜ÙK›İÊN‚ˆ™\ÜÉÜÛİ\˜Ù\×ÙY™\œ™Y	×H
ÏHBˆÛÛ[YBˆN‚ˆ[HÛY[™Ù]
Ûİ\˜ÙVÉİ\›	×K™Yœ™\ÚÚİ\œÏZ[
Ûİ\˜ÙK™Ù]
	Ü™Yœ™\ÚÚİ\œÉË
JJBˆXYÈH\œÙWÜÛİ\˜ÙJÛİ\˜ÙK[
BˆYˆ›İXYÎ‚ˆ˜Z\ÙHÛİ\˜ÙQ\œ›ÜŠ	ÔT”ÑT—ÑSTIÊBˆ[œ]Y]YJİ]KXYËÛİ\˜ÙKÛÛ™šYÊBˆÛİ\˜ÙWÜİXØÙ\ÜÊİ]KÛİ\˜ÙK[ŠXYÊK›İÊBˆ™\ÜÉÜÛİ\˜Ù\×ÛÚÉ×H
ÏHBˆ^Ù\Y™\œ™Y\ÈİÜ‚ˆÛİ\˜ÙWÙY™\œ™Y
İ]KÛİ\˜ÙKİŠİÜ
K›İÊBˆ™\ÜÉÜÛİ\˜Ù\×ÙY™\œ™Y	×H
ÏHBˆYˆİŠİÜ
HOH	Ô•S—Ô‘TUQTÕÓSRU	Î‚ˆœ™XZÂˆ^Ù\Ûİ\˜ÙQ\œ›Üˆ\ÈİÜ‚ˆÛİ\˜ÙWÙY™\œ™Y
İ]KÛİ\˜ÙKİŠİÜ
K›İÊBˆ™\ÜÉÜÛİ\˜Ù\×ÙY™\œ™Y	×H
ÏHB‚ˆ›ØÙ\ÜÙYHˆX^Ü›ØÙ\ÜÙYH[
ÛÛ™šYË™Ù]
	ÛX^Ü›ØÙ\ÜÙYÜ\—Ü[‰Ë
JBˆÚ[Hİ]VÉÜ[™[™É×H[™™\ÜÉÛ™]É×HYÙ][™›ØÙ\ÜÙYX^Ü›ØÙ\ÜÙY‚ˆØ[™Y]HHİ]VÉÜ[™[™É×KœÜ

Bˆ™\İ[HY
İ]KØ[™Y]JBˆ›ØÙ\ÜÙY
ÏHBˆYˆ™\İ[OH	Ó‘UÉÎ‚ˆ™\ÜÉÛ™]É×H
ÏHBˆ[Yˆ™\İ[OH	ÒS•SQ	Î‚ˆ™\ÜÉÚ[˜[Y	×H
ÏHBˆ[ÙN‚ˆ™\ÜÉØ[™XYWØÛÛXİY	×H
ÏHB‚ˆ™\ÜÉÜ™\]Y\İÉ×HHÛY[œ™\]Y\İÂˆ™\ÜÉÜ™[XZ[š[™É×HH[Šİ]VÉÜ[™[™É×JBˆ™\ÜÉÜ›ØÙ\ÜÙY	×HH›ØÙ\ÜÙYˆ™]\›ˆ™\Ü‚‚™YˆXZ[Š
N‚ˆ\œÙ\ˆH\™Ü\œÙK\™İ[Y[\œÙ\Š
Bˆ\œÙ\‹˜YØ\™İ[Y[
	ËKXÛÛ™šYÉËY˜][IØÛÛ™šYËšœÛÛ‰ÊBˆ\œÙ\‹˜YØ\™İ[Y[
	ËK\İ]IËY˜][IÜİ]KšœÛÛ‰ÊBˆ\œÙ\‹˜YØ\™İ[Y[
	ËK\\œÚ\İ	ËXİ[ÛIÜİÜ™WİYIÊBˆ\œÙ\‹˜YØ\™İ[Y[
	ËK\™[[İIËXİ[ÛIÜİÜ™WİYIÊBˆ\™ÜÈH\œÙ\‹œ\œÙWØ\™ÜÊ
BˆÛÛ™šYÈHœÛÛ‹›ØYÊ]
\™ÜË˜ÛÛ™šYÊKœ™XYİ^

JBˆ˜XÚÙ[™H›Û™BˆYˆ\™ÜËœ™[[İN‚ˆœ›ÛHÚ]X—Üİ]H[\ÜÚ]X”İ]Bˆ˜XÚÙ[™HÚ]X”İ]JÛÛ™šYÖÉÜš]˜]WÜ™\É×KÛÛ™šYÖÉÜİ]WØœ˜[˜Ú	×KÜË™Ù][Š	Ô’UUWĞÓÓPÕÔ—ÕÒÑS‰Ë	ÉÊJBˆİ]KÚHH˜XÚÙ[™œ™XY
ÛÛ™šYÖÉÜİ]WÜ]	×JBˆ[ÙN‚ˆİ]HHœÛÛ‹›ØYÊ]
\™ÜËœİ]JKœ™XYİ^

JBˆ˜[Y]Jİ]JBˆYˆ\™ÜËœ™[[İH[™›İ\™ÜËœ\œÚ\İ‚ˆ˜Z\ÙH˜[YQ\œ›ÜŠ	Ô™[[İH[ˆ]\İ\œÚ\İÛÛÛİÛœÈ[™İ\œÛÜ‰ÊBˆ[İÙYÚÜİÈHÜ\œÙK\›Ü]
ÖÉİ\›	×JKšÜİ˜[YH›ÜˆÈ[ˆÛÛ™šYÖÉÜÛİ\˜Ù\É×WBˆÛY[HÛİ\[İ\Ò
ˆİ]Kˆ[İÙYÚÜİËˆX^Ü™\]Y\İÏZ[
ÛÛ™šYË™Ù]
	ÛX^Ü™\]Y\İ×Ü\—Ü[‰Ë
JKˆX^Ü™\]Y\İ×Ü\—ÚÜİZ[
ÛÛ™šYË™Ù]
	ÛX^Ü™\]Y\İ×Ü\—ÚÜİÜ\—Ü[‰Ë
JKˆ
BˆN‚ˆ™\ÜH[Šİ]KÛÛ™šYËÛY[
Bˆš[˜[N‚ˆYˆ\™ÜËœ\œÚ\İ‚ˆYˆ˜XÚÙ[™‚ˆ˜XÚÙ[™Üš]JÛÛ™šYÖÉÜİ]WÜ]	×Kİ]KÚJBˆ[ÙN‚ˆ]H]
\™ÜËœİ]JBˆ\H]Ú]ÜİY™š^
	Ë\	ÊBˆ\Üš]Wİ^
œÛÛ‹™[\Êİ]K[œİ\™WØ\ØÚZOQ˜[ÙJJBˆ\œ™\XÙJ]
Bˆš[
œÛÛ‹™[\Ê™\Ü
JB‚‚šYˆ×Û˜[YW×ÈOH	××ÛXZ[—×ÉÎ‚ˆN‚ˆXZ[Š
Bˆ^Ù\^Ù\[Ûˆ\È^Î‚ˆš[
	ÔÕÔQˆ	È
È\J^ÊK—×Û˜[YW×È
È	ÎÈ›È[XZ[[]™\H\È\Ùˆ\ÈÛÛXİÜ‰ÊBˆ˜Z\ÙHŞ\İ[Q^]
ŠB