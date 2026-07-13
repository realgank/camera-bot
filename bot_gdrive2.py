# -*- coding: utf-8 -*-
"""Волна I — Google Drive: 420 подпапки снимков по датам (find-or-create,
444), 421 ротация старых снимков (план + удаление ТОЛЬКО по двухшаговой
кнопке), 422 миниатюра = thumbnailLink Drive (Pillow не ставим — свой thumb
не жмём, честно берём предпросмотр Drive), 423 дедуп заливок по md5,
424 appProperties {ip, name, ts, md5}, 425 /snaps — история снимков камеры
Drive-запросом, 426 снапшот-копия таблицы перед большим синком,
427 ротация снапшот-копий (последние N + по одной на месяц), 428
keepRevisionForever для бэкапов xlsx, 440 /ga_health — здоровье
сервис-аккаунта, 442 fail-fast проверка доступов при старте, 445 resumable
upload (google_api), 447 ночной бэкап xlsx в _backups/ГГГГ-ММ.
Удаления в Drive — только с подтверждением; тесты ничего не удаляют."""
import os
import time
import json
import hashlib
import datetime
import threading
import subprocess

import bot_state as st
import bot_store as store
import google_api as g
from bot_util import log, log_exc, esc, human_err

DRIVE = "https://www.googleapis.com/drive/v3"
_PEND = {}      # token -> (ts, kind, [file dicts]) — подтверждение удаления
_pend_lock = threading.Lock()


def _sa():
    return st.cget("sa_path")


def _spath():
    return st.cget("ga_state_path")


def files_list(q, fields="files(id,name,createdTime,size,md5Checksum)",
               page_size=200, order=None):
    params = {"q": q, "pageSize": page_size, "fields": fields}
    if order:
        params["orderBy"] = order
    j = g.gjson("GET", f"{DRIVE}/files", sa_path=_sa(), scope=g.SCOPE_DRIVE,
                params=params, timeout=45)
    return j.get("files") or []


def find_or_create_folder(name, parent):
    """444: папка по имени+parent, создаётся только при отсутствии."""
    safe = name.replace("'", "\\'")
    got = files_list(f"name='{safe}' and '{parent}' in parents and "
                     f"mimeType='application/vnd.google-apps.folder' "
                     f"and trashed=false", fields="files(id,name)")
    if got:
        return got[0]["id"]
    j = g.gjson("POST", f"{DRIVE}/files", sa_path=_sa(), scope=g.SCOPE_DRIVE,
                json={"name": name, "parents": [parent],
                      "mimeType": "application/vnd.google-apps.folder"},
                timeout=30, fields="id")
    log(f"drive: создана папка «{name}» -> {j['id']}")
    return j["id"]


def date_folder():
    """420: подпапка сегодняшней даты в папке снимков (кэш в состоянии)."""
    day = datetime.date.today().isoformat()
    s = store.jload(_spath(), {})
    cached = (s.get("date_folders") or {}).get(day)
    if cached:
        return cached
    fid = find_or_create_folder(day, st.cget("drive_folder_id"))

    def upd(d):
        d.setdefault("date_folders", {})
        d["date_folders"] = {k: v for k, v in d["date_folders"].items()
                             if k >= (datetime.date.today()
                                      - datetime.timedelta(days=3)).isoformat()}
        d["date_folders"][day] = fid
        return d
    store.jupdate(_spath(), {}, upd)
    return fid


def upload_snapshot2(ip, data):
    """Волна I: снимок -> Drive. 423 дедуп md5, 420 папка дня, 424
    appProperties, 445 resumable, 422 thumbnailLink в индексе."""
    import bot_sheets
    md5 = hashlib.md5(data).hexdigest()
    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if st.cget("drive_dedup_md5"):  # 423
        dup = files_list(f"appProperties has {{ key='md5' and value='{md5}' }} "
                         f"and trashed=false", fields="files(id,name)")
        if dup:
            log(f"drive: снимок {ip} md5-дубликат {dup[0]['id']} — не заливаю")
            bot_sheets._index_update(ip, {"id": dup[0]["id"],
                                          "name": dup[0]["name"], "ts": now_s})
            return dup[0]["id"]
    parent = date_folder() if st.cget("drive_date_folders") \
        else st.cget("drive_folder_id")
    name = f"{ip}_{datetime.datetime.now():%Y%m%d_%H%M%S}.jpg"
    j = g.upload_resumable(  # 445
        name, data, parents=[parent], mime="image/jpeg", sa_path=_sa(),
        app_properties={"ip": ip, "name": name, "ts": now_s, "md5": md5},
        fields="id,md5Checksum,thumbnailLink")
    fid = j.get("id")
    bot_sheets._index_update(ip, {"id": fid, "name": name, "ts": now_s,
                                  "thumb": j.get("thumbnailLink") or ""})  # 422
    log(f"drive: снимок {name} -> {fid} (resumable)")
    return fid


