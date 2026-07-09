# -*- coding: utf-8 -*-
"""Google-интеграции бота: /sync (пуш Все_камеры.xlsx в СУЩЕСТВУЮЩУЮ таблицу:
clear+update по листам + красная заливка офлайн-строк), /sheet (ссылка + время
последнего синка), заливка снимка /shot в Drive + индекс снимков.
Волна B: I25, I26, I29, I30, I31. Паттерны — scripts/push_sheets.py через google_api.
Таблица и Drive только ДОПОЛНЯЮТСЯ/перезаписываются; xlsx здесь не пишется."""
import os
import json
import time
import datetime
import threading

import bot_state as st
import google_api as g
from bot_util import log, log_exc

SYNC_STATE = os.path.join(st.BASE, "_sheets_sync.json")
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_UP = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"

SYNC_LOCK = threading.Lock()  # single-flight для /sync
_idx_lock = threading.Lock()

RED = {"red": 0.96, "green": 0.78, "blue": 0.78}
HDR_BG = {"red": 0.12, "green": 0.18, "blue": 0.33}


def _sa():
    return st.cget("sa_path")


def _sid():
    return st.cget("sheet_id")


def sheet_url():
    return f"https://docs.google.com/spreadsheets/d/{_sid()}/edit"


def _coerce(v):
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _health_columns(rows):
    """I28: дописывает к листу «Все камеры» колонки «Online (бот)» и
    «Проверка (бот)» из _health_state.json (если health ещё не бегал — молча)."""
    try:
        with open(st.cget("health_state_path"), encoding="utf-8") as f:
            hs = (json.load(f) or {}).get("ips") or {}
    except Exception:
        return
    if not rows or not hs:
        return
    try:
        ipc = rows[0].index("IP-адрес")
    except ValueError:
        return
    rows[0].extend(["Online (бот)", "Проверка (бот)"])
    for row in rows[1:]:
        ip = str(row[ipc] if ipc < len(row) else "").strip()
        e = hs.get(ip)
        if e and e.get("checked"):
            row.extend(["online" if e.get("ok") else "offline",
                        time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(e["checked"]))])
        else:
            row.extend(["", ""])


def _load_xlsx():
    """Все листы xlsx -> {title: rows}; плюс номера офлайн-строк главного листа
    (по последней колонке «Статус …») для красной заливки (I29)."""
    import openpyxl
    import bot_inventory as inv
    wb = openpyxl.load_workbook(inv.inv_path(), read_only=True, data_only=True)
    sheets, offline = {}, []
    for ws in wb.worksheets:
        rows = [[_coerce(c) for c in row] for row in ws.iter_rows(values_only=True)]
        while rows and all(c == "" for c in rows[-1]):
            rows.pop()
        sheets[ws.title] = rows
        if ws.title == inv.SHEET_MAIN and rows:
            stat_cols = [i for i, h in enumerate(rows[0])
                         if h and str(h).startswith("Статус")]
            if stat_cols:
                sc = stat_cols[-1]
                for rn, row in enumerate(rows[1:], start=1):  # 0-based grid index
                    v = str(row[sc] if sc < len(row) else "").lower()
                    if "офлайн" in v or "offline" in v:
                        offline.append(rn)
            _health_columns(rows)  # I28
    wb.close()
    return sheets, offline


def _ranges(idxs):
    """[1,2,3,7] -> [(1,4),(7,8)] — полуоткрытые интервалы для batchUpdate."""
    out = []
    for i in sorted(idxs):
        if out and i == out[-1][1]:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1])
    return [(a, b) for a, b in out]


_SEP_CACHE = {}


def formula_sep(sid, sa):
    """Волна I: разделитель аргументов формул по локали документа
    (en_US -> «,», ru_RU -> «;») — USER_ENTERED парсится локалью."""
    if sid in _SEP_CACHE:
        return _SEP_CACHE[sid]
    try:
        loc = (g.gjson("GET", f"{SHEETS}/{sid}", sa_path=sa, timeout=30,
                       fields="properties.locale")["properties"]
               .get("locale") or "")
    except Exception:
        loc = ""
    sep = ";" if loc.lower().startswith(
        ("ru", "de", "fr", "es", "it", "pl", "pt", "tr", "uk")) else ","
    _SEP_CACHE[sid] = sep
    return sep


