# -*- coding: utf-8 -*-
"""Волна F — дообогащение инвентаря и снимки:
271 /nosnap — камеры без снимка в Drive-индексе + кнопка «доснять» очередью;
298 /enrich — очередь дообогащения: из пропусков (нет MAC/модели) строится
рабочая очередь, бот по кнопке опрашивает камеры ONVIF/ARP и предлагает
записать значения ПО ОДНОЙ, каждая запись — с подтверждением и автобэкапом
(bot_dq.apply_writes). Пароли камер НЕ меняются."""
import os
import time
import threading

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_dq as dq
import bot_store as store
from bot_tg import send, send_chunks, answer_cq
from bot_util import log_exc, esc, human_err

_ENR = {}   # ip -> {"ts", "writes"} — подтверждения записи
_enr_lock = threading.Lock()


# ---------- 271: /nosnap ----------
def _snap_index() -> dict:
    return store.jload(st.cget("snap_index_path"), {})


def cmd_nosnap(chat, arg="", reply_to=None):
    idx = _snap_index()
    cams = [c for c in inv.cams() if c.get("ip")]
    missing = [c for c in cams if c["ip"] not in idx]
    lines = [f"🖼 <b>Снимки в Drive</b>: есть у {len(cams) - len(missing)} из "
             f"{len(cams)} камер"]
    if not missing:
        lines.append("У всех камер есть хотя бы один снимок ✅")
        send(chat, "\n".join(lines), reply_to=reply_to)
        return
    lines.append(f"Без снимка: <b>{len(missing)}</b>")
    for c in missing[:20]:
        lines.append(f"• <code>{c['ip']}</code> {esc(c.get('name') or '')} "
                     f"{esc(c.get('location') or '')}")
    if len(missing) > 20:
        lines.append(f"… и ещё {len(missing) - 20}")
    n = min(int(st.cget("nosnap_batch")), len(missing))
    send_chunks(chat, lines)
    send(chat, f"Доснять недостающие очередью (по {n} за раз)?", silent=True,
         markup={"inline_keyboard": [[
             {"text": f"📸 Доснять {n}", "callback_data": "nsq:go"},
             {"text": "✖️ Отмена", "callback_data": "cancel"}]]})


def cb_nsq(chat, cq, _payload):
    answer_cq(cq.get("id"), "📸 Снимаю очередь…")
    idx = _snap_index()
    missing = [c["ip"] for c in inv.cams()
               if c.get("ip") and c["ip"] not in idx]
    batch = missing[:int(st.cget("nosnap_batch"))]
    if not batch:
        send(chat, "Очередь пуста ✅")
        return
    from onvif_snap import get_snapshot
    import bot_sheets
    ok = fail = 0
    for ip in batch:
        try:
            data, _m = get_snapshot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
            if data:
                bot_sheets.upload_snapshot(ip, data)
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
    left = len(missing) - len(batch)
    kb = None
    if left:
        kb = {"inline_keyboard": [[
            {"text": f"📸 Ещё {min(int(st.cget('nosnap_batch')), left)}",
             "callback_data": "nsq:go"}]]}
    send(chat, f"📸 Досняток: ✅ {ok} · ❌ {fail} · осталось без снимка ~{left}",
         markup=kb)


# ---------- 298: /enrich ----------
def enrich_queue() -> list:
    """Камеры с пропусками (нет MAC или модели), отсортированы по IP."""
    q = [c for c in inv.cams()
         if c.get("ip") and (not c.get("mac") or not c.get("model"))]
    return sorted(q, key=lambda c: tuple(int(o) for o in c["ip"].split(".")))


def cmd_enrich(chat, arg="", reply_to=None):
    q = enrich_queue()
    if not q:
        send(chat, "🧬 Очередь дообогащения пуста: у всех камер есть MAC и "
                   "модель ✅", reply_to=reply_to)
        return
    no_mac = sum(1 for c in q if not c.get("mac"))
    no_model = sum(1 for c in q if not c.get("model"))
    n = min(int(st.cget("enrich_batch")), len(q))
    send(chat, f"🧬 <b>Очередь дообогащения</b>: {len(q)} камер "
               f"(без MAC: {no_mac}, без модели: {no_model})\n"
               f"Кнопка опросит первые {n} по ONVIF/ARP и предложит записать "
               f"значения ПО ОДНОЙ (каждая запись — с бэкапом).",
         reply_to=reply_to,
         markup={"inline_keyboard": [[
             {"text": f"🧬 Обогатить следующие {n}", "callback_data": "enr:go"},
             {"text": "✖️ Отмена", "callback_data": "cancel"}]]})


def cb_enr(chat, cq, _payload):
    answer_cq(cq.get("id"), "🧬 Опрашиваю…")
    from onvif_snap import device_info
    q = enrich_queue()[:int(st.cget("enrich_batch"))]
    if not q:
        send(chat, "🧬 Очередь пуста ✅")
        return
    arp = net.arp_table()
    got = 0
    for c in q:
        ip = c["ip"]
        info = device_info(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
        writes, parts = [], []
        if not c.get("mac"):
            mac = dq.mac_canon(arp.get(ip))
            if mac:
                writes.append((inv.SHEET_MAIN, c["row"], "MAC-адрес",
                               c.get("mac"), mac))
                parts.append(f"MAC <code>{esc(mac)}</code> (ARP)")
        if not c.get("model") and info.get("model"):
            model = f"{info.get('manufacturer') or ''} {info['model']}".strip()
            writes.append((inv.SHEET_MAIN, c["row"], "Модель камеры",
                           c.get("model"), model))
            parts.append(f"модель {esc(model)}")
        if info.get("model"):
            inv.note_onvif(ip, info)
        if not writes:
            continue
        got += 1
        with _enr_lock:
            _ENR[ip] = {"ts": time.time(), "writes": writes}
        send(chat, f"🧬 <code>{ip}</code> {esc(c.get('name') or '')}: "
                   f"нашёл {', '.join(parts)}. Записать в xlsx?",
             silent=True,
             markup={"inline_keyboard": [[
                 {"text": "💾 Записать", "callback_data": f"ewr:{ip}"},
                 {"text": "✖️ Пропустить", "callback_data": "cancel"}]]})
    left = len(enrich_queue())
    kb = {"inline_keyboard": [[
        {"text": "🧬 Следующая партия", "callback_data": "enr:go"}]]} \
        if left > got else None
    send(chat, f"🧬 Партия опрошена: предложений {got} из {len(q)} · "
               f"в очереди остаётся ~{left}", markup=kb)


def cb_ewr(chat, cq, ip):
    with _enr_lock:
        e = _ENR.pop(ip, None)
    if not e or time.time() - e["ts"] > 600:
        answer_cq(cq.get("id"), "⌛ Предложение устарело — повтори /enrich")
        return
    answer_cq(cq.get("id"), "💾 Пишу с бэкапом…")
    try:
        bak = dq.apply_writes(e["writes"], tag="enrich", who=chat)
    except Exception as ex:
        log_exc("enrich write")
        send(chat, human_err(f"Запись для <code>{ip}</code> не прошла", ex))
        return
    send(chat, f"💾 <code>{ip}</code>: записано {len(e['writes'])} полей ✅ · "
               f"бэкап <code>{esc(os.path.basename(bak))}</code>")


HANDLERS = {"/nosnap": cmd_nosnap, "/enrich": cmd_enrich}
ALIASES = {"/обогащение": "/enrich"}
CALLBACKS = {"nsq": cb_nsq, "enr": cb_enr, "ewr": cb_ewr}
