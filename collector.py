"""Courteous multi-source hotel Ausbildung discovery with persistent deduplication."""
import argparse
import json
import os
from pathlib import Path
from urllib import parse

from collector_common import *
from collector_parsers import AusbildungKompassParser, DirectoryParser, TextCollector, direct_leads
from collector_http import CourteousHTTP, NoRedirect
from collector_engine import enqueue, parse_source, pending_fingerprint, run, source_deferred, source_ready, source_success
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--state', default='state.json')
    parser.add_argument('--persist', action='store_true')
    parser.add_argument('--remote', action='store_true')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    backend = None
    if args.remote:
        from github_state import GitHubState
        backend = GitHubState(config['private_repo'], config['state_branch'], os.getenv('PRIVATE_COLLECTOR_TOKEN', ''))
        state, sha = backend.read(config['state_path'])
    else:
        state = json.loads(Path(args.state).read_text())
    validate(state)
    if args.remote and not args.persist:
        raise ValueError('Remote run must persist cooldowns and cursor')
    allowed_hosts = [parse.urlsplit(s['url']).hostname for s in config['sources']]
    client = CourteousHTTP(
        state,
        allowed_hosts,
        max_requests=int(config.get('max_requests_per_run', 28)),
        max_requests_per_host=int(config.get('max_requests_per_host_per_run', 4)),
    )
    try:
        report = run(state, config, client)
    finally:
        if args.persist:
            if backend:
                backend.write(config['state_path'], state, sha)
            else:
                path = Path(args.state)
                tmp = path.with_suffix('.tmp')
                tmp.write_text(json.dumps(state, ensure_ascii=False))
                tmp.replace(path)
    print(json.dumps(report))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('STOPPED: ' + type(exc).__name__ + '; no email delivery is part of this collector')
        raise SystemExit(2)