def _push_kpi(sid, sa, sheets):
    """Волна F (285): лист «Дашборд» с формулами COUNTIF — живая витрина.
    Полностью перезаписывается при каждом /sync; включается kpi_sheet_enabled.
    Волна I: разделитель формул — по локали документа (был жёсткий «;»)."""
    if not st.cget("kpi_sheet_enabled"):
        return
    import bot_inventory as inv
    title = st.cget("kpi_sheet_name")
    main = inv.SHEET_MAIN
    rows0 = sheets.get(main) or []
    if not rows0:
        return
    hdr = rows0[0]
    s = formula_sep(sid, sa)

    def col(name):
        for i, h in enumerate(hdr):
            if str(h).startswith(name):
                return chr(ord("A") + i) if i < 26 else "A" + chr(ord("A") + i - 26)
        return None
    stat = None
    for i, h in enumerate(hdr):  # последняя колонка «Статус …»
        if str(h).startswith("Статус"):
            stat = chr(ord("A") + i) if i < 26 else "A" + chr(ord("A") + i - 26)
    ipc, macc = col("IP-адрес"), col("MAC-адрес")
    q = lambda c: f"'{main}'!{c}2:{c}"
    vals = [["KPI парка камер (обновляется при /sync)",
             time.strftime("%Y-%m-%d %H:%M")],
            ["Камер в инвентаре", f"=COUNTA({q(ipc)})" if ipc else ""],
            ["Онлайн (по статусу)",
             f'=COUNTIF({q(stat)}{s}"*нлайн*")' if stat else ""],
            ["Офлайн (по статусу)",
             f'=COUNTIF({q(stat)}{s}"*флайн*")' if stat else ""],
            ["Без MAC",
             f"=COUNTBLANK('{main}'!{macc}2:{macc}{len(rows0)})" if macc else ""],
            ["Доля онлайн",
             f'=IF(B2>0{s}ROUND(100*B3/B2{s}1)&"%"{s}"")' if stat else ""]]
    meta = g.gjson("GET", f"{SHEETS}/{sid}", sa_path=sa, timeout=30)
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if title not in existing:
        g.gjson("POST", f"{SHEETS}/{sid}:batchUpdate", sa_path=sa,
                json={"requests": [{"addSheet": {"properties": {"title": title}}}]})
    from urllib.parse import quote
    g.request("POST", f"{SHEETS}/{sid}/values/{quote(title)}:clear",
              sa_path=sa).raise_for_status()
    g.gjson("PUT", f"{SHEETS}/{sid}/values/{quote(title + '!A1')}"
                   f"?valueInputOption=USER_ENTERED",
            sa_path=sa, json={"values": vals}, timeout=60)


