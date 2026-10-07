import copy
import json
import time
import unittest
from unittest.mock import patch
from collector_common import blank, keys
from collector_http import CourteousHTTP
from collector_common import Deferred
from identity import same, upsert, canonicalize
from verification import verify_page, publishable, best_email, professions
from production import HEADERS, ingest_sheet, ingest_history, check_history, rows_for, sync, run_cycle
from discovery import search_query, parse_discovery

NOW=time.time()
def hotel(name='Hotel Alpenblick',city='München',website='https://alpenblick.example/'):return {'name':name,'city':city,'website':website,'emails':[],'collection_id':'a','aliases':['a'],'first_seen':NOW,'last_seen':NOW,'source':website}
def html(email='info@alpenblick.example',extra=''):
    return f'<title>Hotel Alpenblick München</title><h1>Hotel Alpenblick</h1><p>Ausbildung Hotelfachmann Beginn 2027</p><p>{email}</p>{extra}'
def verified():
    r=hotel();verify_page(r,r['website'],html(),NOW);return r

class IdentityTests(unittest.TestCase):
    def test_legal_variant_with_city_and_domain_merges(self):
        self.assertTrue(same(hotel(),hotel('Hotel Alpenblick GmbH & Co. KG')))
    def test_different_chain_cities_do_not_merge(self):
        self.assertFalse(same(hotel(),hotel(city='Berlin')))
    def test_shared_email_alone_never_merges(self):
        a=hotel();b=hotel('Hotel Seeblick',website='https://chain.example')
        a['emails']=b['emails']=['jobs@chain.example'];self.assertFalse(same(a,b))
    def test_shared_domain_and_name_without_location_not_enough(self):
        self.assertFalse(same(hotel(city=''),hotel(city='')))
    def test_generic_names_need_address(self):
        a=hotel('Hotel Post');b=hotel('Hotel Post');self.assertFalse(same(a,b))
        a['street']=b['street']='Hauptstrasse 1';self.assertTrue(same(a,b))
    def test_conflicting_street_blocks_merge(self):
        a=hotel();b=hotel();a['street']='A 1';b['street']='A 2';self.assertFalse(same(a,b))
    def test_aliases_and_professions_survive_restart(self):
        s=blank();a=hotel();a['profession']='Hotelfachmann/-frau';upsert(s,a)
        b=hotel('Hotel Alpenblick GmbH');b['collection_id']='b';b['aliases']=['b'];b['profession']='Koch/Köchin'
        cid,new=upsert(s,b);self.assertFalse(new);self.assertEqual(cid,'a')
        s=json.loads(json.dumps(s));cid,new=upsert(s,b)
        self.assertFalse(new);self.assertEqual(s['aliases']['b'],'a');self.assertIn('Koch/Köchin',s['entities']['a']['profession'])
    def test_legacy_state_sections_never_removed(self):
        s=blank();s['records']['a']=hotel();before=copy.deepcopy(s['records']);canonicalize(s)
        self.assertEqual(s['records'],before)
    def test_history_same_location_suppresses(self):
        s=blank();s['historical_entities']=[dict(hotel(),history_id='old')];r=hotel();check_history(s,r)
        self.assertEqual(r['historical_match'],'old')
    def test_history_chain_other_location_does_not_suppress(self):
        s=blank();s['historical_entities']=[dict(hotel(city='Berlin'),history_id='old')];r=hotel();check_history(s,r)
        self.assertFalse(r.get('historical_match'))

class EvidenceTests(unittest.TestCase):
    def test_official_exact_hotel_email_and_training_accepted(self):self.assertTrue(publishable(verified(),NOW))
    def test_blank_email_not_published(self):
        r=hotel();verify_page(r,r['website'],html(''),NOW);self.assertFalse(publishable(r,NOW))
    def test_guessed_or_configured_email_rejected(self):
        r=hotel();r['emails']=['jobs@alpenblick.example'];verify_page(r,r['website'],html(''),NOW);self.assertIsNone(best_email(r,NOW))
    def test_third_party_cannot_verify(self):
        r=hotel();verify_page(r,'https://directory.example',html(),NOW);self.assertFalse(publishable(r,NOW))
    def test_snippet_without_page_identity_rejected(self):
        r=hotel();verify_page(r,r['website'],'Ausbildung Hotelfachmann jobs@alpenblick.example',NOW);self.assertFalse(publishable(r,NOW))
    def test_hr_outranks_info_and_no_downgrade(self):
        r=verified();verify_page(r,r['website'],html('personal@alpenblick.example'),NOW)
        r['selected_email']=best_email(r,NOW)['email'];verify_page(r,r['website']+'kontakt',html(),NOW)
        self.assertEqual(best_email(r,NOW)['email'],'personal@alpenblick.example')
    def test_chain_contact_requires_local_context(self):
        r=hotel();raw='<h1>Unsere Hotels</h1>Hotel Alpenblick München Ausbildung Hotelfachmann'+(' x'*500)+'jobs@alpenblick.example'
        verify_page(r,r['website'],raw,NOW);self.assertIsNone(best_email(r,NOW))
    def test_chain_contact_with_exact_location_context_accepted(self):
        r=hotel();raw='<h1>Unsere Hotels</h1><p>Hotel Alpenblick München Ausbildung Hotelfachmann Bewerbung: jobs@alpenblick.example</p>'
        verify_page(r,r['website'],raw,NOW);self.assertTrue(publishable(r,NOW))
    def test_another_property_training_not_borrowed(self):
        r=hotel();raw='<h1>Unsere Hotels</h1>Hotel Alpenblick München Kontakt info@alpenblick.example'+(' x'*500)+'Hotel Seeblick Berlin Ausbildung Hotelfachmann'
        verify_page(r,r['website'],raw,NOW);self.assertFalse(publishable(r,NOW))
    def test_identity_mismatch_rejected(self):
        r=hotel('Hotel Seeblick');verify_page(r,r['website'],html(),NOW);self.assertIsNone(best_email(r,NOW))
    def test_email_scope_and_identity_evidence_required(self):
        r=verified();r['email_evidence'][0]['hotel_identity']='other';self.assertFalse(publishable(r,NOW))
    def test_expired_evidence_reverified_before_publish(self):self.assertFalse(publishable(verified(),NOW+91*86400))
    def test_2027_not_inferred_from_copyright(self):
        r=hotel();verify_page(r,r['website'],html().replace('Beginn 2027','')+'<footer>Copyright 2027</footer>',NOW)
        self.assertEqual(r['training_evidence'][0]['status'],'CURRENT_AUSBILDUNG_NO_2027_DATE')
    def test_training_professions_aggregate(self):
        r=hotel();verify_page(r,r['website'],html(extra='<p>Ausbildung Koch und Fachkraft Küche</p>'),NOW)
        self.assertIn('Koch/Köchin',r['profession']);self.assertIn('Fachkraft Küche',r['profession'])
    def test_unrelated_koch_without_training_not_detected(self):self.assertFalse(professions('Unser Koch begrüßt Sie'))

