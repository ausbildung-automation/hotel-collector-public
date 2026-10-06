import random
import re
import time
from email.utils import parsedate_to_datetime
from urllib import error, parse, request, robotparser
from collector_common import BOT, Deferred, canonical
class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CourteousHTTP:
    def __init__(self, state, allowed_hosts, opener=None, clock=time.time, sleeper=time.sleep, rng=random.uniform, max_requests=28, max_requests_per_host=4):
        self.state = state
        self.allowed = set(allowed_hosts)
        self.opener = opener or request.build_opener(NoRedirect()).open
        self.clock = clock
        self.sleep = sleeper
        self.rng = rng
        self.requests = 0
        self.max_requests = max_requests
        self.max_requests_per_host = max_requests_per_host
        self.host_requests = {}

    def host(self, url):
        canonical(url)
        host = parse.urlsplit(url).hostname.lower()
        if host not in self.allowed:
            raise Deferred('SOURCE_NOT_ALLOWLISTED')
        return host

    def pause(self, url, status, headers=None):
        host = self.host(url)
        s = self.state['hosts'].setdefault(host, {})
        failures = s.get('failures', 0) + 1
        s['failures'] = failures
        seconds = min(7 * 86400, 900 * 2 ** min(failures - 1, 9))
        if status in (401, 403):
            seconds = max(seconds, 86400)
        value = (headers or {}).get('Retry-After', '')
        if value:
            try:
                seconds = max(seconds, float(value))
            except ValueError:
                try:
                    seconds = max(seconds, parsedate_to_datetime(value).timestamp() - self.clock())
                except (ValueError, TypeError, OverflowError):
                    pass
        s['until'] = self.clock() + seconds
        s['last_status'] = status

    def raw(self, url, headers=None):
        host = self.host(url)
        s = self.state['hosts'].setdefault(host, {})
        if s.get('until', 0) > self.clock():
            raise Deferred('HOST_COOLDOWN')
        if self.requests >= self.max_requests:
            raise Deferred('RUN_REQUEST_LIMIT')
        if self.host_requests.get(host, 0) >= self.max_requests_per_host:
            raise Deferred('HOST_REQUEST_LIMIT')
        delay = max(15, s.get('delay', 15)) + self.rng(0, 10)
        remaining = s.get('last_request', 0) + delay - self.clock()
        if remaining > 60:
            raise Deferred('LONG_CRAWL_DELAY')
        if remaining > 0:
            self.sleep(remaining)
        self.requests += 1
        self.host_requests[host] = self.host_requests.get(host, 0) + 1
        s['last_request'] = self.clock()
        try:
            req = request.Request(url, headers={'User-Agent': BOT, 'Accept': 'text/html,text/plain;q=0.9', **(headers or {})})
            with self.opener(req, timeout=25) as response:
                body = response.read(6 * 1024 * 1024 + 1)
                if len(body) > 6 * 1024 * 1024:
                    raise Deferred('PAGE_TOO_LARGE')
                return body, response.headers
        except error.HTTPError as exc:
            if exc.code in (304, 404, 410):
                raise
            self.pause(url, exc.code, exc.headers)
            raise Deferred('HTTP_' + str(exc.code)) from None
        except (error.URLError, TimeoutError, OSError):
            self.pause(url, 0)
            raise Deferred('NETWORK_COOLDOWN') from None

    def robots(self, url):
        host = self.host(url)
        s = self.state['hosts'].setdefault(host, {})
        if s.get('robots_until', 0) <= self.clock():
            robots_url = parse.urlunsplit(('https', host, '/robots.txt', '', ''))
            try:
                body, _ = self.raw(robots_url)
            except error.HTTPError as exc:
                if exc.code in (404, 410):
                    body = b'User-agent: *\nDisallow:\n'
                else:
                    raise Deferred('ROBOTS_UNAVAILABLE') from None
            s['robots'] = body.decode('utf-8', 'replace')[:512000]
            s['robots_until'] = self.clock() + 86400
        rp = robotparser.RobotFileParser()
        rp.parse(s['robots'].splitlines())
        if not rp.can_fetch(BOT, url):
            raise Deferred('ROBOTS_DISALLOW')
        rate = rp.request_rate(BOT)
        s['delay'] = max(15, rp.crawl_delay(BOT) or 0, rate.seconds / rate.requests if rate and rate.requests else 0)

    def get(self, url, refresh_hours=72):
        self.robots(url)
        page = self.state['pages'].setdefault(url, {})
        if page.get('checked_at') and page['checked_at'] + max(1, refresh_hours) * 3600 > self.clock():
            raise Deferred('CACHED_FRESH')
        headers = {}
        if page.get('etag'):
            headers['If-None-Match'] = page['etag']
        if page.get('modified'):
            headers['If-Modified-Since'] = page['modified']
        try:
            body, received = self.raw(url, headers)
        except error.HTTPError as exc:
            page['checked_at'] = self.clock()
            if exc.code == 304:
                raise Deferred('UNCHANGED') from None
            raise Deferred('REMOVED_' + str(exc.code)) from None
        text = body.decode('utf-8', 'replace')
        if re.search(r'(cf-chl-|g-recaptcha|hcaptcha|verify you are human|access denied)', text, re.I):
            self.pause(url, 403)
            raise Deferred('CHALLENGE_STOP')
        if 'html' not in received.get('Content-Type', 'text/html').lower():
            raise Deferred('NON_HTML')
        page.update(checked_at=self.clock(), etag=received.get('ETag', ''), modified=received.get('Last-Modified', ''))
        self.state['hosts'][self.host(url)]['failures'] = 0
        return text


