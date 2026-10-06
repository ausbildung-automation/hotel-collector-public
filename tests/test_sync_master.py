import unittest
from sync_master import HEADERS, plan

class SyncSafety(unittest.TestCase):
    def fixture(self):
        return {'name': '=unsafe()', 'city': 'Berlin', 'source': 'https://example.org', 'status': 'NEEDS_REVIEW', 'last_seen': 1, 'collection_id': 'id1'}

    def test_preserves_manual_decisions_when_rows_sorted(self):
        row = [''] * 11
        row[8:11] = ['عرض 2027 مؤكد / 2027 offer confirmed', 'Keep my note', 'id1']
        changes, _ = plan('COLLECTOR_NEW', {'id1': self.fixture()}, [HEADERS, [''] * 10 + ['id2'], row])
        self.assertEqual(changes[0]['range'], "'COLLECTOR_NEW'!A5:H5")
        self.assertEqual(len(changes[0]['values'][0]), 8)

    def test_new_rows_keep_stable_identity_and_literal_text(self):
        changes, _ = plan('COLLECTOR_NEW', {'id1': self.fixture()}, [HEADERS])
        self.assertEqual(changes[0]['range'], "'COLLECTOR_NEW'!A4:K4")
        self.assertEqual(changes[0]['values'][0][0], '=unsafe()')
        self.assertEqual(changes[0]['values'][0][10], 'id1')
        self.assertNotIn('Berlin', changes[0]['values'][0])

    def test_changed_headers_stop(self):
        with self.assertRaises(ValueError):
            plan('COLLECTOR_NEW', {}, [['Wrong headers']])

    def test_ambiguous_identifiers_stop(self):
        row = [''] * 10 + ['id1']
        with self.assertRaises(ValueError):
            plan('COLLECTOR_NEW', {}, [HEADERS, row, row])
