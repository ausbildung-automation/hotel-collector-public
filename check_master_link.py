"""Read-only verification that the configured service account can access the intended Master."""
import json
import os
from urllib.parse import quote

from sync_master import HEADERS, TABS

MASTER_ID = "1qBViUSJvnnkagG_2XPlxSgeHQOJA4MDm0_gFKitrxcI"


def main():
    raw = os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        print("MASTER_LINK_SKIPPED_NO_SERVICE_ACCOUNT")
        return 0

    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession

    info = json.loads(raw)
    if info.get("type") != "service_account" or info.get("token_uri") != "https://oauth2.googleapis.com/token":
        raise ValueError("Invalid Google service-account credentials")

    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    session = AuthorizedSession(credentials)
    base = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(MASTER_ID, safe="")

    meta = session.get(base + "?fields=spreadsheetId,sheets.properties", timeout=45)
    if not meta.ok:
        raise RuntimeError("Master metadata read failed")

    data = meta.json()
    if data.get("spreadsheetId") != MASTER_ID:
        raise RuntimeError("Unexpected spreadsheet ID")

    props = {s["properties"]["title"]: s["properties"] for s in data.get("sheets", [])}
    for tab in TABS.values():
        if tab not in props:
            raise RuntimeError("Required Master tab missing")
        if props[tab]["gridProperties"]["columnCount"] != 11:
            raise RuntimeError("Unexpected Master column count")

        values_url = base + "/values/" + quote(f"'{tab}'!A3:K3", safe="")
        response = session.get(values_url, timeout=45)
        if not response.ok:
            raise RuntimeError("Master header read failed")
        rows = response.json().get("values", [])
        if not rows or rows[0] != HEADERS:
            raise RuntimeError("Master headers do not match sync schema")

    print("MASTER_LINK_OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("MASTER_LINK_FAILED; credentials and spreadsheet content suppressed")
        raise SystemExit(2)
