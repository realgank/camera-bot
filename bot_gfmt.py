# -*- coding: utf-8 -*-
"""Волна I — оформление живой Google-таблицы (идемпотентно, после синка):
401 диаграммы на «Дашборде» (pie онлайн/офлайн + bar по подсетям, addChart
один раз), 403 protected ranges warningOnly на синкуемых листах, 404
именованные диапазоны IP/MAC/Статус, 406 самообновляемая COUNTIF-сводка по
подсетям (данные bar-диаграммы), 407 условное форматирование свежести
«Проверка (бот)» (>24 ч жёлтая, >7 дн. красная), 408 data validation
(dropdown) для «Статус монтажа»/«Модель», 409 filter views (офлайн, без MAC,
подсети), 410 автоширина + banded rows, 411 колонка =HYPERLINK на снимок
Drive, 412 колонка =IMAGE-миниатюр (флаг sheet_image_thumbs, по умолчанию
выкл), 414 лист-шаблон «Приёмка камеры» + /accept_sheet — копия под камеру.
Всё — только запись в Google-таблицу; xlsx и камеры не трогаются."""
import json
import time
from urllib.parse import quote

import bot_state as st
import google_api as g
from bot_util import log, log_exc, esc

SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
TPL_TITLE = "Приёмка (шаблон)"
BAND1 = {"red": 1, "green": 1, "blue": 1}
BAND2 = {"red": 0.94, "green": 0.96, "blue": 1.0}
YEL = {"red": 1.0, "green": 0.95, "blue": 0.68}
REDBG = {"red": 0.98, "green": 0.72, "blue": 0.72}


def _meta_fmt(sid, sa):
    return g.gjson(
        "GET", f"{SHEETS}/{sid}", sa_path=sa, timeout=30,
        fields="sheets(properties(sheetId,title),charts(chartId,spec.title),"
               "protectedRanges(protectedRangeId,range.sheetId),"
               "bandedRanges(bandedRangeId,range.sheetId),"
               "filterViews(filterViewId,title),conditionalFormats),"
               "namedRanges(name)")


def _hdr_idx(hdr, name, prefix=False):
    for i, h in enumerate(hdr):
        s = str(h or "")
        if (prefix and s.startswith(name)) or s == name:
            return i
    return None


def _grid(sheet_id, r0=None, r1=None, c0=None, c1=None):
    rng = {"sheetId": sheet_id}
    if r0 is not None:
        rng["startRowIndex"] = r0
    if r1 is not None:
        rng["endRowIndex"] = r1
    if c0 is not None:
        rng["startColumnIndex"] = c0
    if c1 is not None:
        rng["endColumnIndex"] = c1
    return rng


def named_range_reqs(existing_names, main_id, hdr, nrows):
    """404: addNamedRange для IP/MAC/последней «Статус …» (если ещё нет)."""
    stat = None
    for i, h in enumerate(hdr):
        if str(h or "").startswith("Статус"):
            stat = i
    want = {"IP_ADDR": _hdr_idx(hdr, "IP-адрес"),
            "MAC_ADDR": _hdr_idx(hdr, "MAC-адрес"), "STATUS": stat}
    reqs = []
    for name, ci in want.items():
        if ci is None or name in existing_names:
            continue
        reqs.append({"addNamedRange": {"namedRange": {
            "name": name, "range": _grid(main_id, 1, nrows, ci, ci + 1)}}})
    return reqs


def freshness_reqs(main_id, ci, nrows):
    """407: свежесть «Проверка (бот)»: >7 дн. красная, >24 ч жёлтая."""
    from bot_gsheets2 import col_letter
    cl = col_letter(ci)
    out = []
    for days, color in ((7, REDBG), (1, YEL)):
        out.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [_grid(main_id, 1, nrows, ci, ci + 1)],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [
                {"userEnteredValue":
                 f'=AND(${cl}2<>"",NOW()-DATEVALUE(LEFT(${cl}2,10))>{days})'}]},
                "format": {"backgroundColor": color}}}}})
    return out


def validation_reqs(main_id, sheets_main, nrows):
    """408: dropdown для «Статус монтажа»/«Модель» из имеющихся значений."""
    hdr = sheets_main[0]
    reqs = []
    for col_name in ("Статус монтажа", "Модель"):
        ci = _hdr_idx(hdr, col_name)
        if ci is None:
            continue
        vals = sorted({str(r[ci]).strip() for r in sheets_main[1:]
                       if ci < len(r) and str(r[ci]).strip()})[:15]
        if not vals:
            continue
        reqs.append({"setDataValidation": {
            "range": _grid(main_id, 1, nrows, ci, ci + 1),
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
                {"userEnteredValue": v} for v in vals]},
                "showCustomUi": True, "strict": False}}})
    return reqs


