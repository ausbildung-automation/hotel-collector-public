"""One live directory check, aggregate-only output; no private history or writes."""
import json
from urllib.parse import urlsplit
from pathlib import Path
from collector import CourteousHTTP, Deferred, DirectoryParser, blank

config=json.loads(Path('config.json').read_text())
source=config['sources'][0]['url']
state=blank()
client=CourteousHTTP(state,[urlsplit(source).hostname],max_requests=3)
try:
    page=client.get(source)
except Deferred as stop:
    print(json.dumps({'live_source':'DEFERRED','reason':str(stop),'requests':client.requests}))
else:
    parser=DirectoryParser();parser.feed(page)
    leads=list(parser.leads(source))
    print(json.dumps({'live_source':'PARSED' if leads else 'PARSER_REVIEW_REQUIRED','training_cards':len(leads),'requests':client.requests}))
    if not leads: raise SystemExit(1)