def fmt_requests(existing, sheets, offline):
    """I29 + шапки: список batchUpdate-запросов форматирования (и для
    диффового синка волны I — общий код)."""
    reqs = []
    for title, spid in existing.items():
        if title not in sheets:
            continue
        reqs.append({"repeatCell": {
            "range": {"sheetId": spid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": HDR_BG,
                "textFormat": {"bold": True,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
            "fields": "userEnteredFormat(textFormat,backgroundColor)"}})
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": spid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}})
    import bot_inventory as inv
    main_id = existing.get(inv.SHEET_MAIN)
    if main_id is not None and sheets.get(inv.SHEET_MAIN):
        nrows = len(sheets[inv.SHEET_MAIN])
        # сброс старой заливки данных, затем красим офлайн (если данные есть)
        reqs.append({"repeatCell": {
            "range": {"sheetId": main_id, "startRowIndex": 1, "endRowIndex": nrows},
            "cell": {}, "fields": "userEnteredFormat.backgroundColor"}})
        for a, b in _ranges(offline):
            reqs.append({"repeatCell": {
                "range": {"sheetId": main_id, "startRowIndex": a, "endRowIndex": b},
                "cell": {"userEnteredFormat": {"backgroundColor": RED}},
                "fields": "userEnteredFormat.backgroundColor"}})
    return reqs


def save_sync_state(state):
    """Состояние последнего синка (_sheets_sync.json) — общий писатель."""
    tmp = SYNC_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SYNC_STATE)


def sync():
    """I25: пуш xlsx -> существующая Google-таблица. Возвращает текст-отчёт.
    Полная перезапись значений (clear+update), ручные правки таблицы затираются.
    Волна I: обычный /sync идёт через дифф (bot_gsheets2), это — режим full."""
    if not SYNC_LOCK.acquire(blocking=False):
        return "⏳ Синхронизация уже идёт — подожди."
    t0 = time.time()
    try:
        sheets, offline = _load_xlsx()
        sid, sa = _sid(), _sa()
        meta = g.gjson("GET", f"{SHEETS}/{sid}", sa_path=sa, timeout=30,
                       fields="sheets.properties(sheetId,title)")  # 437
        existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                    for s in meta["sheets"]}
        add = [{"addSheet": {"properties": {"title": t}}}
               for t in sheets if t not in existing]
        if add:
            g.gjson("POST", f"{SHEETS}/{sid}:batchUpdate", sa_path=sa,
                    json={"requests": add})
            meta = g.gjson("GET", f"{SHEETS}/{sid}", sa_path=sa, timeout=30,
                           fields="sheets.properties(sheetId,title)")
            existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                        for s in meta["sheets"]}
        from urllib.parse import quote
        stats, total = [], 0
        for title, rows in sheets.items():
            g.request("POST", f"{SHEETS}/{sid}/values/{quote(title)}:clear",
                      sa_path=sa).raise_for_status()
            r = g.gjson("PUT",
                        f"{SHEETS}/{sid}/values/{quote(title + '!A1')}"
                        f"?valueInputOption=RAW",
                        sa_path=sa, json={"values": rows}, timeout=180)
            cells = r.get("updatedCells", 0)
            total += cells
            stats.append(f"«{title}»: {len(rows)} строк")
        reqs = fmt_requests(existing, sheets, offline)
        if reqs:
            g.gjson("POST", f"{SHEETS}/{sid}:batchUpdate", sa_path=sa,
                    json={"requests": reqs}, timeout=120)
        try:  # Волна F (285): живой KPI-лист «Дашборд» (best-effort)
            _push_kpi(sid, sa, sheets)
        except Exception:
            log_exc("sync: KPI-лист не обновился (не критично)")
        dt = time.time() - t0
        save_sync_state(
            {"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "cells": total, "offline": len(offline), "mode": "full",
             "sheets": {t: len(r) for t, r in sheets.items()}, "sec": round(dt, 1)})
        log(f"sync: {total} ячеек, офлайн {len(offline)}, {dt:.1f}s")
        return ("✅ <b>Синк xlsx → Google Sheets готов</b> (полный)\n"
                + "\n".join(stats)
                + f"\n🔴 офлайн-строк подсвечено: {len(offline)}"
                + f"\n⏱ {dt:.1f}s · ячеек {total}\n"
                + f'<a href="{sheet_url()}">Открыть таблицу</a>')
    finally:
        SYNC_LOCK.release()


def last_sync():
    try:
        with open(SYNC_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------- I30 + I31: снимок /shot -> Drive + индекс ----------
def _index_update(ip, entry):
    path = st.cget("snap_index_path")
    with _idx_lock:
        try:
            with open(path, encoding="utf-8") as f:
                idx = json.load(f)
        except Exception:
            idx = {}
        idx[ip] = entry
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def upload_snapshot(ip, data):
    """Заливка JPEG в Drive-папку снимков. Волна I: через bot_gdrive2 —
    папки по датам (420), дедуп md5 (423), appProperties (424), resumable (445);
    при любой беде — легаси-multipart в корень папки."""
    try:
        import bot_gdrive2
        return bot_gdrive2.upload_snapshot2(ip, data)
    except Exception:
        log_exc("drive: upload через bot_gdrive2 не удался — легаси multipart")
    name = f"{ip}_{datetime.datetime.now():%Y%m%d_%H%M%S}.jpg"
    meta = json.dumps({"name": name, "parents": [st.cget("drive_folder_id")]})
    mp = (b"--BND\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
          + meta.encode()
          + b"\r\n--BND\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n--BND--")
    r = g.request("POST", DRIVE_UP, sa_path=_sa(), scope=g.SCOPE_DRIVE, data=mp,
                  headers={"Content-Type": "multipart/related; boundary=BND"},
                  timeout=45)
    r.raise_for_status()
    fid = r.json()["id"]
    _index_update(ip, {"id": fid, "name": name,
                       "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    log(f"drive: снимок {name} -> {fid}")
    return fid


def upload_snapshot_async(ip, data):
    """I30: копия снимка в Drive в фоне, best-effort (бот не ждёт и не падает)."""
    if not st.cget("drive_shot_upload"):
        return

    def run():
        try:
            upload_snapshot(ip, data)
        except Exception:
            log_exc(f"drive: заливка снимка {ip} не удалась (не критично)")

    threading.Thread(target=run, daemon=True, name="drive-up").start()