def filter_view_reqs(existing_titles, main_id, hdr, nrows, ncols):
    """409: filter views «Только офлайн», «Без MAC», по подсетям."""
    stat = None
    for i, h in enumerate(hdr):
        if str(h or "").startswith("Статус"):
            stat = i
    mac = _hdr_idx(hdr, "MAC-адрес")
    ipc = _hdr_idx(hdr, "IP-адрес")
    views = []
    if stat is not None:
        views.append(("Только офлайн", stat, {
            "condition": {"type": "TEXT_CONTAINS",
                          "values": [{"userEnteredValue": "флайн"}]}}))
    if mac is not None:
        views.append(("Без MAC", mac, {"condition": {"type": "BLANK"}}))
    if ipc is not None:
        for sub in st.cget("diff_subnets") or []:
            views.append((f"Подсеть {sub}.x", ipc, {
                "condition": {"type": "TEXT_STARTS_WITH",
                              "values": [{"userEnteredValue": sub + "."}]}}))
    reqs = []
    for title, ci, crit in views:
        if title in existing_titles:
            continue
        reqs.append({"addFilterView": {"filter": {
            "title": title, "range": _grid(main_id, 0, nrows, 0, ncols),
            "criteria": {str(ci): crit}}}})
    return reqs


def chart_reqs(existing_titles, dash_id, n_subnets):
    """401: pie онлайн/офлайн (данные A3:B4 «Дашборда» из _push_kpi) и bar
    по подсетям (данные G2:H, пишутся _chart_data). Добавляются один раз."""
    reqs = []
    if "Онлайн/офлайн" not in existing_titles:
        reqs.append({"addChart": {"chart": {"spec": {
            "title": "Онлайн/офлайн",
            "pieChart": {
                "legendPosition": "RIGHT_LEGEND",
                "domain": {"sourceRange": {"sources": [_grid(dash_id, 2, 4, 0, 1)]}},
                "series": {"sourceRange": {"sources": [_grid(dash_id, 2, 4, 1, 2)]}}}},
            "position": {"overlayPosition": {"anchorCell": {
                "sheetId": dash_id, "rowIndex": 9, "columnIndex": 0}}}}}})
    if "Камеры по подсетям" not in existing_titles and n_subnets:
        reqs.append({"addChart": {"chart": {"spec": {
            "title": "Камеры по подсетям",
            "basicChart": {
                "chartType": "COLUMN", "legendPosition": "NO_LEGEND",
                "domains": [{"domain": {"sourceRange": {
                    "sources": [_grid(dash_id, 1, 1 + n_subnets, 6, 7)]}}}],
                "series": [{"series": {"sourceRange": {
                    "sources": [_grid(dash_id, 1, 1 + n_subnets, 7, 8)]}},
                    "targetAxis": "LEFT_AXIS"}]}},
            "position": {"overlayPosition": {"anchorCell": {
                "sheetId": dash_id, "rowIndex": 9, "columnIndex": 6}}}}}})
    return reqs


def _chart_data(sid, sa, main_title, hdr):
    """406: COUNTIF-сводка по подсетям (G:H «Дашборда») — сама пересчитывается."""
    import bot_sheets as sh
    from bot_gsheets2 import col_letter
    ipc = _hdr_idx(hdr, "IP-адрес")
    if ipc is None:
        return 0
    cl = col_letter(ipc)
    s = sh.formula_sep(sid, sa)
    subs = st.cget("diff_subnets") or []
    vals = [["Подсеть", "Камер"]]
    for sub in subs:
        vals.append([f"{sub}.x",
                     f"=COUNTIF('{main_title}'!{cl}2:{cl}{s}\"{sub}.*\")"])
    g.gjson("PUT", f"{SHEETS}/{sid}/values/"
            f"{quote(st.cget('kpi_sheet_name') + '!G1')}"
            f"?valueInputOption=USER_ENTERED",
            sa_path=sa, json={"values": vals}, timeout=30)
    return len(subs)