class SyncTests(unittest.TestCase):
    def test_preserve_human_fields_including_private_pending(self):
        s=blank();s['records']['a']=verified();row=['Hotel Alpenblick','info@alpenblick.example','https://alpenblick.example/','','','','','','2027 offer confirmed','my note','a']
        ingest_sheet(s,[HEADERS,row]);rows=rows_for(s,[HEADERS,row]);self.assertEqual(rows[0][8:10],row[8:10])
        s['entities']['a']['email_evidence']=[];self.assertEqual(rows_for(s,[HEADERS,row]),[]);self.assertEqual(s['human_fields']['a']['notes'],'my note')
    def test_all_legacy_rows_preserved_before_migration(self):
        s=blank();row=['Hotel New','','','','','','','','Not reviewed','remember','old']
        ingest_sheet(s,[HEADERS,row]);self.assertIn('old',s['entities']);self.assertEqual(s['migration']['original_sheet'],[HEADERS,row])
    def test_atomic_single_tab_write_checkpoint_before_write(self):
        s=blank();s['records']['a']=verified();events=[]
        class Sheet:
            props={'COLLECTOR_NEW':{'sheetId':7,'gridProperties':{'rowCount':1000}}}
            rows=[HEADERS]
            def collector(self):return self.rows
            def req(self,method,route,body):
                events.append('write');self.body=body
                data=body['requests'][0]['updateCells']['rows']
                self.rows=[HEADERS]+[[v['userEnteredValue']['stringValue'] for v in row['values']] for row in data]
        sh=Sheet();ingest_sheet(s,[HEADERS]);sync(sh,s,lambda:events.append('checkpoint'))
        self.assertEqual(events,['checkpoint','write','checkpoint'])
        self.assertTrue(all(list(r)[0] in ('updateCells','repeatCell') for r in sh.body['requests']))
        self.assertTrue(all(next(iter(r.values()))['range']['sheetId']==7 for r in sh.body['requests']))
    def test_concurrent_edit_stops_before_write(self):
        s=blank();s['records']['a']=verified()
        class Sheet:
            n=0
            def collector(self):
                self.n+=1
                return [HEADERS] if self.n==1 else [HEADERS,['changed']]
            def req(self,*a):raise AssertionError('must not write')
        with self.assertRaisesRegex(ValueError,'concurrently'):sync(Sheet(),s,lambda:None)
    def test_runtime_checkpoint_keeps_unresolved(self):
        s=blank();s['records']['a']=hotel();c=CourteousHTTP(s,[],deadline=0,clock=lambda:NOW,check_dns=False);calls=[]
        with patch.dict('os.environ',{'SERPER_API_KEY':''}):report=run_cycle(s,{'sources':[]},c,lambda:calls.append(1))
        self.assertTrue(calls);self.assertIn('a',s['entities']);self.assertEqual(report['published'],0)
    def test_runtime_stops_before_network(self):
        c=CourteousHTTP(blank(),['example.com'],deadline=NOW+10,clock=lambda:NOW,check_dns=False)
        with self.assertRaisesRegex(Deferred,'RUNTIME_LIMIT'):c.reserve('https://example.com')
        self.assertEqual(c.requests,0)
    def test_query_rotation_all_regions_and_families(self):
        queries=[search_query(i) for i in range(96)]
        self.assertEqual(len(set(f for f,q in queries)),6);self.assertEqual(len(set(q for f,q in queries)),96)
    def test_structured_job_respects_germany_and_location(self):
        obj={'@type':'JobPosting','title':'Ausbildung Hotelfachmann','hiringOrganization':{'name':'Hotel Test','url':'https://test.example'},'jobLocation':{'address':{'addressCountry':'DE','addressLocality':'Berlin'}}}
        raw='<script type="application/ld+json">'+json.dumps(obj)+'</script>'
        self.assertEqual(parse_discovery('https://test.example/jobs',raw,NOW)[0]['city'],'Berlin')
    def test_workflow_no_cron_same_concurrency_and_budget(self):
        from pathlib import Path
        a=Path('.github/workflows/collect.yml').read_text();b=Path('.github/workflows/enrich-master.yml').read_text()
        self.assertIn('types: [external-cron]',a);self.assertIn('timeout-minutes: 60',a)
        self.assertNotIn('schedule:',a+b);self.assertIn('group: hotel-collection-private-state',b)

if __name__=='__main__':unittest.main()
