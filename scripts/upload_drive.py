# -*- coding: utf-8 -*-
"""Массовая выгрузка снимков камер на Google Drive (сервис-аккаунт).
Пишет row->file_id в snap_urls.json рядом с папкой снимков.
Волна I: 443 пути из tg_bot_config.json/аргумента (не чужой scratchpad),
444 папка «Снимки_камер_МФК» ищется по имени+parent и переиспользуется
(создаётся только при отсутствии), JWT/ретраи — общий google_api (439).
Использование: py scripts\\upload_drive.py [папка_с_JPEG]"""
import sys
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              line_buffering=True)
sys.path.insert(0, r"C:\Users\1\camera")

import google_api as g  # noqa: E402

BASE = r"C:\Users\1\camera"
CFG = json.load(open(os.path.join(BASE, "tg_bot_config.json"), encoding="utf-8"))
SA = os.environ.get("GOOGLE_SA") or CFG.get("sa_path") \
    or os.path.join(BASE, "service-account.json")
PARENT = os.environ.get("GDRIVE_FOLDER") or CFG.get("drive_folder_id") or ""
IMGDIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "snaps")
FOLDER_NAME = "Снимки_камер_МФК"
DRIVE = "https://www.googleapis.com/drive/v3"

if not os.path.isdir(IMGDIR):
    sys.exit(f"нет папки со снимками: {IMGDIR} "
             f"(укажи аргументом: py scripts\\upload_drive.py <папка>)")
g.token(sa_path=SA, scope=g.SCOPE_DRIVE)
print("token OK")

# 444: find-or-create папки (раньше плодилась новая при каждом запуске)
q = (f"name='{FOLDER_NAME}' and trashed=false and "
     f"mimeType='application/vnd.google-apps.folder'"
     + (f" and '{PARENT}' in parents" if PARENT else ""))
got = g.gjson("GET", f"{DRIVE}/files", sa_path=SA, scope=g.SCOPE_DRIVE,
              params={"q": q, "fields": "files(id,name)"},
              timeout=30).get("files") or []
if got:
    FID = got[0]["id"]
    print("folder reused:", FID)
else:
    body = {"name": FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder"}
    if PARENT:
        body["parents"] = [PARENT]
    FID = g.gjson("POST", f"{DRIVE}/files", sa_path=SA, scope=g.SCOPE_DRIVE,
                  json=body, timeout=30, fields="id")["id"]
    g.request("POST", f"{DRIVE}/files/{FID}/permissions", sa_path=SA,
              scope=g.SCOPE_DRIVE, timeout=30,
              json={"role": "reader", "type": "anyone"})
    print("folder created:", FID)

files = sorted(f for f in os.listdir(IMGDIR) if f.lower().endswith(".jpg"))
print("files to upload:", len(files))


def up(fn):
    m = re.search(r"row(\d+)", fn)
    row = int(m.group(1)) if m else fn
    with open(os.path.join(IMGDIR, fn), "rb") as f:
        data = f.read()
    try:  # 445: resumable — обрыв не теряет файл
        j = g.upload_resumable(fn, data, parents=[FID], mime="image/jpeg",
                               sa_path=SA, fields="id")
        return row, j.get("id")
    except Exception:
        return row, None


out = {}
with ThreadPoolExecutor(max_workers=6) as ex:
    n = 0
    for row, fid in ex.map(up, files):
        out[row] = fid
        n += 1
        if n % 100 == 0:
            print(f"  {n}/{len(files)}")
okc = sum(1 for v in out.values() if v)
print(f"uploaded: {okc}/{len(files)}")
json.dump({"folder": FID, "rows": out},
          open(os.path.join(IMGDIR, "snap_urls.json"), "w"))
print("-> snap_urls.json (в папке снимков)")
