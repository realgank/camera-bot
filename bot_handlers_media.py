# -*- coding: utf-8 -*-
"""Альбомы и мульти-IP (Волна C): U10 (sendMediaGroup для нескольких IP),
U17 /lapse (серия кадров), U18 /compare (два снимка рядом), U24 (мульти-IP
в одном сообщении -> «снимки всех / диаг всех»), общий _send_album (его же
использует U50 «Снимки всех избранных» из bot_handlers_ux)."""
import time
import threading
from typing import List, Tuple
from concurrent.futures import ThreadPoolExecutor

import bot_state as st
import bot_net as net
import bot_inventory as inv
from bot_tg import (send, send_chunks, send_photo, send_media_group,
                    chat_action, answer_cq)
from bot_util import esc, human_err
from onvif_snap import get_snapshot


def _snap_many(ips: List[str]) -> Tuple[List[Tuple[bytes, str]], List[str]]:
    """Параллельные снимки: ([(jpeg, подпись)], [ошибки])."""
    def one(ip):
        data, msg = get_snapshot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
        return ip, data, msg

    got, fails = [], []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for ip, data, msg in ex.map(one, ips):
            if data:
                lbl = inv.label(ip)
                got.append((data, f"{ip}" + (f" · {lbl}" if lbl else "")
                            + f" · {time.strftime('%H:%M:%S')}"))
            else:
                fails.append(f"❌ <code>{ip}</code>: {esc(str(msg)[:80])}")
    return got, fails


def _send_album(chat, ips: List[str], reply_to=None):
    chat_action(chat, "upload_photo")
    got, fails = _snap_many(ips[:10])
    if got:
        if len(got) == 1:
            send_photo(chat, got[0][0], caption=got[0][1], reply_to=reply_to)
        else:
            send_media_group(chat, got, reply_to=reply_to)  # U10
    if fails:
        send_chunks(chat, [f"Снимков нет с {len(fails)} камер:"] + fails)
    if not got and not fails:
        send(chat, "Пустой список IP.", reply_to=reply_to)


def cmd_lapse(chat, arg="", reply_to=None):
    """U17: /lapse <ip> [n] — серия из n кадров альбомом."""
    parts = (arg or "").split()
    if not parts:
        send(chat, "Серия снимков: <code>/lapse 10.20.50.51 4</code>",
             reply_to=reply_to)
        return
    ip = parts[0] if net.valid_ip(parts[0]) else inv.resolve_ip(parts[0])
    if not ip:
        send(chat, f"«{esc(parts[0])}» — не IP и не имя из инвентаря.",
             reply_to=reply_to)
        return
    try:
        n = max(2, min(int(parts[1]), int(st.cget("lapse_max"))))
    except (IndexError, ValueError):
        n = 3
    delay = float(st.cget("lapse_delay_s"))
    send(chat, f"🎞 Серия {n} кадров с <code>{ip}</code> "
               f"(пауза {delay:.0f}s)…", silent=True, reply_to=reply_to)
    chat_action(chat, "upload_photo")
    shots, msg, t_start = [], "?", time.time()
    for i in range(n):
        data, msg = get_snapshot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
        if data:
            shots.append((data, f"{ip} · кадр {i + 1}/{n} · "
                                f"+{time.time() - t_start:.0f}s"))
        if i < n - 1:
            time.sleep(delay)
    if shots:
        send_media_group(chat, shots)
    else:
        send(chat, human_err(f"Серия с <code>{ip}</code> не получилась", msg))


def cmd_compare(chat, arg="", reply_to=None):
    """U18: /compare <ip1> <ip2> — два снимка рядом (альбом)."""
    ips = []
    for tok in (arg or "").split()[:2]:
        ip = tok if net.valid_ip(tok) else inv.resolve_ip(tok)
        if ip:
            ips.append(ip)
    if len(ips) != 2:
        send(chat, "Сравнение: <code>/compare 10.20.50.51 10.20.50.52</code> "
                   "(IP или имена)", reply_to=reply_to)
        return
    send(chat, f"🆚 Снимаю <code>{ips[0]}</code> и <code>{ips[1]}</code>…",
         silent=True, reply_to=reply_to)
    _send_album(chat, ips, reply_to=reply_to)


# ---------- U24: мульти-IP ----------
_multi = {}
_multi_n = [0]
_multi_lock = threading.Lock()


def multi_ip_offer(chat, ips: List[str], reply_to=None):
    """Сообщение с несколькими IP -> кнопки «снимки всех / диаг всех»."""
    ips = ips[:int(st.cget("multi_ip_max"))]
    with _multi_lock:
        _multi_n[0] += 1
        tok = str(_multi_n[0] % 1000)
        _multi[tok] = ips
        while len(_multi) > 8:
            _multi.pop(next(iter(_multi)))
    send(chat, f"Вижу {len(ips)} IP: "
               + ", ".join(f"<code>{i}</code>" for i in ips),
         markup={"inline_keyboard": [[
             {"text": f"📸 Снимки всех ({len(ips)})", "callback_data": f"mshot:{tok}"},
             {"text": "🩺 Диаг всех", "callback_data": f"mdiag:{tok}"}]]},
         reply_to=reply_to)


def cb_mshot(chat, cq, tok):
    with _multi_lock:
        ips = _multi.get(tok)
    if not ips:
        answer_cq(cq.get("id"), "⌛ Список устарел — пришли IP заново")
        return
    answer_cq(cq.get("id"), "📸 Снимаю всех…")
    _send_album(chat, ips)


def cb_mdiag(chat, cq, tok):
    with _multi_lock:
        ips = _multi.get(tok)
    if not ips:
        answer_cq(cq.get("id"), "⌛ Список устарел — пришли IP заново")
        return
    answer_cq(cq.get("id"), "🩺 Проверяю всех…")
    chat_action(chat)
    rows = []
    for ip in ips:
        ports = net.open_ports(ip, ports=(80, 554))
        ok = "🟢" if ports else "🔴"
        lbl = inv.label(ip) or ""
        rows.append(f"{ok} {ip:<15} п:{','.join(map(str, ports)) or '—':<7} {lbl[:24]}")
    send(chat, "<pre>" + esc("\n".join(rows)) + "</pre>")


HANDLERS = {"/lapse": cmd_lapse, "/compare": cmd_compare}
ALIASES = {"/серия": "/lapse", "/сравни": "/compare"}
CALLBACKS = {"mshot": cb_mshot, "mdiag": cb_mdiag}
