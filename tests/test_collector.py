import io, json, unittest
from urllib.error import HTTPError
from collector import CourteousHTTP, Deferred, DirectoryParser, add, blank, run

URL='https://directory.example/list'
HTML='''<h3>Hotel Alpenblick</h3><p>82467 Garmisch Ausbildungsberufe: Hotelfachfrau / Hotelfachmann</p><p>E-Mail: jobs@alpenblick.example</p><a href="https://alpenblick.example/">Website</a><h3>Restaurant Elsewhere</h3><p>Koch other@example.org</p><a href="https://elsewhere.example/">Website</a>'''

def lead(name='Hotel Alpenblick',city='Garmisch',email='jobs@alpenblick.example',website='https://alpenblick.example/'):
    return {'name':name,'city':city,'emails':[email],'website':website,'source':URL}

class Response:
    def __init__(self,body,headers=None): self.body=body;self.headers=headers or {'Content-Type':'text/html'}
    def read(self,*args): return self.body
    def __enter__(self): return self
    def __exit__(self,*args): pass

class Tests(unittest.TestCase):
    def test_email_stays_inside_its_hotel_card(self):
        p=DirectoryParser();p.feed(HTML);leads=list(p.leads(URL))
        self.assertEqual(len(leads),1);self.assertEqual(leads[0]['emails'],['jobs@alpenblick.example'])
        self.assertEqual(leads[0]['city'],'Garmisch')
    def test_repeated_source_blocks_single_new_record(self):
        s=blank();self.assertEqual(add(s,lead()),'NEW');self.assertEqual(add(s,lead()),'DUPLICATE')
        self.assertEqual(len(s['records']),1)
    def test_changed_email_does_not_repeat_hotel(self):
        s=blank();add(s,lead());self.assertEqual(add(s,lead(email='new@alpenblick.example')),'DUPLICATE')
    def test_historical_collected_but_unsent_is_excluded(self):
        s=blank();add(s,lead(),historical=True)
        self.assertEqual(add(s,lead()),'ALREADY_COLLECTED_REVIEW');self.assertFalse(s['records'])
    def test_chain_domain_is_review_not_automatic_duplicate(self):
        s=blank();add(s,lead(website='https://chain.example/hotel-a'))
        outcome=add(s,lead(name='Hotel Seeblick',city='Berlin',email='b@chain.example',website='https://chain.example/hotel-b'))
        self.assertEqual(outcome,'ALREADY_COLLECTED_REVIEW')
    def test_robots_disallow_stops_before_page(self):
        s=blank();calls=[]
        def op(req,timeout): calls.append(req.full_url);return Response(b'User-agent: *\nDisallow: /list\n')
        c=CourteousHTTP(s,['directory.example'],opener=op)
        with self.assertRaisesRegex(Deferred,'ROBOTS_DISALLOW'): c.get(URL)
        self.assertEqual(calls,['https://directory.example/robots.txt'])
    def test_429_is_not_retried_and_cooldown_survives_restart(self):
        s=blank();calls=[]
        def op(req,timeout): calls.append(req.full_url);raise HTTPError(req.full_url,429,'limited',{'Retry-After':'3600'},None)
        c=CourteousHTTP(s,['directory.example'],opener=op,clock=lambda:1000000)
        with self.assertRaises(Deferred): c.get(URL)
        self.assertEqual(len(calls),1)
        restored=json.loads(json.dumps(s));c2=CourteousHTTP(restored,['directory.example'],opener=op,clock=lambda:1000100)
        with self.assertRaisesRegex(Deferred,'HOST_COOLDOWN'): c2.get(URL)
        self.assertEqual(len(calls),1)
    def test_long_retry_after_is_honoured(self):
        s=blank();c=CourteousHTTP(s,['directory.example'],clock=lambda:1000000)
        c.pause(URL,429,{'Retry-After':'172800'})
        self.assertEqual(s['hosts']['directory.example']['until'],1172800)
    def test_challenge_stops_and_pauses_host(self):
        s=blank();s['hosts']['directory.example']={'robots':'User-agent: *\nAllow: /','robots_until':9999999}
        c=CourteousHTTP(s,['directory.example'],opener=lambda *a,**kw:Response(b'verify you are human'),clock=lambda:1000000)
        with self.assertRaisesRegex(Deferred,'CHALLENGE_STOP'): c.get(URL)
        self.assertGreaterEqual(s['hosts']['directory.example']['until'],1086400)
    def test_cache_avoids_network(self):
        s=blank();s['hosts']['directory.example']={'robots':'User-agent: *\nAllow: /','robots_until':9999999};s['pages'][URL]={'checked_at':1000000}
        c=CourteousHTTP(s,['directory.example'],opener=lambda *a,**kw:self.fail('unexpected request'),clock=lambda:1000100)
        with self.assertRaisesRegex(Deferred,'CACHED_72H'): c.get(URL)
    def test_request_budget_counts_robots(self):
        s=blank();c=CourteousHTTP(s,['directory.example'],opener=lambda *a,**kw:Response(b'User-agent: *\nAllow: /'),max_requests=1,sleeper=lambda _:None)
        with self.assertRaisesRegex(Deferred,'RUN_REQUEST_LIMIT'): c.get(URL)
        self.assertEqual(c.requests,1)
    def test_disallowed_hosts_do_not_receive_requests(self):
        c=CourteousHTTP(blank(),['directory.example'],opener=lambda *a,**kw:self.fail('unexpected request'))
        with self.assertRaises(Deferred): c.get('https://other.example/')
    def test_resume_pending_without_refetching(self):
        s=blank();s['pending']=[lead(),lead(name='Hotel City',city='Berlin',email='jobs@city.example',website='https://city.example/')]
        c=CourteousHTTP(s,['directory.example'],opener=lambda *a,**kw:self.fail('unexpected request'))
        config={'max_new_per_run':1,'sources':[{'url':URL}]}
        first=run(s,config,c);self.assertEqual(first['new'],1);self.assertEqual(first['remaining'],1)
        second=run(s,config,c);self.assertEqual(second['new'],1);self.assertEqual(second['remaining'],0)

if __name__=='__main__': unittest.main()