def _snap_columns(sid, sa, main_title, rows, width):
    """411 (+412 по флагу): колонки «Снимок (Drive)» и «Превью» ПРАВЕЕ данных
    xlsx (дифф-синк их не трогает). Формулы =HYPERLINK / =IMAGE по индексу."""
    import bot_sheets as sh
    from bot_gsheets2 import col_letter
    try:
        with open(st.cget("snap_index_path"), encoding="utf-8") as f:
            idx = json.load(f) or {}
    except Exception:
        idx = {}
    ipc = _hdr_idx(rows[0], "IP-адрес")
    if ipc is None or not idx:
        return []
    s = sh.formula_sep(sid, sa)
    link_col, done = [["Снимок (Drive)"]], ["411 HYPERLINK"]
    img_col = [["Превью"]]
    thumbs = bool(st.cget("sheet_image_thumbs"))
    for r in rows[1:]:
        ip = str(r[ipc]).strip() if ipc < len(r) else ""
        e = idx.get(ip) or {}
        fid = e.get("id")
        link_col.append(
            [f'=HYPERLINK("https://drive.google.com/file/d/{fid}"{s}"снимок")'
             if fid else ""])
        img_col.append(
            [f'=IMAGE("https://drive.google.com/uc?id={fid}")' if fid else ""])
    data = [{"range": f"'{main_title}'!{col_letter(width)}1",
             "values": link_col}]
    if thumbs:  # 412: тяжёлый лист — только по флагу
        data.append({"range": f"'{main_title}'!{col_letter(width + 1)}1",
                     "values": img_col})
        done.append("412 IMAGE")
    g.gjson("POST", f"{SHEETS}/{sid}/values:batchUpdate", sa_path=sa,
            json={"valueInputOption": "USER_ENTERED", "data": data}, timeout=60)
    return done


def decorate(sid, sa, sheets, offline):
    """Оформление после синка (идемпотентно). Возвращает список применённого."""
    import bot_inventory as inv
    main = sheets.get(inv.SHEET_MAIN) or []
    if not main:
        return []
    hdr = main[0]
    nrows, ncols = len(main), max(len(r) for r in main)
    meta = _meta_fmt(sid, sa)
    by_title = {s["properties"]["title"]: s for s in meta.get("sheets") or []}
    main_s = by_title.get(inv.SHEET_MAIN) or {}
    dash_s = by_title.get(st.cget("kpi_sheet_name")) or {}
    main_id = (main_s.get("properties") or {}).get("sheetId")
    dash_id = (dash_s.get("properties") or {}).get("sheetId")
    if main_id is None:
        return []
    done, reqs = [], []
    names = {nr.get("name") for nr in meta.get("namedRanges") or []}
    r = named_range_reqs(names, main_id, hdr, nrows)  # 404
    if r:
        reqs += r
        done.append("404 named")
    for title in (inv.SHEET_MAIN, "Неизвестные устройства"):  # 403
        s = by_title.get(title)
        if not s:
            continue
        spid = s["properties"]["sheetId"]
        if any(True for _p in s.get("protectedRanges") or []):
            continue
        reqs.append({"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": spid}, "warningOnly": True,
            "description": "Затирается синком бота — правь xlsx или колонку "
                           "«Комментарий»"}}})
        done.append(f"403 protect «{title}»")
    if not main_s.get("bandedRanges"):  # 410 banding
        reqs.append({"addBanding": {"bandedRange": {
            "range": _grid(main_id, 1, nrows, 0, ncols),
            "rowProperties": {"firstBandColor": BAND1,
                              "secondBandColor": BAND2}}}})
        done.append("410 banding")
    reqs.append({"autoResizeDimensions": {"dimensions": {  # 410 автоширина
        "sheetId": main_id, "dimension": "COLUMNS",
        "startIndex": 0, "endIndex": min(ncols, 26)}}})
    ci = _hdr_idx(hdr, "Проверка (бот)")
    has_fresh = any("DATEVALUE(LEFT(" in json.dumps(cf)
                    for cf in main_s.get("conditionalFormats") or [])
    if ci is not None and not has_fresh:  # 407
        reqs += freshness_reqs(main_id, ci, nrows)
        done.append("407 свежесть")
    reqs += validation_reqs(main_id, main, nrows)  # 408 (идемпотентно)
    done.append("408 validation")
    fv = {f.get("title") for f in main_s.get("filterViews") or []}
    r = filter_view_reqs(fv, main_id, hdr, nrows, ncols)  # 409
    if r:
        reqs += r
        done.append(f"409 filter×{len(r)}")
    if dash_id is not None:  # 401 + 406
        n_sub = _chart_data(sid, sa, inv.SHEET_MAIN, hdr)
        charts = {(c.get("spec") or {}).get("title")
                  for c in dash_s.get("charts") or []}
        r = chart_reqs(charts, dash_id, n_sub)
        if r:
            reqs += r
            done.append("401 charts")
        done.append("406 COUNTIF")
    if reqs:
        g.gjson("POST", f"{SHEETS}/{sid}:batchUpdate", sa_path=sa,
                json={"requests": reqs}, timeout=120)
    done += _snap_columns(sid, sa, inv.SHEET_MAIN, main, ncols)  # 411/412
    log(f"gfmt: оформление — {', '.join(done) or 'нечего делать'}")
    return done