# ---------- 425: /snaps ----------
def snaps_for(ip):
    got = files_list(f"appProperties has {{ key='ip' and value='{ip}' }} "
                     f"and trashed=false",
                     fields="files(id,name,createdTime,size)",
                     order="createdTime desc")
    if not got:  # легаси-файлы без appProperties — по префиксу имени
        got = files_list(f"name contains '{ip}_' and trashed=false "
                         f"and mimeType='image/jpeg'",
                         fields="files(id,name,createdTime,size)",
                         order="createdTime desc")
    return got


def cmd_snaps(chat, arg="", reply_to=None):
    from bot_tg import send, chat_action
    import bot_net as net
    import bot_inventory as inv
    a = (arg or "").strip()
    ip = a if net.valid_ip(a) else (inv.resolve_ip(a) if a else None)
    if not ip:
        send(chat, "Снимки камеры в Drive: <code>/snaps 10.20.50.51</code> "
                   "или по имени.", reply_to=reply_to)
        return
    chat_action(chat)
    got = snaps_for(ip)
    if not got:
        send(chat, f"📭 В Drive нет снимков <code>{ip}</code>.", reply_to=reply_to)
        return
    lines = [f"📸 <b>Снимки {esc(inv.label(ip) or ip)} в Drive</b> "
             f"({len(got)}):"]
    for f in got[:15]:
        kb_size = int(f.get("size") or 0) // 1024
        lines.append(f"• <a href=\"https://drive.google.com/file/d/{f['id']}\">"
                     f"{esc((f.get('createdTime') or '')[:16].replace('T', ' '))}"
                     f"</a> · {kb_size} КБ")
    if len(got) > 15:
        lines.append(f"… и ещё {len(got) - 15}")
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- 426/427: снапшот-копии таблицы ----------
def snapshot_spreadsheet():
    """426: files.copy таблицы в _snapshots/ с именем-датой."""
    sid = st.cget("sheet_id")
    folder = find_or_create_folder("_snapshots", st.cget("drive_folder_id"))
    name = f"snapshot_{datetime.datetime.now():%Y%m%d_%H%M%S}_Все_камеры"
    j = g.gjson("POST", f"{DRIVE}/files/{sid}/copy", sa_path=_sa(),
                scope=g.SCOPE_DRIVE, timeout=90, fields="id,name",
                json={"name": name, "parents": [folder]})
    log(f"drive: снапшот таблицы {j.get('name')} -> {j.get('id')}")
    return j


def snapshot_rotation_plan():
    """427: (оставить, удалить): последние N + по одной на месяц."""
    folder = find_or_create_folder("_snapshots", st.cget("drive_folder_id"))
    snaps = files_list(f"'{folder}' in parents and trashed=false",
                       fields="files(id,name,createdTime)",
                       order="createdTime desc")
    keep_n = int(st.cget("drive_table_snapshots"))
    keep, drop, monthly = [], [], set()
    for i, f in enumerate(snaps):
        month = (f.get("createdTime") or "")[:7]
        if i < keep_n or month not in monthly:
            keep.append(f)
            monthly.add(month)
        else:
            drop.append(f)
    return keep, drop


def cmd_gsnap(chat, arg="", reply_to=None):
    """/gsnap — список снапшот-копий; /gsnap new — сделать; ротация по кнопке."""
    from bot_tg import send, chat_action
    chat_action(chat)
    if (arg or "").strip().lower() == "new":
        j = snapshot_spreadsheet()
        send(chat, f"📸 Снапшот-копия создана: <code>{esc(j.get('name'))}</code>",
             reply_to=reply_to)
        return
    keep, drop = snapshot_rotation_plan()
    lines = [f"🗃 <b>Снапшот-копии таблицы</b>: {len(keep) + len(drop)} шт. "
             f"(хранить {st.cget('drive_table_snapshots')} + по 1 на месяц)"]
    for f in keep[:10]:
        lines.append(f"✅ {esc(f['name'])}")
    for f in drop[:10]:
        lines.append(f"🗑 {esc(f['name'])}")
    lines.append("Создать сейчас: /gsnap new")
    kb = None
    if drop:
        tok = str(int(time.time()))
        with _pend_lock:
            _PEND.clear()
            _PEND[tok] = (time.time(), "snap", drop)
        kb = {"inline_keyboard": [[
            {"text": f"🗑 Удалить {len(drop)} старых…",
             "callback_data": f"gsnrot:{tok}"},
            {"text": "✖️ Отмена", "callback_data": "cancel"}]]}
    send(chat, "\n".join(lines), markup=kb, reply_to=reply_to)


