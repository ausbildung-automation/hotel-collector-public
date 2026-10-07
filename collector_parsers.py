import re
import time
from html.parser import HTMLParser
from urllib import parse
from collector_common import EMAIL, HOTEL_WORDS, SourceError, canonical, norm
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
            from verification import professions
            if not re.search(r'Hotelfach(?:frau|mann|leute)', text, re.I) and not professions(text):
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
                'profession': '; '.join(__import__('verification').professions(text)) or 'Hotelfachmann/-frau',
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
        if not self.skip and tag in {'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'br', 'section', 'article'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in {'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'section', 'article'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        return '\n'.join(line.strip() for line in ''.join(self.parts).splitlines() if line.strip())


def direct_leads(source, html):
    parser = TextCollector()
    parser.feed(html)
    text = parser.text()
    folded = ' '.join(text.split()).casefold()
    required = source.get('required_phrases', [])
    if not required or any(' '.join(str(phrase).split()).casefold() not in folded for phrase in required):
        raise SourceError('REQUIRED_TEXT_MISSING')
    excluded = source.get('excluded_phrases', [])
    if any(' '.join(str(phrase).split()).casefold() in folded for phrase in excluded):
        raise SourceError('EXCLUDED_TEXT_PRESENT')
    # Configured addresses are hints only: exact publication on this page is mandatory.
    emails = sorted(set(e.lower() for e in EMAIL.findall(text)))
    evidence = source.get('evidence') or 'Direct employer page advertises Hotelfach training; intake date requires review'
    yield {
        'name': source['name'],
        'city': source.get('city', ''),
        'website': canonical(source.get('website') or source['url']),
        'emails': emails,
        'source': source['url'],
        'evidence': evidence,
        'profession': '; '.join(__import__('verification').professions(text)) or 'Hotelfachmann/-frau',
        'last_seen': time.time(),
    }


class AusbildungKompassParser(HTMLParser):
    """Best-effort parser for the small Ausbildungskompass Hotelfach listing page."""
    SKIP_LINES = {
        'ausbildung', 'praktikum', 'ferienjob / schülerjob', 'zusammenarbeit mit regionalen schulen',
        'förderfähiger hintergrund', 'ausbildung in teilzeit', 'praktikum für lehrkräfte', 'details', 'jetzt bewerben',
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections = []
        self.current = None
        self.in_heading = False
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.skip += 1
            return
        if tag in {'h3', 'h4'}:
            if self.current:
                self.sections.append(self.current)
            self.current = {'heading': '', 'lines': []}
            self.in_heading = True
        elif self.current and tag in {'p', 'li', 'div', 'br', 'a'}:
            self.current['lines'].append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.skip = max(0, self.skip - 1)
        elif tag in {'h3', 'h4'}:
            self.in_heading = False
        elif self.current and tag in {'p', 'li', 'div','a'}:
            self.current['lines'].append('\n')

    def handle_data(self, data):
        if self.skip or not self.current:
            return
        if self.in_heading:
            self.current['heading'] += data
        else:
            self.current['lines'].append(data)

    def close(self):
        super().close()
        if self.current:
            self.sections.append(self.current)
            self.current = None

    def leads(self, source):
        self.close()
        for section in self.sections:
            if 'hotelfach' not in norm(section['heading']):
                continue
            lines = [re.sub(r'\s+', ' ', line).strip() for line in ''.join(section['lines']).splitlines()]
            lines = [line for line in lines if line]
            if not any(re.search(r'\bAusbildung\s+2027\b', line, re.I) for line in lines):
                continue
            candidates = []
            for line in lines:
                low = line.casefold()
                if low in self.SKIP_LINES or re.fullmatch(r'Ausbildung\s+20\d{2}', line, re.I):
                    continue
                if re.fullmatch(r'\(?\d+\)?', line) or re.search(r'^\d{5}\b', line):
                    continue
                if len(line) > 120:
                    continue
                if HOTEL_WORDS.search(line):
                    candidates.append(line)
            if not candidates:
                continue
            name = candidates[0]
            city = ''
            for line in lines:
                m = re.match(r'\d{5}\s+(.+)$', line)
                if m:
                    city = m.group(1).strip()
                    break
            yield {
                'name': name,
                'city': city,
                'website': '',
                'emails': [],
                'source': source['url'],
                'evidence': 'Niche Ausbildung directory lists Hotelfachmann/-frau Ausbildung 2027; employer email requires review',
                'profession': 'Hotelfachmann/-frau',
                'last_seen': time.time(),
            }


