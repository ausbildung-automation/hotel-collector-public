import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class Conflict(Exception):
    pass


class Transient(Exception):
    """A registry request failed transiently; a PUT may already have committed."""
    pass


class GitHubState:
    def __init__(self, repo, branch, token, sleeper=time.sleep):
        if not token or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
            raise ValueError('Shared registry credentials/repository missing')
        self.repo, self.branch, self.token = repo, branch, token
        self.sleep = sleeper

    def request(self, method, route, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            'https://api.github.com/repos/' + self.repo + '/' + route,
            data=raw,
            method=method,
            headers={
                'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json',
                'Content-Type': 'application/json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if method == 'PUT' and error.code in (409, 422):
                raise Conflict() from error
            if error.code in (500, 502, 503, 504):
                raise Transient(f'GitHub {method} temporary error {error.code}') from error
            raise RuntimeError(f'GitHub {method} error {error.code}; collection paused') from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise Transient('Temporary registry connection failure') from error

    def _read_once(self, path):
        route = 'contents/' + urllib.parse.quote(path, safe='/') + '?ref=' + urllib.parse.quote(self.branch, safe='')
        item = self.request('GET', route)
        if item.get('encoding') == 'base64' and item.get('content'):
            raw = base64.b64decode(item['content'])
        else:
            blob = self.request('GET', 'git/blobs/' + item['sha'])
            if blob.get('encoding') != 'base64':
                raise ValueError('Unsupported GitHub content encoding')
            raw = base64.b64decode(blob['content'])
        return json.loads(raw), item['sha']

    def read(self, path):
        last = None
        for attempt in range(4):
            try:
                return self._read_once(path)
            except Transient as exc:
                last = exc
                self.sleep(min(0.5 * (attempt + 1), 2))
        raise RuntimeError('Private collection state temporarily unreadable; collection paused') from last

    def write(self, path, data, sha):
        serialized = json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n'
        encoded = base64.b64encode(serialized.encode()).decode()
        route = 'contents/' + urllib.parse.quote(path, safe='/')
        payload = {
            'message': 'Hotel collection checkpoint [skip ci]',
            'branch': self.branch,
            'sha': sha,
            'content': encoded,
        }
        last = None
        for attempt in range(4):
            try:
                return self.request('PUT', route, payload)
            except Conflict:
                raise
            except Transient as exc:
                last = exc
                # PUT may already have committed. Verify before retrying anything.
                try:
                    remote, remote_sha = self.read(path)
                except RuntimeError:
                    self.sleep(min(0.5 * (attempt + 1), 2))
                    continue
                if remote == data:
                    return {'verified_after_transient': True, 'sha': remote_sha}
                if remote_sha != sha:
                    raise Conflict('State changed while verifying an ambiguous write') from exc
                self.sleep(min(0.5 * (attempt + 1), 2))
        raise RuntimeError('Private collection state write could not be verified; collection paused') from last
