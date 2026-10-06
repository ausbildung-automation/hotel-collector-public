"""Read-only live source smoke check with aggregate-only output."""
import json
from pathlib import Path
from urllib.parse import urlsplit

from collector import CourteousHTTP, Deferred, SourceError, blank, parse_source

config = json.loads(Path('config.json').read_text())
sources = config['sources']

# Exercise every adapter plus a few additional direct employers without turning CI into a crawler.
selected = []
seen_adapters = set()
for source in sources:
    adapter = source.get('adapter')
    if adapter not in seen_adapters:
        selected.append(source)
        seen_adapters.add(adapter)
for source in sources:
    if len(selected) >= 6:
        break
    if source not in selected:
        selected.append(source)

state = blank()
client = CourteousHTTP(
    state,
    [urlsplit(s['url']).hostname for s in selected],
    max_requests=16,
    max_requests_per_host=3,
)
report = {'tested_sources': 0, 'parsed_sources': 0, 'deferred_sources': 0, 'parsed_leads': 0, 'requests': 0, 'adapters_ok': {}}
for source in selected:
    report['tested_sources'] += 1
    try:
        html = client.get(source['url'], refresh_hours=1)
        leads = parse_source(source, html)
        if not leads:
            raise SourceError('PARSER_EMPTY')
        report['parsed_sources'] += 1
        report['parsed_leads'] += len(leads)
        adapter = source.get('adapter', 'unknown')
        report['adapters_ok'][adapter] = report['adapters_ok'].get(adapter, 0) + 1
    except (Deferred, SourceError):
        report['deferred_sources'] += 1
report['requests'] = client.requests
print(json.dumps(report, sort_keys=True))
if report['parsed_sources'] == 0:
    raise SystemExit(1)
