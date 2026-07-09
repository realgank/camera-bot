# -*- coding: utf-8 -*-
"""Волна J — I44: /macfill — заполнение пустых MAC в инвентаре партиями.
Кандидаты = строки «Все камеры» с IP, но без MAC. По кнопке партия
macfill_batch (деф. 10): TCP-коннект (наполняет ARP) + ONVIF
GetNetworkInterfaces (HwAddress приоритетнее ARP — ловушка IP-конфликтов из
памяти: ARP может залипнуть) → превью заполнений → двухшаговое подтверждение →
запись через bot_dq.apply_writes (автобэкап + журнал 265). Живых записей в
тестах нет — план строится чистой функцией plan_writes()."""
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_sw_api as sw
from bot_tg import send, edit_message, answer_cq, chat_action
from bot_util import log, log_exc, esc, human_err

_confirm = sw.Confirm()
_PLANS = {}          # tok -> {"ts", "writes", "lines"}
_lock = threading.Lock()


def candidates():
    """Записи инвентаря с IP, но без MAC."""
    return [c for c in inv.cams() if c.get("ip") and not c.get("nmac")]


def probe_one(ip):
    """(ip, onvif_mac|None, alive). TCP-коннект наполняет ARP попутно."""
    alive = net.tcp_alive(ip, t=1.0)
    mac = None
    if alive:
        try:
            import bot_provision
            ifc = bot_provision.get_ifaces(ip, timeout=6)
            mac = ifc.get("hwaddr") or None
        except Exception:
            log_exc(f"macfill: ONVIF {ip}")
    return ip, mac, alive


def plan_writes(results, arp, cams_by_ip):
    """Чистая логика превью: results=[(ip, onvif_mac, alive)], arp={ip:mac}.
    -> (writes для apply_writes, строки превью). ONVIF приоритетнее ARP."""
    import bot_dq
    writes, lines = [], []
    for ip, omac, alive in results:
        rec = cams_by_ip.get(ip)
        if not rec:
            continue
        src, mac = None, None
        if omac and bot_dq.mac_canon(omac):
            src, mac = "ONVIF", bot_dq.mac_canon(omac)
        elif arp.get(ip) and bot_dq.mac_canon(arp[ip]):
            src, mac = "ARP", bot_dq.mac_canon(arp[ip])
        if not mac:
            lines.append(f"• <code>{ip}</code> — "
                         + ("MAC не получен" if alive else "молчит"))
            continue
        writes.append((inv.SHEET_MAIN, rec["row"], "MAC-адрес",
                       rec.get("mac"), mac))
        lines.append(f"💾 <code>{ip}</code> → <code>{mac}</code> ({src})")
    return writes, lines


def cmd_macfill(chat, arg="", reply_to=None):
    cands = candidates()
    if not cands:
        send(chat, "✅ У всех камер инвентаря MAC заполнен.", reply_to=reply_to)
        return
    n = min(int(st.cget("macfill_batch")), len(cands))
    lines = [f"🧩 <b>/macfill</b>: без MAC в инвентаре — <b>{len(cands)}</b> "
             f"камер. Первые:"]
    for c in cands[:10]:
        lines.append(f"• <code>{c['ip']}</code> {esc(c.get('name') or '')}")
    if len(cands) > 10:
        lines.append(f"… и ещё {len(cands) - 10}")
    lines.append(f"\nОпросить партию {n} (TCP + ONVIF, ~20с) и показать "
                 f"превью — запись только после подтверждения.")
    send(chat, "\n".join(lines), reply_to=reply_to,
         markup={"inline_keyboard": [[
             {"text": f"🔎 Опросить {n}", "callback_data": "mfq:0"},
             {"text": "✖️ Отмена", "callback_data": "cancel"}]]})


def cb_mfq(chat, cq, payload):
    """Партия: опрос -> превью -> кнопка записи."""
    answer_cq(cq.get("id"), "🔎 Опрашиваю партию…")
    chat_action(chat)
    cands = candidates()
    if not cands:
        send(chat, "✅ Кандидатов не осталось — все MAC заполнены.")
        return
    batch = [c["ip"] for c in cands[:int(st.cget("macfill_batch"))]]
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(probe_one, batch))
    arp = net.arp_table()
    writes, lines = plan_writes(results, arp, {c["ip"]: c for c in cands})
    head = (f"🧩 <b>Превью /macfill</b>: опрошено {len(batch)}, "
            f"заполнится <b>{len(writes)}</b>:")
    kb = None
    if writes:
        tok = str(int(time.time()))
        with _lock:
            _PLANS.clear()
            _PLANS[tok] = {"ts": time.time(), "writes": writes}
        _confirm.put(tok, True)
        kb = {"inline_keyboard": [[
            {"text": f"💾 Записать {len(writes)} (с бэкапом)…",
             "callback_data": f"mfw:{tok}"},
            {"text": "✖️ Отмена", "callback_data": "cancel"}]]}
    else:
        lines.append("Записывать нечего (камеры молчат) — попробуй позже.")
    send(chat, "\n".join([head] + lines), markup=kb)


def cb_mfw(chat, cq, tok):
    """Финальное подтверждение записи (двухшаговость)."""
    if _confirm.take(tok) is None:
        answer_cq(cq.get("id"), "⌛ Превью устарело — повтори /macfill")
        return
    _confirm.put(tok + "!", True)
    answer_cq(cq.get("id"), "Нужно финальное подтверждение")
    with _lock:
        plan = _PLANS.get(tok)
    n = len(plan["writes"]) if plan else 0
    mid = (cq.get("message") or {}).get("message_id")
    txt = (f"⚠️ <b>Финальное подтверждение</b>: записать {n} MAC в "
           f"Все_камеры.xlsx (перед записью — автобэкап)?")
    kb = {"inline_keyboard": [[
        {"text": "✅ Да, записать", "callback_data": f"mfwy:{tok}"},
        {"text": "✖️ Отмена", "callback_data": "cancel"}]]}
    if mid:
        edit_message(chat, mid, txt, markup=kb)
    else:
        send(chat, txt, markup=kb)


def cb_mfwy(chat, cq, tok):
    if _confirm.take(tok + "!") is None:
        answer_cq(cq.get("id"), "⌛ Подтверждение устарело — повтори /macfill")
        return
    with _lock:
        plan = _PLANS.pop(tok, None)
    if not plan:
        answer_cq(cq.get("id"), "⌛ План не найден — повтори /macfill")
        return
    answer_cq(cq.get("id"), "💾 Записываю…")
    import bot_dq
    try:
        bak = bot_dq.apply_writes(plan["writes"], tag="macfill", who="macfill")
    except Exception as e:
        log_exc("macfill: запись")
        send(chat, human_err("Запись MAC не удалась", e))
        return
    try:
        import bot_autosync
        bot_autosync.mark_dirty(f"/macfill {len(plan['writes'])} MAC")
    except Exception:
        pass
    try:
        import bot_obs
        bot_obs.audit("macfill:write", f"{len(plan['writes'])} MAC", "OK")
    except Exception:
        pass
    left = len(candidates())
    send(chat, f"✅ Записано {len(plan['writes'])} MAC (бэкап "
               f"{esc(bak.rsplit(chr(92), 1)[-1])}).\n"
               f"Осталось без MAC: {left}"
               + (" — следующая партия: /macfill" if left else " 🎉"))


HANDLERS = {"/macfill": cmd_macfill}
ALIASES = {"/макфилл": "/macfill"}
CALLBACKS = {"mfq": cb_mfq, "mfw": cb_mfw, "mfwy": cb_mfwy}