def cb_gsnrot(chat, cq, tok):
    from bot_tg import send, answer_cq, edit_message
    with _pend_lock:
        e = _PEND.get(tok)
    if not e or time.time() - e[0] > 300:
        answer_cq(cq.get("id"), "⌛ Список устарел — повтори /gsnap")
        return
    answer_cq(cq.get("id"), "Нужно финальное подтверждение")
    mid = (cq.get("message") or {}).get("message_id")
    txt = (f"⚠️ <b>Финальное подтверждение</b>: удалить {len(e[2])} "
           f"файлов из Drive (в корзину)?")
    kb = {"inline_keyboard": [[
        {"text": "✅ Да, удалить", "callback_data": f"gsnroty:{tok}"},
        {"text": "✖️ Отмена", "callback_data": "cancel"}]]}
    (edit_message(chat, mid, txt, markup=kb) if mid
     else send(chat, txt, markup=kb))


def cb_gsnroty(chat, cq, tok):
    from bot_tg import send, answer_cq
    with _pend_lock:
        e = _PEND.pop(tok, None)
    if not e or time.time() - e[0] > 300:
        answer_cq(cq.get("id"), "⌛ Подтверждение устарело")
        return
    answer_cq(cq.get("id"), "🗑 Удаляю…")
    ok = err = 0
    for f in e[2]:
        try:  # в корзину (trashed), не безвозвратно
            g.gjson("PATCH", f"{DRIVE}/files/{f['id']}", sa_path=_sa(),
                    scope=g.SCOPE_DRIVE, json={"trashed": True}, timeout=30)
            ok += 1
        except Exception:
            err += 1
    log(f"drive: ротация {e[1]}: в корзину {ok}, ошибок {err}")
    send(chat, f"🗑 В корзину Drive отправлено {ok}"
               + (f", ошибок {err}" if err else "") + ".")


# ---------- 421: ротация снимков (план + кнопка) ----------
def old_snaps_plan():
    """Снимки старше N дней (кроме последнего по каждой камере) — кандидаты."""
    cut = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=float(st.cget("drive_snap_keep_days"))))
    cut_s = cut.strftime("%Y-%m-%dT%H:%M:%S")
    old = files_list(f"'{st.cget('drive_folder_id')}' in parents and "
                     f"mimeType='image/jpeg' and trashed=false and "
                     f"createdTime < '{cut_s}'",
                     fields="files(id,name,createdTime)", page_size=500)
    try:
        with open(st.cget("snap_index_path"), encoding="utf-8") as f:
            latest = {e.get("id") for e in (json.load(f) or {}).values()}
    except Exception:
        latest = set()
    return [f for f in old if f["id"] not in latest]


def cmd_snaprotate(chat, arg="", reply_to=None):
    from bot_tg import send, chat_action
    chat_action(chat)
    drop = old_snaps_plan()
    if not drop:
        send(chat, f"🧹 Снимков старше {st.cget('drive_snap_keep_days')} дн. "
                   f"в корне папки нет (подпапки дат не трогаю).",
             reply_to=reply_to)
        return
    tok = str(int(time.time()))
    with _pend_lock:
        _PEND.clear()
        _PEND[tok] = (time.time(), "old_snaps", drop)
    send(chat, f"🧹 Старых снимков (>{st.cget('drive_snap_keep_days')} дн., "
               f"кроме последних по камерам): <b>{len(drop)}</b>.\n"
               f"Удалять только по подтверждению.",
         markup={"inline_keyboard": [[
             {"text": f"🗑 Удалить {len(drop)}…", "callback_data": f"gsnrot:{tok}"},
             {"text": "✖️ Отмена", "callback_data": "cancel"}]]},
         reply_to=reply_to)


