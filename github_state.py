import base64, json, re
import urllib.request, urllib.parse, urllib.error

class Conflict(Exception):
    pass


class Transient(Exception):
    """A registry request failed transiently; a PUT may already have committed."""
    pass


class GitHubState:
    def __init__(self, repo, branch, token):
        if not token or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
            raise ValueError('Shared registry credentials/repository missing')
        self.repo, self.branch, self.token = repo, branch, token

    def request(self, method, route, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request('https://api.github.com/repos/' + self.repo + '/' + route, data=raw, method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'})
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

    def read(self, path):
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

    def write(self, path, data, sha):
        encoded = base64.b64encode((json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n').encode()).decode()
        return self.request('PUT', 'contents/' + urllib.parse.quote(path, safe='/'), {
            'message': 'Hotel collection checkpoint [skip ci]', 'branch': self.branch, 'sha': sha, 'content': encoded})

