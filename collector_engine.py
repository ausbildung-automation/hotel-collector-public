import copy
import time
from collector_common import Deferred, SourceError, add, digest, norm
from collector_parsers import AusbildungKompassParser, DirectoryParser, direct_leads
def parse_source(source, html):
    adapter = source.get('adapter')
    if adapter == 'dehoga_h3':
        parser = DirectoryParser()
        parser.feed(html)
        return list(parser.leads(source['url']))
    if adapter == 'single_opportunity':
        return list(direct_leads(source, html))
    if adapter == 'ausbildungskompass':
        parser = AusbildungKompassParser()
        parser.feed(html)
        return list(parser.leads(source))
    raise SourceError('UNKNOWN_ADAPTER')


def pending_fingerprint(lead):
    return digest(norm(lead.get('name', '')) + '|' + norm(lead.get('city', '')) + '|' + str(lead.get('website', '')).strip().casefold())


def enqueue(state, leads, source, config):
    existing = {pending_fingerprint(item) for item in state['pending']}
    added = 0
    now = time.time()
    for lead in leads:
        fp = pending_fingerprint(lead)
        if fp in existing:
            continue
        if len(state['pending']) >= config.get('max_pending', 5000):
            break
        lead = copy.deepcopy(lead)
        lead['_priority'] = int(source.get('priority', 20))
        lead['_queued_at'] = now
        state['pending'].append(lead)
        existing.add(fp)
        added += 1
    state['pending'].sort(key=lambda x: (int(x.get('_priority', 50)), float(x.get('_queued_at', 0)), norm(x.get('name', ''))))
    return added


def source_ready(state, source, now):
    meta = state['source_meta'].setdefault(source['url'], {})
    return meta.get('retry_after', 0) <= now


def source_success(state, source, count, now):
    meta = state['source_meta'].setdefault(source['url'], {})
    meta.update(last_success=now, last_count=count, failures=0, retry_after=0, last_reason='OK')


def source_deferred(state, source, reason, now):
    meta = state['source_meta'].setdefault(source['url'], {})
    expected = {'CACHED_FRESH', 'UNCHANGED', 'HOST_COOLDOWN', 'RUN_REQUEST_LIMIT', 'HOST_REQUEST_LIMIT'}
    if reason in expected:
        meta['last_reason'] = reason
        return
    failures = int(meta.get('failures', 0)) + 1
    meta['failures'] = failures
    if reason in {'ROBOTS_DISALLOW', 'REMOVED_404', 'REMOVED_410'}:
        delay = 7 * 86400
    elif reason in {'CHALLENGE_STOP', 'HTTP_401', 'HTTP_403'}:
        delay = 86400
    else:
        delay = min(7 * 86400, 3 * 3600 * (2 ** min(failures - 1, 5)))
    meta['retry_after'] = now + delay
    meta['last_reason'] = reason


def run(state, config, client):
    report = {'new': 0, 'already_collected': 0, 'invalid': 0, 'sources_ok': 0, 'sources_deferred': 0, 'requests': 0}
    budget = int(config.get('max_new_per_run', 100))
    now = client.clock()

    for source in sorted(config['sources'], key=lambda s: (int(s.get('priority', 20)), s['url'])):
        if client.requests >= client.max_requests:
            break
        if not source_ready(state, source, now):
            report['sources_deferred'] += 1
            continue
        try:
            html = client.get(source['url'], refresh_hours=int(source.get('refresh_hours', 24)))
            leads = parse_source(source, html)
            if not leads:
                raise SourceError('PARSER_EMPTY')
            enqueue(state, leads, source, config)
            source_success(state, source, len(leads), now)
            report['sources_ok'] += 1
        except Deferred as stop:
            source_deferred(state, source, str(stop), now)
            report['sources_deferred'] += 1
            if str(stop) == 'RUN_REQUEST_LIMIT':
                break
        except SourceError as stop:
            source_deferred(state, source, str(stop), now)
            report['sources_deferred'] += 1

    processed = 0
    max_processed = int(config.get('max_processed_per_run', 4000))
    while state['pending'] and report['new'] < budget and processed < max_processed:
        candidate = state['pending'].pop(0)
        result = add(state, candidate)
        processed += 1
        if result == 'NEW':
            report['new'] += 1
        elif result == 'INVALID':
            report['invalid'] += 1
        else:
            report['already_collected'] += 1

    report['requests'] = client.requests
    report['remaining'] = len(state['pending'])
    report['processed'] = processed
    return report


