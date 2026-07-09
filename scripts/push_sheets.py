# -*- coding: utf-8 -*-
"""Разовая выгрузка sheets.json -> НОВАЯ Google-таблица (создание + запись +
шапки + перенос в папку + шаринг). Волна I (443): пути/ID — из
tg_bot_config.json и аргументов, дата в названии — текущая; JWT/ретраи — из
общего google_api (439). Живую таблицу бота НЕ трогает — всегда создаёт новую.
Использование: py scripts\\push_sheets.py [путь_к_sheets.json] [заголовок]"""
import sys
import io
import json
import os
import datetime
from urllib.parse import quote

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"C:\Users\1\camera")

import google_api as g  # noqa: E402

BASE = r"C:\Users\1\camera"
CFG = json.load(open(os.path.join(BASE, "tg_bot_config.json"), encoding="utf-8"))
SA = os.environ.get("GOOGLE_SA") or CFG.get("sa_path") \
    or os.path.join(BASE, "service-account.json")
FOLDER = os.environ.get("GDRIVE_FOLDER") or CFG.get("drive_folder_id") or ""
SHARE_TO = os.environ.get("GSHARE_TO") or CFG.get("gcal_share_email") \
    or "realgank@gmail.com"
SHEETS_JSON = sys.argv[1] if len(sys.argv) > 1 \
    else os.path.join(BASE, "exports", "sheets.json")
TITLE = sys.argv[2] if len(sys.argv) > 2 else \
    f"Все камеры МФК Зарядье ({datetime.date.today().isoformat()})"
API = "https://sheets.googleapis.com/v4/spreadsheets"

g.token(sa_path=SA)
print("token OK")
data = json.load(open(SHEETS_JSON, encoding="utf-8"))
order = [n for n in ["Все камеры", "Лист1", "Неизвестные устройства"]
         if n in data] or list(data)

j = g.gjson("POST", API, sa_path=SA, timeout=60, json={
    "properties": {"title": TITLE},
    "sheets": [{"properties": {"title": n}} for n in order]})
sid, url = j["spreadsheetId"], j["spreadsheetUrl"]
print("created:", sid)


def coerce(v):
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


for n in order:
    rows = [[coerce(c) for c in row] for row in data[n]["rows"]]
    rr = g.gjson("PUT", f"{API}/{sid}/values/{quote(n + '!A1')}"
                        f"?valueInputOption=RAW",
                 sa_path=SA, json={"values": rows}, timeout=120)
    print(f"  wrote '{n}': {len(rows)} rows -> {rr.get('updatedCells')} cells")

meta = g.gjson("GET", f"{API}/{sid}", sa_path=SA, timeout=30,
               fields="sheets.properties(sheetId,title)")  # 437
reqs = []
for sh in meta["sheets"]:
    spid = sh["properties"]["sheetId"]
    reqs.append({"repeatCell": {
        "range": {"sheetId": spid, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.12, "green": 0.18, "blue": 0.33},
            "textFormat": {"bold": True,
                           "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
        "fields": "userEnteredFormat(textFormat,backgroundColor)"}})
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": spid, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount"}})
g.gjson("POST", f"{API}/{sid}:batchUpdate", sa_path=SA,
        json={"requests": reqs}, timeout=60)
print("  formatted headers")

if FOLDER:
    try:
        g.request("PATCH", f"https://www.googleapis.com/drive/v3/files/{sid}"
                  f"?addParents={FOLDER}&removeParents=root"
                  f"&supportsAllDrives=true",
                  sa_path=SA, scope=g.SCOPE_DRIVE, timeout=30)
        print("  moved to folder", FOLDER)
    except Exception as e:
        print("  move skipped:", e)

rs = g.request("POST", f"https://www.googleapis.com/drive/v3/files/{sid}"
               f"/permissions?sendNotificationEmail=true",
               sa_path=SA, scope=g.SCOPE_DRIVE, timeout=30,
               json={"role": "writer", "type": "user",
                     "emailAddress": SHARE_TO})
print("  shared:", rs.status_code)
print("URL:", url)
json.dump({"id": sid, "url": url},
          open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "sheet_url.json"), "w"))
