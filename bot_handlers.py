# -*- coding: utf-8 -*-
"""Обработчики команд и колбэков бота камер МФК «Зарядье».
Бот НИКОГДА не меняет пароли камер.
Волна A: R10-R11, R26, R39-R41, R49, U2, U3, U20-U22, U25, U26, U33,
U36 (fuzzy), U37 (алиасы), U41, U48.
Волна B: I4 (диагностика с инвентарём), I5/U11 (/shot: имя+локация и снимок
по имени), I6 (/find: известная/НОВАЯ), I30 (копия снимка в Drive), I34
(«MAC сменился» в /diag); новые команды — в bot_handlers_inv (подключаются внизу).
"""
import os
import time
import difflib
import logging
import datetime
import threading

import bot_state as st
import bot_net as net
import bot_inventory as inv
from bot_tg import (send, send_photo, send_document,
                    chat_action, answer_cq, tg_ping)
from bot_util import log, log_exc, esc, human_err, mem_mb, BOT_VERSION, LOG_PATH
from onvif_snap import device_info, get_snapshot


def ip_card_text(ip):
    """Карточка по голому IP (для camera_bot): инвентарное имя, локация, свитч."""
    try:
        return inv.ip_card_text(ip)
    except Exception:
        log_exc("ip_card_text")
        return f"Что сделать с <code>{ip}</code>?"


def _resolve_cam(arg):
    """U11/I8: если аргумент не IP — ищем камеру по имени/MAC в инвентаре."""
    a = (arg or "").strip()
    if net.valid_ip(a):
        return a
    try:
        return inv.resolve_ip(a)
    except Exception:
        log_exc("_resolve_cam")
        return None

