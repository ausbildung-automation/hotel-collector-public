# Hotel Ausbildung collector — production v3

Collection only: no application delivery, SMTP, or sender repository writes.

## Operation

The existing external scheduler dispatches `external-cron` to the main collection workflow. No GitHub cron or ChatGPT automation is configured. `.collector-run-now` is an explicit operator smoke-cycle trigger, not a scheduler. A manual compatibility workflow uses the same pipeline and concurrency group.

The workflow allows 60 minutes; network work stops after 50 minutes, reserving time for checkpoints and Sheet synchronization. Normal maximum: **120 HTTP requests**, including robots, API searches, and redirects; **4 per host**. Requests are sequential, with robots/crawl-delay/request-rate compliance, a 15–25 second minimum host interval, persistent exponential cooldown, Retry-After and CAPTCHA stop. No proxies or restriction evasion. DNS/private-network checks and bounded redirects protect discovered URLs.

## Discovery and verification

Six discovery families are supported through a persistent Germany-wide query rotation: employer sites, associations, chambers, hospitality portals, regional training directories, and official employment portals. Existing direct-employer, DEHOGA and Ausbildungskompass adapters remain. A configured `SERPER_API_KEY` enables query discovery; without it, the report explicitly says search is unavailable and existing sources/queued links still run. Search results are URL pointers, never email evidence. JSON-LD job postings and bounded career/contact/ATS links extend discovery without a fixed tiny hotel list.

Hotel training is prioritized across Hotelfach, Hotelmanagement, Restaurants und Veranstaltungsgastronomie, Gastronomie, Koch/Köchin and Fachkraft Küche. A training page without a 2027 date is labeled separately from explicit 2027 evidence.

Only a literal published email from an identified employer page or employer-linked ATS can pass publication. Chain pages require exact-property recipient context. Evidence retains URL, timestamp, identity, scope and recipient role. Configured or third-party emails alone cannot pass. Recruiting addresses outrank general contacts; an existing verified hotel-specific recipient is not downgraded. Verification means publicly sourced and relevant, **not an SMTP deliverability test**. Evidence is refreshed within 90 days.

## State and identity

Private `state/collection_registry.json` keeps all legacy fields and adds `entities`, `aliases`, `human_fields`, `historical_entities`, `discovery_queue`, `discovery_visited`, search cursor, evidence and per-hotel retry schedules. `_Registry` and `Do_Not_Repeat` are refreshed read-only each cycle; the existing 21,138 historical keys and delivery-history provenance remain intact. Counts of keys are not hotel counts.

One physical location has one canonical ID. Legal-name variants require corroborating city/domain/address/property signals. Conflicting known locations block merges. Generic names require a street/location URL. Shared email, brand or domain alone never merges locations. Ambiguities stay private. Old IDs remain aliases. All old rows and human decisions/notes are archived privately before any projection change, including historical matches and missing-email rows.

`COLLECTOR_NEW` is the only results tab. It contains verified, nonhistorical candidates with an email and training evidence. Unresolved hotels remain private with exponential retry delays, rather than being deleted. The first strict migration can reduce the visible count substantially while verification catches up; no throughput promise is made before measuring runs.

The Sheet is updated with one atomic batch, not clear-then-rewrite. Human fields are preserved by ID and aliases; conflicting decisions stop the write. A fresh read detects intervening human edits. Sheets offers no compare-and-swap transaction, so avoid editing during the brief final projection write. Other tabs are read-only. A failed sync never rolls back the private checkpoint.

## Validation and rollback

Run `python -m unittest discover -s tests` and `python -m py_compile *.py`.

Production: `python production.py`. Compatibility: `python collector.py --remote --persist`. Sync only: `python sync_master.py`.

Rollback refs in both repositories: `rollback/pre-production-upgrade-20261007`. The original state sections are preserved for recovery. Do not revert only the code and let the old sync run against new live state without restoring a consistent private checkpoint and Sheet snapshot.

Logs contain aggregate requests/hosts/source families/leads/new identities/suppressed duplicates/verification attempts/verified emails/pending/published/explicit-2027/duration/error categories only. Private data must never be committed to this public repository or uploaded as public Actions artifacts.