# ---------- 428 + 447: бэкап xlsx ----------
def keep_revision(fid):
    """428: пометить последнюю ревизию keepRevisionForever."""
    j = g.gjson("GET", f"{DRIVE}/files/{fid}/revisions", sa_path=_sa(),
                scope=g.SCOPE_DRIVE, fields="revisions(id)", timeout=30)
    revs = j.get("revisions") or []
    if revs:
        g.gjson("PATCH", f"{DRIVE}/files/{fid}/revisions/{revs[-1]['id']}",
                sa_path=_sa(), scope=g.SCOPE_DRIVE,
                json={"keepRevisionForever": True}, timeout=30)


def backup_xlsx():
    """447: Все_камеры.xlsx -> _backups/ГГГГ-ММ/ (дедуп по md5, 428).
    Возвращает текст результата."""
    import bot_inventory as inv
    path = inv.inv_path()
    with open(path, "rb") as f:
        data = f.read()
    md5 = hashlib.md5(data).hexdigest()
    s = store.jload(_spath(), {})
    if s.get("last_backup_md5") == md5:
        return "xlsx не менялся — бэкап не нужен"
    backups = find_or_create_folder("_backups", st.cget("drive_folder_id"))
    month = find_or_create_folder(datetime.date.today().strftime("%Y-%m"),
                                  backups)
    name = f"Все_камеры_{datetime.datetime.now():%Y%m%d_%H%M%S}.xlsx"
    j = g.upload_resumable(name, data, parents=[month], sa_path=_sa(),
                           mime="application/vnd.openxmlformats-officedocument"
                                ".spreadsheetml.sheet",
                           app_properties={"md5": md5, "kind": "inv_backup"})
    try:
        keep_revision(j.get("id"))  # 428
    except Exception:
        log_exc("drive: keepRevisionForever (не критично)")
    store.jupdate(_spath(), {}, lambda d: {**d, "last_backup_md5": md5,
                                           "last_backup_ts": time.time(),
                                           "last_backup_id": j.get("id")})
    log(f"drive: бэкап xlsx {name} -> {j.get('id')}")
    return f"залит {name} ({len(data) // 1024} КБ)"


# ---------- 416: PDF-отчёт ----------
def cmd_report_pdf(chat, arg="", reply_to=None):
    from bot_tg import send, send_document, chat_action
    chat_action(chat, "upload_document")
    sid, sa = st.cget("sheet_id"), _sa()
    try:
        meta = g.gjson("GET", f"https://sheets.googleapis.com/v4/spreadsheets/"
                       f"{sid}", sa_path=sa, timeout=30,
                       fields="sheets.properties(sheetId,title)")
        want = (arg or "").strip() or st.cget("kpi_sheet_name")
        gid = next((s["properties"]["sheetId"] for s in meta["sheets"]
                    if s["properties"]["title"] == want),
                   meta["sheets"][0]["properties"]["sheetId"])
        r = g.request("GET", f"https://docs.google.com/spreadsheets/d/{sid}/"
                      f"export?format=pdf&gid={gid}&portrait=false"
                      f"&fitw=true&gridlines=false",
                      sa_path=sa, scope=g.SCOPE_ALL, timeout=90)
        r.raise_for_status()
        fn = f"отчет_{want}_{time.strftime('%Y%m%d')}.pdf"
        send_document(chat, r.content, fn,
                      caption=f"📄 «{want}» · {len(r.content) // 1024} КБ")
    except Exception as e:
        log_exc("/report_pdf")
        send(chat, human_err("PDF-экспорт не удался", e), reply_to=reply_to)


# ---------- 440 + 442 + 449: здоровье SA ----------
def probe_task_status():
    """449: статус задачи планировщика camera_bot_probe (или строка ошибки)."""
    tn = st.cget("probe_task_name")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", tn, "/FO", "LIST"],
                           capture_output=True, timeout=15, creationflags=flags)
        if r.returncode != 0:
            return f"задача {tn} НЕ зарегистрирована"
        out = r.stdout.decode("cp866", errors="replace")
        status = next((ln.split(":", 1)[1].strip() for ln in out.splitlines()
                       if "Status" in ln or "Состояние" in ln), "?")
        return f"{tn}: {status}"
    except Exception as e:
        return f"schtasks: {type(e).__name__}"