# ---------- Клавиатуры ----------
MAIN_KB = {
    "keyboard": [
        ["📸 Снимок", "🩺 Диагностика", "ℹ️ Инфо"],
        ["🔍 Поиск камер"],
        ["📊 Статус", "🏓 Пинг", "❓ Помощь"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}
BTN_ASK = {"📸 Снимок": "shot", "🩺 Диагностика": "diag", "ℹ️ Инфо": "info"}
BTN_CMD = {"🔍 Поиск камер": "/find", "📊 Статус": "/status",
           "🏓 Пинг": "/ping", "❓ Помощь": "/help"}
ACTION_LABEL = {"shot": "📸 снимок", "diag": "🩺 диагностику", "info": "ℹ️ инфо"}
CANCEL_KB = {"inline_keyboard": [[{"text": "✖️ Отмена", "callback_data": "cancel"}]]}


def actions_kb(ip, with_orig=False):
    """U3: инлайн-кнопки действий по IP; with_orig — плюс «Оригинал» (U41)."""
    rows = [[{"text": "📸 Снимок", "callback_data": f"shot:{ip}"},
             {"text": "🩺 Диаг", "callback_data": f"diag:{ip}"},
             {"text": "ℹ️ Инфо", "callback_data": f"info:{ip}"}]]
    if with_orig:
        rows.append([{"text": "📎 Оригинал (без сжатия)", "callback_data": f"orig:{ip}"}])
    try:  # Волна D (192): если есть эталон обзора — кнопка сравнения
        import bot_dossier
        if bot_dossier.has_baseline(ip):
            rows.append([{"text": "⚖️ С эталоном", "callback_data": f"bline:{ip}"}])
    except Exception:
        pass
    return {"inline_keyboard": rows}


def fail_kb(action, ip, try8080=False):
    """U21: «Повторить» под ошибкой; U22: попробовать ONVIF на 8080."""
    row = [{"text": "🔄 Повторить", "callback_data": f"{action}:{ip}"}]
    if try8080:
        row.append({"text": "🔁 Порт 8080", "callback_data": f"{action}@8080:{ip}"})
    return {"inline_keyboard": [row]}


def ask_ip(chat, action):
    """Режим ожидания IP + пресеты + кнопка отмены (U25)."""
    st.set_pending(chat, action)
    ips = list(dict.fromkeys(list(st.RECENT) + list(st.cget("presets"))))[:8]
    rows = [[{"text": f"📷 {ip}", "callback_data": f"{action}:{ip}"}] for ip in ips]
    rows.append([{"text": "✖️ Отмена", "callback_data": "cancel"}])
    send(chat, f"Пришли IP камеры для {ACTION_LABEL[action]} — "
               f"или выбери из последних/пресетов:",
         markup={"inline_keyboard": rows})


# ---------- Команды ----------
def cmd_start(chat, arg="", reply_to=None):
    """U2: онбординг-тур; U43: deep-link /start shot_10-20-50-1."""
    a = (arg or "").strip()
    if "_" in a:  # U43: t.me/бот?start=shot_10-20-50-1
        action, raw = a.split("_", 1)
        if action == "cam":  # Волна D (175): QR-наклейка -> карточка камеры
            import bot_qr
            log(f"deep-link: cam {raw}")
            bot_qr.start_cam(chat, raw)
            return
        ip = raw.replace("-", ".")
        if action in ACTION_FN and net.valid_ip(ip):
            log(f"deep-link: {action} {ip}")
            run_action(chat, action, ip)
            return
    presets = list(st.cget("presets"))
    test_ip = presets[-1] if presets else "10.20.52.100"
    send(chat,
         "👋 <b>Бот диагностики ~600 IP-камер МФК «Зарядье»</b>\n\n"
         "1️⃣ Пришли IP камеры — предложу снимок/диагностику/инфо\n"
         "2️⃣ Кнопки внизу — основные действия\n"
         "3️⃣ /find [подсеть] — просканирует и найдёт камеры\n"
         "4️⃣ /help — полный список команд\n\n"
         "🔒 Пароли камер бот никогда не меняет.", markup=MAIN_KB, reply_to=reply_to)
    send(chat, "Попробуй прямо сейчас:", silent=True,
         markup={"inline_keyboard": [[
             {"text": f"📸 Снимок с тестовой {test_ip}",
              "callback_data": f"shot:{test_ip}"}]]})


def cmd_shot(chat, arg="", reply_to=None, port=80):
    ips = net.find_ips(arg)
    if len(ips) >= 2:  # U10: несколько IP -> альбом sendMediaGroup
        import bot_handlers_media as _m
        _m._send_album(chat, ips, reply_to=reply_to)
        return
    ip = _resolve_cam(arg)  # U11: /shot по имени камеры
    if not ip:  # R27
        send(chat, "Укажи IP или имя: <code>/shot 10.20.51.50</code>, "
                   "<code>/shot AS-7C.01</code>", reply_to=reply_to)
        return
    st.remember_ip(ip)
    chat_action(chat, "upload_photo")  # R11
    psuf = "" if port == 80 else f" (порт {port})"
    send(chat, f"📸 Снимаю кадр с <code>{ip}</code>{psuf} …",
         reply_to=reply_to, silent=True)
    t0 = time.time()
    data, msg = get_snapshot(ip, port=port, user=st.CAM_USER, pwd=st.CAM_PASS)
    dt = time.time() - t0
    if data:
        # U42: штамп времени и размера в подписи
        cap = (f"✅ {ip} — {len(data) // 1024} КБ, за {dt:.1f}s · "
               f"🕒 {datetime.datetime.now():%d.%m.%Y %H:%M:%S}")
        rec = inv.get(ip)  # I5: имя + локация + свитч/порт в подписи
        if rec:
            bits = [str(rec[k]) for k in ("name", "location") if rec.get(k)]
            if rec.get("switch") or rec.get("port"):
                bits.append(f"{rec.get('switch') or '?'} п.{rec.get('port') or '?'}")
            if bits:
                cap += "\n📒 " + " · ".join(bits)
        else:
            cap += "\n🆕 в инвентаре не найдена"
        send_photo(chat, data, caption=cap,
                   markup=actions_kb(ip, with_orig=True))  # U3 + U41
        try:  # I30: копия снимка в Drive (фон, best-effort)
            import bot_sheets
            bot_sheets.upload_snapshot_async(ip, data)
        except Exception:
            log_exc("shot: drive upload не запустился")
    else:  # U48-шаблон + U21 повтор + U22 порт 8080
        send(chat, human_err(f"Не удалось снять кадр с <code>{ip}</code> (за {dt:.1f}s)", msg),
             markup=fail_kb("shot", ip, try8080=(port != 8080)))


def cmd_orig(chat, arg="", reply_to=None, port=80):
    """U41: снимок документом — без пересжатия Telegram."""
    ip = (arg or "").strip()
    if not net.valid_ip(ip):
        return
    chat_action(chat, "upload_document")
    data, msg = get_snapshot(ip, port=port, user=st.CAM_USER, pwd=st.CAM_PASS)
    if data:
        fn = f"{ip}_{datetime.datetime.now():%Y%m%d_%H%M%S}.jpg"
        send_document(chat, data, fn, caption=f"📎 {ip} — оригинал, {len(data) // 1024} КБ")
    else:
        send(chat, human_err(f"Оригинал с <code>{ip}</code> не получен", msg),
             markup=fail_kb("orig", ip, try8080=(port != 8080)))


def cmd_diag(chat, arg="", reply_to=None, port=80):
    ip = _resolve_cam(arg)
    if not ip:
        send(chat, "Укажи IP или имя: <code>/diag 192.168.0.250</code>", reply_to=reply_to)
        return
    st.remember_ip(ip)
    chat_action(chat)  # R11
    send(chat, f"🩺 Диагностика <code>{ip}</code> …", reply_to=reply_to, silent=True)
    t0 = time.time()
    alive = net.ping(ip) is not None
    mac = net.arp_table().get(ip, "—")
    v = net.vendor(mac) if mac != "—" else "—"
    ports = net.open_ports(ip, ports=tuple(st.cget("diag_ports")))
    # порт для ONVIF: явный (из кнопки 8080) либо авто по открытым
    oport = port if port != 80 else (80 if 80 in ports else (8080 if 8080 in ports else None))
    if oport:
        info = device_info(ip, port=oport, user=st.CAM_USER, pwd=st.CAM_PASS)
    else:
        info = {"error": "порты ONVIF закрыты"}
    if info.get("model"):
        onvif_s = f"{esc(info['manufacturer'])} {esc(info['model'])}"
        inv.note_onvif(ip, info)  # I48: копим прошивки
    else:
        onvif_s = esc(info.get("error"))
    # I4: обогащение инвентарём (что это и где стоит) + I34 «MAC сменился»
    rec = inv.get(ip)
    if rec:
        bits = [f"<b>{esc(rec['name'])}</b>" if rec.get("name") else None,
                esc(rec.get("location")) if rec.get("location") else None,
                (f"{esc(rec.get('switch') or '?')} п.{esc(rec.get('port') or '?')}"
                 if rec.get("switch") or rec.get("port") else None)]
        inv_s = "📒 " + " · ".join(b for b in bits if b)
        if mac != "—" and rec.get("nmac") and inv.norm_mac(mac) != rec["nmac"]:
            inv_s += (f"\n⚠️ <b>MAC сменился</b>: в инвентаре "
                      f"<code>{esc(rec.get('mac'))}</code>")
    else:
        inv_s = "📒 🆕 в инвентаре не найдена"
    # U28: моно-таблица <pre> с иконками портов
    picons = " ".join(f"{p}{'🟢' if p in ports else '·'}"
                      for p in st.cget("diag_ports"))
    table = (f"ping   {'OK' if alive else '--'}\n"
             f"MAC    {mac}  {v}\n"
             f"порты  {picons}")
    txt = (f"<b>{ip}</b> {'✅' if alive or ports else '❌'}\n"
           f"{inv_s}\n"
           f"<pre>{esc(table)}</pre>"
           f"ONVIF: {onvif_s}\n"
           f"⏱ проверено за {time.time() - t0:.1f}s")
    kb = None
    if not info.get("model") and 8080 in ports and oport != 8080:  # U22
        kb = {"inline_keyboard": [[
            {"text": "🔁 ONVIF на 8080", "callback_data": f"diag@8080:{ip}"}]]}
    send(chat, txt, markup=kb)


def cmd_info(chat, arg="", reply_to=None, port=80):
    """Живой ONVIF-опрос (кнопки ℹ️/«Обновить»; /info идёт в кэш-версию, I47)."""
    ip = _resolve_cam(arg)
    if not ip:
        send(chat, "Укажи IP или имя: <code>/info 10.20.51.50</code>", reply_to=reply_to)
        return
    st.remember_ip(ip)
    chat_action(chat)  # R11
    t0 = time.time()
    info = device_info(ip, port=port, user=st.CAM_USER, pwd=st.CAM_PASS)
    dt = time.time() - t0
    if info.get("model"):
        inv.note_onvif(ip, info)  # I48
        lbl = inv.label(ip)
        send(chat, f"✅ <code>{ip}</code> (за {dt:.1f}s)"
                   + (f"\n📒 {esc(lbl)}" if lbl else "") + "\n"
                   f"{esc(info['manufacturer'])} {esc(info['model'])}\n"
                   f"fw {esc(info['firmware'])}\nsn {esc(info['serial'])}",
             reply_to=reply_to)
    else:
        send(chat, human_err(f"<code>{ip}</code>: ONVIF не ответил за {dt:.1f}s",
                             info.get("error")),
             markup=fail_kb("info", ip, try8080=(port != 8080)), reply_to=reply_to)


def cmd_status(chat, arg="", reply_to=None):
    """R39/U47: PID, версия, память, ошибки за час, длительность команд."""
    s = st.stats_snapshot()
    up = int(time.time() - s["started"])
    h, m, sec = up // 3600, (up % 3600) // 60, up % 60
    lat = tg_ping()
    lat_s = f"{lat:.0f} мс" if lat is not None else "таймаут (канал дёргается)"
    mem = mem_mb()
    mem_s = f"{mem:.0f} МБ" if mem is not None else "—"
    dur_s = ", ".join(f"{c} {d:.1f}s" for c, d in s["durations"][-3:]) or "—"
    maint_s = ""
    try:  # Волна D (159): активные окна работ в /status
        import bot_ops
        line = bot_ops.status_line()
        if line:
            maint_s = f"\n{esc(line)}"
    except Exception:
        pass
    try:  # Волна F (299): свежесть фактов
        import bot_reconcile
        fresh = bot_reconcile.freshness_note()
        if fresh:
            maint_s += f"\n{esc(fresh)}"
    except Exception:
        pass
    send(chat,
         f"✅ <b>Бот работает</b> · v{esc(BOT_VERSION)}\n"
         f"🆔 PID {os.getpid()} · 🧠 память {mem_s}\n"
         f"⏱ аптайм: {h}ч {m}м {sec}с\n"
         f"📨 запросов: {s['requests']} · последняя: {esc(s['last_cmd'])}\n"
         f"⚠️ ошибок за час: {s['errors_hour']} · ретраев tg: {s['retries']} · "
         f"429: {s['e429']}\n"
         f"🐢 последние команды: {esc(dur_s)}\n"
         f"📡 задержка Telegram: {lat_s}\n"
         f"🌐 подсеть скана: {esc(st.cget('scan_subnet'))}\n"
         f"🔑 креды камер: {esc(st.CAM_USER)}/**** (пароли не меняются)"
         + maint_s,
         markup=MAIN_KB, reply_to=reply_to)


def cmd_ping(chat, arg="", reply_to=None):
    lat = tg_ping()
    if lat is not None:
        send(chat, f"🏓 pong · Telegram {lat:.0f} мс · бот жив ✅",
             markup=MAIN_KB, reply_to=reply_to)
    else:
        send(chat, "🏓 pong · бот жив ✅, но Telegram отвечает с таймаутом "
                   "(DPI-троттлинг)", markup=MAIN_KB, reply_to=reply_to)


def cmd_log(chat, arg="", reply_to=None):
    """R40/U35: хвост camera_bot.log в чат (по умолчанию 30 строк, максимум 200)."""
    try:
        n = max(1, min(int(arg), 200))
    except (TypeError, ValueError):
        n = st.cget("log_tail_lines")
    try:
        with open(LOG_PATH, encoding="utf-8-sig", errors="replace") as f:
            tail = f.readlines()[-n:]
    except Exception as e:
        send(chat, human_err("Не смог прочитать лог", e), reply_to=reply_to)
        return
    if not tail:
        send(chat, "Лог пуст.", reply_to=reply_to)
        return
    buf = ""
    for ln in tail:
        ln = ln.rstrip()
        if buf and len(buf) + len(ln) > 3000:
            send(chat, f"<pre>{esc(buf)}</pre>")
            buf = ""
        buf += ln + "\n"
    if buf:
        send(chat, f"<pre>{esc(buf)}</pre>", reply_to=reply_to)


def cmd_restart(chat, arg="", reply_to=None):
    """R41: удалённый перезапуск — выходим кодом 0, run_bot.cmd поднимет заново."""
    send(chat, "♻️ Перезапускаюсь… (run_bot.cmd поднимет через ~3с)", reply_to=reply_to)
    log("RESTART по команде владельца", logging.WARNING)
    try:  # Волна E (230): маркер причины выхода (os._exit минует atexit)
        import bot_release
        bot_release.mark_exit("restart_cmd", 0)
    except Exception:
        pass
    threading.Timer(1.5, lambda: os._exit(0)).start()


# ---------- Диспетчеризация команд ----------
HANDLERS = {
    "/shot": cmd_shot, "/diag": cmd_diag, "/info": cmd_info,
    "/status": cmd_status, "/ping": cmd_ping,
    "/start": cmd_start, "/log": cmd_log, "/restart": cmd_restart,
}
ALIASES = {  # U37: русские алиасы
    "/снимок": "/shot", "/диаг": "/diag", "/инфо": "/info",
    "/статус": "/status", "/пинг": "/ping",
    "/лог": "/log", "/рестарт": "/restart",
}
ACTION_FN = {"shot": cmd_shot, "diag": cmd_diag, "info": cmd_info, "orig": cmd_orig}
CB_EXT = {}  # Волна C: внешние колбэки (prefix -> fn(chat, cq, payload))

# Волна B: команды инвентаря/Google; Волна C: UX и здоровье парка;
# Волна D: зоны/операционка/документы/полевой режим
# (импорт в конце — избегаем циклического импорта)
import bot_handlers_inv as _hi  # noqa: E402
import bot_handlers_media as _hm  # noqa: E402
import bot_handlers_ux as _ux  # noqa: E402
import bot_handlers_health as _hh  # noqa: E402
import bot_zones as _bz  # noqa: E402
import bot_issues as _bi  # noqa: E402
import bot_ops as _bo  # noqa: E402
import bot_docs as _bd  # noqa: E402
import bot_qr as _bq  # noqa: E402
import bot_field as _bf  # noqa: E402
import bot_check as _bc  # noqa: E402
import bot_shift as _bs  # noqa: E402
import bot_dossier as _bds  # noqa: E402
import bot_lifecycle as _bl  # noqa: E402
# Волна E: диагностика процесса и релизы
import bot_debug as _dbg  # noqa: E402
import bot_release as _rel  # noqa: E402
# Волна F: качество данных инвентаря и отчётность
import bot_dq_cmds as _dqc  # noqa: E402
import bot_reports as _rp  # noqa: E402
import bot_reconcile as _rc  # noqa: E402
import bot_reconcile_net as _rcn  # noqa: E402
import bot_backups as _bk  # noqa: E402
import bot_enrich as _en  # noqa: E402
# Волна G: глубокий мониторинг и аналитика парка (301-350; 322-329 -> волна H)
import bot_metrics as _mx  # noqa: E402  (SQLite 347 + тик ретенции)
import bot_imgqa as _iq  # noqa: E402
import bot_camtime as _ct  # noqa: E402
import bot_rtsp as _rts  # noqa: E402
import bot_predict as _pd  # noqa: E402
import bot_secaudit as _sa  # noqa: E402
# Волна H: сеть, коммутаторы, топология (351-400 + 322-329)
import bot_topo as _tp  # noqa: E402
import bot_sw_mon as _swm  # noqa: E402
import bot_sw_cmds as _swk  # noqa: E402
import bot_sw_audit as _swa  # noqa: E402
import bot_sw_cfg as _swc  # noqa: E402
import bot_netcheck as _nc  # noqa: E402
# Волна I: Google-экосистема и автоматизация (401-450)
import bot_gsheets2 as _gs2  # noqa: E402  (дифф-синк 429, журнал событий 402)
import bot_gfmt as _gf  # noqa: E402       (оформление таблицы 401/403-412)
import bot_gdrive2 as _gd2  # noqa: E402   (Drive: снимки/снапшоты/бэкапы)
import bot_gcal as _gc  # noqa: E402       (календарь ППР 417-418)
import bot_nightly as _ni  # noqa: E402    (ночные задачи 415/446-450)
# Волна J: отложенные операции DEFER (I11/I12, I27, I32, I36, I37/38/41,
# I44, U16, U44) — все записи только через двухшаговые подтверждения
import bot_poe as _poe  # noqa: E402       (/reboot — PoE-цикл порта)
import bot_provision as _prov  # noqa: E402  (/provision — ПНР заводской)
import bot_macfill as _mf  # noqa: E402    (/macfill — MAC в инвентарь)
import bot_unknownq as _uq  # noqa: E402   (очередь «Неизвестных»)
import bot_autosync as _asn  # noqa: E402  (/autosync xlsx->Sheets)
import bot_media_ops as _mo  # noqa: E402  (/snapall -> Drive, /clip ffmpeg)
import bot_inline as _il  # noqa: E402     (inline-режим @бот)
for _mod in (_hi, _hm, _ux, _hh,
             _bz, _bi, _bo, _bd, _bq, _bf, _bc, _bs, _bds, _bl,
             _dbg, _rel,
             _dqc, _rp, _rc, _rcn, _bk, _en,
             _mx, _iq, _ct, _rts, _pd, _sa,
             _tp, _swm, _swk, _swa, _swc, _nc,
             _gs2, _gf, _gd2, _gc, _ni,
             _poe, _prov, _mf, _uq, _asn, _mo, _il):
    HANDLERS.update(_mod.HANDLERS)
    ALIASES.update(_mod.ALIASES)
    CB_EXT.update(getattr(_mod, "CALLBACKS", {}))
free_text = _bf.try_text        # Волна D: свободный текст (кнопки поля, номера)
on_media = _bds.on_media        # Волна D: фото/голос в досье (193/194)
on_inline = _il.on_inline       # Волна J (U44): inline_query из main-цикла
multi_ip_offer = _hm.multi_ip_offer  # U24: используется в camera_bot

_LAST_CMDS = {"/shot": "shot", "/diag": "diag", "/info": "info", "/find": "find"}


def _obs_cmd(cmd, arg, dt, ok, err, retries0):
    """Волна E: 203 канонический event-лог + 212 аудит (best-effort)."""
    try:
        import bot_obs
        bot_obs.note_cmd(cmd, arg, dt, ok, err=err,
                         tg_retries=st.STATS["retries"] - retries0)
        bot_obs.audit(cmd, arg, "OK" if ok else f"ERR {err}")
    except Exception:
        pass


def _crash(ctx, exc):
    """Волна E (245): крэш-репорт с контекстом и хвостом ring-buffer."""
    try:
        import bot_debug
        bot_debug.crash_report(ctx, exc)
    except Exception:
        pass


def dispatch(chat, cmd, arg, reply_to=None):
    cmd = ALIASES.get(cmd, cmd)
    fn = HANDLERS.get(cmd)
    if not fn:
        sugg = difflib.get_close_matches(cmd, list(HANDLERS) + list(ALIASES),
                                         n=1, cutoff=0.6)  # U36
        hint = f" Может, ты имел в виду <b>{esc(sugg[0])}</b>?" if sugg else ""
        log(f"UNK команда {cmd!r} chat={chat}")
        send(chat, f"Не понял команду.{hint} /help", reply_to=reply_to)
        return
    if cmd in _LAST_CMDS and arg:  # U8: история для /last
        st.note_last(_LAST_CMDS[cmd], arg)
    st.note_cmd_start(cmd, arg)
    t0 = time.time()
    r0 = st.STATS["retries"]
    log(f"CMD {cmd} arg={arg!r} chat={chat}")
    try:
        fn(chat, arg, reply_to=reply_to)
        dt = time.time() - t0
        st.note_cmd_done(cmd, dt)
        log(f"OK  {cmd} arg={arg!r} ({dt:.1f}s)")
        _obs_cmd(cmd, arg, dt, True, None, r0)
    except Exception as e:
        st.note_error()
        log_exc(f"ERR {cmd} arg={arg!r}: {type(e).__name__}: {e}")  # R2
        _obs_cmd(cmd, arg, time.time() - t0, False,
                 f"{type(e).__name__}: {e}", r0)
        _crash({"cmd": cmd, "arg": str(arg)[:200], "chat": chat}, e)
        send(chat, human_err(f"Команда {esc(cmd)} упала", e), reply_to=reply_to)


def run_action(chat, action, ip, port=80):
    """Действие из колбэка/PENDING — с теми же статистикой и обработкой ошибок."""
    fn = ACTION_FN.get(action)
    if not fn:
        return
    if action in ("shot", "diag", "info"):  # U8
        st.note_last(action, ip)
    st.note_cmd_start(action, ip)
    t0 = time.time()
    r0 = st.STATS["retries"]
    log(f"ACT {action} ip={ip} port={port} chat={chat}")
    try:
        fn(chat, ip, port=port)
        dt = time.time() - t0
        st.note_cmd_done(action, dt)
        _obs_cmd(f"act:{action}", ip, dt, True, None, r0)
    except Exception as e:
        st.note_error()
        log_exc(f"ERR {action} ip={ip}: {type(e).__name__}: {e}")
        _obs_cmd(f"act:{action}", ip, time.time() - t0, False,
                 f"{type(e).__name__}: {e}", r0)
        _crash({"cmd": f"act:{action}", "arg": ip, "chat": chat}, e)
        send(chat, human_err(f"Действие {esc(action)} упало", e))


# ---------- Колбэки ----------
CB_TOAST = {"shot": "📸 Снимаю…", "diag": "🩺 Диагностирую…",
            "info": "ℹ️ Опрашиваю…", "orig": "📎 Отправляю оригинал…"}


def on_callback(cq):
    data = cq.get("data", "")
    chat = (cq.get("message") or {}).get("chat", {}).get("id")
    uid = (cq.get("from") or {}).get("id")
    owner = st.cget("owner_chat_id")
    owner_uid = st.cget("owner_user_id") or owner
    if chat != owner or (uid is not None and uid != owner_uid):  # R23 + R26
        log(f"DENY callback chat={chat} from={uid} data={data!r}", logging.WARNING)
        answer_cq(cq.get("id"), "⛔ Доступ только у владельца")
        return
    if data == "cancel":  # U25
        st.clear_pending(chat)
        answer_cq(cq.get("id"), "✖️ Отменено")
        return
    # Волна C: внешние колбэки (find-пагинация, help-табы, ребут, unknown …)
    head, _, payload = data.partition(":")
    if head in CB_EXT:
        log(f"CBQ chat={chat} from={uid} data={data!r}")
        try:
            CB_EXT[head](chat, cq, payload)
        except Exception as e:
            st.note_error()
            log_exc(f"ERR callback {data!r}: {type(e).__name__}: {e}")
            send(chat, human_err(f"Кнопка {esc(head)} упала", e))
        return
    if ":" not in data:
        answer_cq(cq.get("id"))
        return
    left, ip = data.split(":", 1)
    port = 80
    if "@" in left:  # вариант «действие@8080» (U22)
        left, p = left.split("@", 1)
        port = int(p) if p.isdigit() else 80
    if left in ACTION_FN and net.valid_ip(ip):
        answer_cq(cq.get("id"), CB_TOAST.get(left))  # U26
        st.clear_pending(chat)
        log(f"CBQ chat={chat} from={uid} data={data!r}")
        run_action(chat, left, ip, port=port)
    else:
        answer_cq(cq.get("id"))