# ---------- 414: лист-шаблон приёмки ----------
TPL_ROWS = [
    ["Приёмка камеры", ""], ["Камера (имя)", ""], ["IP", ""], ["MAC", ""],
    ["Локация", ""], ["Коммутатор / порт", ""],
    ["Фокус/обзор соответствует ТЗ", "нет"], ["ONVIF отвечает", "нет"],
    ["RTSP-поток идёт", "нет"], ["Снимок приложен (ссылка)", ""],
    ["Часы/NTP настроены", "нет"], ["Наклейка/QR на месте", "нет"],
    ["Замечания", ""], ["Принял (ФИО)", ""], ["Дата", ""]]


def _ensure_template(sid, sa):
    meta = g.gjson("GET", f"{SHEETS}/{sid}", sa_path=sa, timeout=30,
                   fields="sheets.properties(sheetId,title)")
    ids = {s["properties"]["title"]: s["properties"]["sheetId"]
           for s in meta.get("sheets") or []}
    if TPL_TITLE not in ids:
        r = g.gjson("POST", f"{SHEETS}/{sid}:batchUpdate", sa_path=sa, json={
            "requests": [{"addSheet": {"properties": {
                "title": TPL_TITLE, "gridProperties": {
                    "rowCount": 30, "columnCount": 4}}}}]})
        ids[TPL_TITLE] = r["replies"][0]["addSheet"]["properties"]["sheetId"]
        g.gjson("PUT", f"{SHEETS}/{sid}/values/{quote(TPL_TITLE + '!A1')}"
                       f"?valueInputOption=RAW",
                sa_path=sa, json={"values": TPL_ROWS}, timeout=30)
    return ids[TPL_TITLE]


def cmd_accept_sheet(chat, arg="", reply_to=None):
    """414: /accept_sheet <ip|имя> — копия листа приёмки под камеру."""
    from bot_tg import send, chat_action
    import bot_inventory as inv
    a = (arg or "").strip()
    if not a:
        send(chat, "Лист приёмки: <code>/accept_sheet 10.20.50.51</code> "
                   "или по имени — создам копию шаблона в Google-таблице.",
             reply_to=reply_to)
        return
    recs = inv.search(a)
    rec = recs[0] if recs else {}
    label = rec.get("name") or a
    chat_action(chat)
    sid, sa = st.cget("sheet_id"), st.cget("sa_path")
    tpl = _ensure_template(sid, sa)
    title = f"Приёмка {label} {time.strftime('%d.%m')}"[:90]
    r = g.gjson("POST", f"{SHEETS}/{sid}:batchUpdate", sa_path=sa, json={
        "requests": [{"duplicateSheet": {
            "sourceSheetId": tpl, "newSheetName": title}}]})
    new_id = r["replies"][0]["duplicateSheet"]["properties"]["sheetId"]
    fill = [["Приёмка камеры", time.strftime("%Y-%m-%d")],
            ["Камера (имя)", rec.get("name") or ""], ["IP", rec.get("ip") or a],
            ["MAC", rec.get("mac") or ""], ["Локация", rec.get("location") or ""],
            ["Коммутатор / порт",
             f"{rec.get('switch') or ''} п.{rec.get('port') or ''}".strip(" п.")]]
    g.gjson("PUT", f"{SHEETS}/{sid}/values/{quote(title + '!A1')}"
                   f"?valueInputOption=RAW",
            sa_path=sa, json={"values": fill}, timeout=30)
    url = (f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid={new_id}")
    send(chat, f"📋 Лист приёмки создан: <a href=\"{url}\">{esc(title)}</a>",
         reply_to=reply_to)


def cmd_sheet_fmt(chat, arg="", reply_to=None):
    """Оформить таблицу сейчас (401/403-412), без синка данных."""
    from bot_tg import send, chat_action
    import bot_sheets as sh
    chat_action(chat)
    send(chat, "🎨 Оформляю таблицу …", silent=True, reply_to=reply_to)
    try:
        sheets, offline = sh._load_xlsx()
        done = decorate(st.cget("sheet_id"), st.cget("sa_path"), sheets, offline)
        send(chat, "🎨 Готово: " + (esc(", ".join(done)) if done else
                                    "нечего применять"), reply_to=reply_to)
    except Exception as e:
        from bot_util import human_err
        log_exc("/sheet_fmt")
        send(chat, human_err("Оформление упало", e), reply_to=reply_to)


HANDLERS = {"/sheet_fmt": cmd_sheet_fmt, "/accept_sheet": cmd_accept_sheet}
ALIASES = {"/оформи_таблицу": "/sheet_fmt", "/приёмка_лист": "/accept_sheet"}
CALLBACKS = {}
