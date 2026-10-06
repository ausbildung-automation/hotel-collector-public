"""Private Sheets projection. Never modifies legacy tabs or review decisions."""
import json
import os
from datetime import datetime, timezone
from urllib.parse import quote

TABS = {'records': 'COLLECTOR_NEW', 'review_candidates': 'COLLECTOR_DUPLICATE_REVIEW'}
HEADERS = ['المنشأة', 'المدينة', 'الإيميل', 'رابط المنشأة', 'التخصص', 'حالة الجمع', 'دليل المصدر', 'تاريخ الجمع UTC', 'مصدر الجمع', 'قرار المراجعة', 'ملاحظاتك', 'معرّف ثابت']

def projection(key, record):
    stamp = datetime.fromtimestamp(record.get('first_seen', record['last_seen']), timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    return [record['name'], record.get('city', ''), '; '.join(record.get('emails', [])), record.get('website', ''), record.get('profession', ''), 'تحتاج مراجعة' if record['status'] == 'NEEDS_REVIEW' else 'تطابق محتمل — غير مؤكد', 'Hotelfach مذكور؛ سنة 2027 وقبول الإيميل غير متحققين', stamp, record['source'], 'لم تُراجع', '', record.get('collection_id', key)]

def plan(tab, entries, existing):
    """Address rows by immutable ID; never overwrite human columns J/K."""
    if not existing or existing[0] != HEADERS:
        raise ValueError('Master tab headers changed; stop rather than overwrite')
    positions = {}
    for rownum, row in enumerate(existing[1:], 4):
        if not any(row):
            continue
        if len(row) < 12 or not row[11] or row[11] in positions:
            raise ValueError('Missing or duplicate row identity; manual review needed')
        positions[row[11]] = rownum
    next_row = len(existing) + 3
    changes = []
    for key, record in entries.items():
        row = projection(key, record)
        if row[11] in positions:
            n = positions[row[11]]
            changes.append({'range': f"'{tab}'!A{n}:I{n}", 'values': [row[:9]]})
        else:
            changes.append({'range': f"'{tab}'!A{next_row}:L{next_row}", 'values': [row]})
            next_row += 1
    return changes, next_row - 1

def main():
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    from github_state import GitHubState
    from collector import validate
    config = json.load(open('config.json'))
    state, _ = GitHubState(config['private_repo'], config['state_branch'], os.environ['PRIVATE_COLLECTOR_TOKEN']).read(config['state_path'])
    validate(state)
    info = json.loads(os.environ['GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON'])
    if info.get('type') != 'service_account' or info.get('token_uri') != 'https://oauth2.googleapis.com/token':
        raise ValueError('Only Google service-account credentials supported')
    session = AuthorizedSession(service_account.Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/spreadsheets']))
    base = 'https://sheets.googleapis.com/v4/spreadsheets/' + quote(os.environ['GOOGLE_SHEETS_ID'], safe='')
    def request(method, route, body=None):
        response = session.request(method, base + route, json=body, timeout=45)
        if not response.ok:
            raise RuntimeError('Sheets request failed; response content suppressed')
        return response.json()
    metadata = request('GET', '?fields=sheets.properties')
    sheets = {s['properties']['title']: s['properties'] for s in metadata['sheets']}
    changes, resizes = [], []
    for section, tab in TABS.items():
        props = sheets[tab]  # Missing tab stops; never create or select a legacy tab.
        end = props['gridProperties']['rowCount']
        existing = request('GET', '/values/' + quote(f"'{tab}'!A3:L{end}", safe='')) .get('values', [])
        planned, needed = plan(tab, state[section], existing)
        changes.extend(planned)
        if needed > end:
            resizes.append({'updateSheetProperties': {'properties': {'sheetId': props['sheetId'], 'gridProperties': {'rowCount': needed + 100}}, 'fields': 'gridProperties.rowCount'}})
        changes.append({'range': f"'{tab}'!A2", 'values': [['آخر مزامنة UTC: ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S') + ' — للمراجعة فقط؛ لا إرسال تلقائي.']]})
    if resizes:
        request('POST', ':batchUpdate', {'requests': resizes})
    # RAW keeps scraped strings from executing as formulas.
    for start in range(0, len(changes), 400):
        request('POST', '/values:batchUpdate', {'valueInputOption': 'RAW', 'data': changes[start:start + 400]})
    print(json.dumps({'master_sync': 'success', 'new_candidates': len(state['records']), 'review_candidates': len(state['review_candidates'])}))

if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('MASTER_SYNC_STOPPED; credentials and spreadsheet content suppressed')
        raise SystemExit(2)