def ga_health_text():
    """440: токен, чтение таблицы, квота Drive, возраст ключа, probe-задача."""
    sa = _sa()
    lines = ["☁️ <b>Google-здоровье</b>"]
    try:
        g.token(sa_path=sa)
        lines.append("✅ токен выдаётся")
    except Exception as e:
        lines.append(f"❌ токен: {esc(type(e).__name__)}: {esc(str(e)[:80])}")
        return "\n".join(lines)
    try:
        meta = g.gjson("GET", "https://sheets.googleapis.com/v4/spreadsheets/"
                       + st.cget("sheet_id"), sa_path=sa, timeout=30,
                       fields="properties.title,sheets.properties.title")
        lines.append(f"✅ таблица читается: "
                     f"«{esc((meta.get('properties') or {}).get('title') or '?')}»"
                     f" · листов {len(meta.get('sheets') or [])}")
    except Exception as e:
        lines.append(f"❌ таблица: {esc(str(e)[:100])} — расшарь на "
                     f"<code>{esc(g.sa_email(sa))}</code>")
    try:
        q = g.gjson("GET", f"{DRIVE}/about", sa_path=sa, scope=g.SCOPE_DRIVE,
                    fields="storageQuota", timeout=30).get("storageQuota") or {}
        used = int(q.get("usage") or 0) / 1073741824
        lim = int(q.get("limit") or 0) / 1073741824
        pct = f" ({100 * used / lim:.0f}%)" if lim else ""
        lines.append(f"💾 квота Drive: {used:.2f} / {lim:.0f} ГБ{pct}")
    except Exception:
        lines.append("⚠️ квота Drive не прочиталась")
    try:
        age = (time.time() - os.path.getmtime(sa)) / 86400
        maxd = float(st.cget("sa_key_max_age_days"))
        mark = "✅" if age < maxd else "⚠️"
        lines.append(f"{mark} ключу SA {age:.0f} дн. (порог {maxd:.0f}; "
                     f"ротация — /gcal remind)")
    except OSError:
        lines.append("⚠️ файл ключа не найден")
    lines.append(f"⏰ {esc(probe_task_status())}")
    try:
        import bot_sheets as sh
        ls = sh.last_sync()
        lines.append(f"🔄 последний синк: {esc((ls or {}).get('ts') or 'не было')}"
                     + (f" · {ls.get('mode')}" if ls and ls.get("mode") else ""))
    except Exception:
        pass
    return "\n".join(lines)


def cmd_ga_health(chat, arg="", reply_to=None):
    from bot_tg import send, chat_action
    chat_action(chat)
    send(chat, ga_health_text(), reply_to=reply_to)


def startup_check():
    """442: при старте бота (фоном) — SA видит таблицу и папку снимков."""
    if not st.cget("ga_check_on_start"):
        return

    def run():
        problems = []
        try:
            g.gjson("GET", "https://sheets.googleapis.com/v4/spreadsheets/"
                    + st.cget("sheet_id"), sa_path=_sa(), timeout=30,
                    fields="properties.title")
        except Exception as e:
            problems.append(f"таблица: {type(e).__name__} {str(e)[:80]}")
        try:
            g.gjson("GET", f"{DRIVE}/files/{st.cget('drive_folder_id')}",
                    sa_path=_sa(), scope=g.SCOPE_DRIVE, timeout=30,
                    fields="id,name")
        except Exception as e:
            problems.append(f"папка снимков: {type(e).__name__} {str(e)[:80]}")
        if problems:
            log("ga: СТАРТ-ПРОВЕРКА ДОСТУПОВ ПРОВАЛЕНА: " + "; ".join(problems))
            try:
                import bot_metrics as mx
                mx.owner_alert(
                    "☁️❌ <b>Google-доступы сломаны</b> (/sync упадёт):\n"
                    + "\n".join("• " + esc(p) for p in problems)
                    + f"\nРасшарь на <code>{esc(g.sa_email(_sa()))}</code> "
                      f"и проверь /ga_health", aid="gapi_broken")
            except Exception:
                pass
        else:
            log("ga: старт-проверка доступов OK (таблица + папка снимков)")

    threading.Thread(target=run, daemon=True, name="ga-check").start()


HANDLERS = {"/snaps": cmd_snaps, "/ga_health": cmd_ga_health,
            "/report_pdf": cmd_report_pdf, "/gsnap": cmd_gsnap,
            "/snaprotate": cmd_snaprotate}
ALIASES = {"/снимки": "/snaps"}
CALLBACKS = {"gsnrot": cb_gsnrot, "gsnroty": cb_gsnroty}
