import unittest
import enrich_master as e

class EnricherSafetyTests(unittest.TestCase):
    def test_final_statuses_are_protected(self):
        self.assertTrue(e.finalized("✅ Packaged"))
        self.assertTrue(e.finalized("⚠️ REVIEW — already contacted / duplicate"))
        self.assertTrue(e.finalized("⛔ Rejected — DUPLICATE HOTEL ROW"))
        self.assertFalse(e.finalized("RAW | 2026-09-25-R700-A"))
        self.assertFalse(e.finalized("Needs review"))

    def test_better_recipient_wins(self):
        site="https://example-hotel.de"
        self.assertGreater(e.score_email("personal@example-hotel.de",site), e.score_email("info@example-hotel.de",site))

    def test_offer_requires_training_and_profession(self):
        site="https://example-hotel.de"
        good=e.score_offer("https://example-hotel.de/karriere/ausbildung","Ausbildung zum Hotelfachmann (m/w/d)",site)
        weak=e.score_offer("https://example-hotel.de/karriere","Wir suchen Mitarbeiter",site)
        self.assertGreaterEqual(good,85)
        self.assertLess(weak,85)

    def test_domain_normalization(self):
        self.assertTrue(e.same_domain("example-hotel.de","https://www.example-hotel.de/jobs"))
        self.assertFalse(e.same_domain("example-hotel.de","other-hotel.de"))

if __name__=="__main__":
    unittest.main()
