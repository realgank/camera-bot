# -*- coding: utf-8 -*-
"""Волна J — I11+I12: /reboot <ip|имя> — жёсткий перезапуск камеры циклом
PoE порта Cross-24. Порядок: порт по MAC (живой mac_dynamic свитча + факты +
инвентарь) → показать ВСЁ, что сейчас на порту (MAC + вендоры) → двухшаговое
подтверждение с TTL (Confirm) → PoE off → пауза → PoE on (portEnable —
RadioGroup 1/0, ВСЕГДА верифицируем!) → ждать возвращения камеры TCP-пробой
до poe_reboot_wait_s → отчёт + аудит. Если на порту >1 MAC — ОТКАЗ (можно
погасить чужое устройство / аплинк). save на свитче НЕ вызывается нигде.
Huawei фоновым PoE-ребутом не поддержан — честный отказ."""
import time
import logging

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_sw_api as sw
from bot_tg import send, edit_message, answer_cq, chat_action
from bot_util import log, log_exc, esc, human_err

_confirm = sw.Confirm()


def _audit(step, detail):
    log(f"poe_reboot: {step}: {detail}")
    try:
        import bot_obs
        bot_obs.audit(f"poe_reboot:{step}", detail, "")
    except Exception:
        pass


def _live_port_macs(sw_ip):
    """Живая MAC-таблица свитча: {порт: [mac, …]} (или None при ошибке)."""
    try:
        entries = sw.cross24_get(sw_ip, "mac_dynamic").get("entries") or []
    except Exception:
        log_exc(f"poe_reboot: mac_dynamic {sw_ip} не прочитался")
        return None
    by_port = {}
    for e in entries:
        p, m = e.get("port"), (e.get("macAddr") or "").lower()
        if p and m:
            by_port.setdefault(str(p), []).append(m)
    return by_port


def find_port(ip, mac):
    """(info, None) либо (None, причина отказа). info = {sw_ip, host, port,
    macs, live, src}. Порт ищем живьём на свитчах-кандидатах (факты +
    инвентарь); если камера сейчас молчит — берём порт из фактов/инвентаря
    и живьём проверяем, что на нём никого лишнего."""
    rec = inv.get(ip) or {}
    nmac = inv.norm_mac(mac)
    # кандидаты-свитчи: факты (access первыми) + колонка инвентаря
    cand = []
    hits = inv.switch_ports(mac) if mac else []
    for h in hits:
        if h.get("sw_ip") and h["sw_ip"] not in cand:
            cand.append(h["sw_ip"])
    inv_sw = str(rec.get("sw_ip") or "").strip()
    if inv_sw and net.valid_ip(inv_sw) and inv_sw not in cand:
        cand.append(inv_sw)
    if not cand:
        return None, ("не знаю свитч камеры: MAC не виден в фактах, "
                      "в инвентаре нет «IP коммутатора». Обнови /facts_refresh "
                      "или заполни инвентарь.")
    live_tables = {}
    for sip in cand[:3]:
        t = _live_port_macs(sip)
        if t is not None:
            live_tables[sip] = t
    # 1) живой поиск: MAC сейчас виден на порту
    best = None
    for sip, table in live_tables.items():
        for port, macs in table.items():
            if nmac and any(inv.norm_mac(m) == nmac for m in macs):
                if best is None or len(macs) < len(best[2]):
                    best = (sip, port, macs)
    if best:
        sip, port, macs = best
        host = next((h["host"] for h in hits if h["sw_ip"] == sip), None) or sip
        return {"sw_ip": sip, "host": host, "port": str(port),
                "macs": macs, "live": True, "src": "live"}, None
    # 2) камера молчит: порт из фактов (min density), фолбэк — инвентарь
    if hits:
        h = hits[0]
        sip, port, host, src = h["sw_ip"], str(h["port"]), h["host"], "факты"
    elif inv_sw and rec.get("port"):
        sip, port, host, src = inv_sw, str(rec["port"]).strip(), \
            str(rec.get("switch") or inv_sw), "инвентарь"
    else:
        return None, ("камера не отвечает, MAC живьём не виден, а порт "
                      "в фактах/инвентаре не записан — PoE дёргать не по чему.")
    table = live_tables.get(sip)
    if table is None:
        t = _live_port_macs(sip)
        if t is None:
            return None, f"свитч <code>{sip}</code> не отвечает web-API — не могу проверить порт."
        table = t
    macs = table.get(port, [])
    return {"sw_ip": sip, "host": host, "port": port,
            "macs": macs, "live": False, "src": src}, None


def port_guard(info, nmac):
    """Причина отказа или None. >1 MAC либо чужой MAC на порту = отказ."""
    idx = sw.port_index(info["port"])
    if idx is None or idx >= 24:
        return (f"«{info['port']}» — не медный PoE-порт (GE1-GE24), похоже "
                f"на аплинк. PoE не трогаю.")
    others = [m for m in info["macs"] if inv.norm_mac(m) != nmac]
    if others or len(info["macs"]) > 1:
        return (f"на порту {info['port']} сейчас {len(info['macs'])} MAC — "
                f"это аплинк или несколько устройств, PoE-off погасил бы "
                f"всех. Отказ (I12). MAC на порту: "
                + ", ".join(f"<code>{esc(m)}</code>" for m in info["macs"][:6]))
    return None


def _macs_text(macs):
    if not macs:
        return "порт пуст (камера обесточена/молчит — MAC не виден)"
    return "\n".join(f"• <code>{esc(m)}</code> — {esc(net.vendor(m))}"
                     for m in macs)


def cmd_reboot(chat, arg="", reply_to=None):
    """I11+I12: PoE-ребут камеры через порт Cross-24."""
    a = (arg or "").strip()
    if not a:
        send(chat, "PoE-ребут камеры: <code>/reboot 10.20.50.51</code> или по "
                   "имени.\n⚠️ Выключает-включает PoE её порта на свитче "
                   "(камера обесточивается, ~40-60с простоя). Мягкий ONVIF-ребут"
                   " — /reboot_soft.", reply_to=reply_to)
        return
    ip = a if net.valid_ip(a) else inv.resolve_ip(a)
    if not ip:
        send(chat, f"«{esc(a)}» не найдено в инвентаре (или неоднозначно) — "
                   f"уточни через /cam.", reply_to=reply_to)
        return
    chat_action(chat)
    rec = inv.get(ip) or {}
    # MAC: живой ARP (после TCP-пробы) приоритетнее инвентарного
    alive = net.tcp_alive(ip)
    live_mac = net.arp_table().get(ip)
    mac = live_mac or rec.get("mac")
    if not mac:
        send(chat, f"У <code>{ip}</code> не известен MAC (ни в ARP, ни в "
                   f"инвентаре) — порт не найти. Попробуй /macfill.",
             reply_to=reply_to)
        return
    info, err = find_port(ip, mac)
    if err:
        send(chat, f"❌ PoE-ребут <code>{ip}</code>: {err}", reply_to=reply_to)
        return
    guard = port_guard(info, inv.norm_mac(mac))
    if guard:
        send(chat, f"⛔ PoE-ребут <code>{ip}</code>: {guard}", reply_to=reply_to)
        _audit("refuse", f"{ip} {info['sw_ip']}:{info['port']} {guard[:120]}")
        return
    key = f"{ip}|{info['sw_ip']}|{info['port']}"
    _confirm.put(key, {"ip": ip, "sw_ip": info["sw_ip"], "port": info["port"],
                       "host": info["host"]})
    lbl = inv.label(ip)
    send(chat,
         f"⚡ <b>PoE-ребут</b> <code>{ip}</code>"
         + (f" — {esc(lbl)}" if lbl else "") + "\n"
         f"🔌 свитч {esc(info['host'])} <code>{info['sw_ip']}</code> · порт "
         f"<b>{esc(info['port'])}</b> (источник: {esc(info['src'])})\n"
         f"Сейчас на порту:\n{_macs_text(info['macs'])}\n\n"
         f"⚠️ Порт будет обесточен на {st.cget('poe_reboot_off_s')}с, камера "
         f"перезагрузится (~40-60с). Конфиг свитча НЕ сохраняется.\n"
         f"Подтверди в течение {st.cget('sw_confirm_ttl_s')}с:",
         markup={"inline_keyboard": [[
             {"text": "⚡ Выключить-включить PoE…", "callback_data": f"poeR:{key}"},
             {"text": "✖️ Отмена", "callback_data": "cancel"}]]},
         reply_to=reply_to)


def cb_poer(chat, cq, key):
    """Шаг 2: финальное подтверждение (двухшаговость I12)."""
    p = _confirm.take(key)
    if not p:
        answer_cq(cq.get("id"), "⌛ Подтверждение устарело — повтори /reboot")
        return
    _confirm.put(key, p)  # вернуть под тот же TTL для финального шага
    answer_cq(cq.get("id"), "Нужно финальное подтверждение")
    mid = (cq.get("message") or {}).get("message_id")
    txt = (f"⚠️ <b>Финальное подтверждение</b>\nPoE off/on порта "
           f"{esc(p['port'])} @ {esc(p['host'])} <code>{p['sw_ip']}</code> "
           f"для камеры <code>{p['ip']}</code> — жми ✅ в течение "
           f"{st.cget('sw_confirm_ttl_s')}с.")
    kb = {"inline_keyboard": [[
        {"text": "✅ Да, дёрнуть PoE", "callback_data": f"poeRy:{key}"},
        {"text": "✖️ Отмена", "callback_data": "cancel"}]]}
    if mid:
        edit_message(chat, mid, txt, markup=kb)
    else:
        send(chat, txt, markup=kb)


def poe_cycle(sip, port, ip, off_s=None, wait_s=None):
    """Ядро PoE-ребута: off → пауза → on (verify) → ждать возврата камеры
    TCP-пробой. Возвращает (poe_ok: bool, back_s: float|None). Конфиг свитча
    НЕ сохраняется. Переиспользуется /reboot (cb_poery) и watchdog bot_newcam.
    Вызывающий сам отвечает за гарды (find_port + port_guard) и подтверждение."""
    off_s = float(st.cget("poe_reboot_off_s")) if off_s is None else off_s
    wait_s = float(st.cget("poe_reboot_wait_s")) if wait_s is None else wait_s
    idx = sw.port_index(port)
    poe_ok = False
    try:
        sw.cross24_set(sip, "poe_poeEdit",
                       {"portList": port, "portEnable": 0, "portWatchDog": 0})
        time.sleep(off_s)
    finally:
        try:  # portEnable — RadioGroup 1/0; всегда верифицируем состояние
            sw.cross24_set(sip, "poe_poeEdit",
                           {"portList": port, "portEnable": 1, "portWatchDog": 0})
            state = sw.cross24_get(sip, "poe_poe")["ports"][idx]
            poe_ok = bool(state.get("portEnable"))
        except Exception:
            log_exc(f"poe_reboot: не смог вернуть PoE {sip} {port}")
    if not poe_ok:
        return False, None
    t0 = time.time()
    back = None
    while time.time() - t0 < wait_s:
        if net.tcp_alive(ip, t=1.0):
            back = time.time() - t0
            break
        time.sleep(3)
    return True, back


def cb_poery(chat, cq, key):
    """Шаг 3: исполнение. PoE возвращается в poe_cycle, save НЕ вызывается."""
    p = _confirm.take(key)
    if not p:
        answer_cq(cq.get("id"), "⌛ Подтверждение устарело — повтори /reboot")
        return
    answer_cq(cq.get("id"), "⚡ Дёргаю PoE…")
    chat_action(chat)
    ip, sip, port = p["ip"], p["sw_ip"], p["port"]
    _audit("go", f"{ip} via {sip}:{port} подтверждён владельцем")
    wait_s = float(st.cget("poe_reboot_wait_s"))
    poe_ok, back = poe_cycle(sip, port, ip)
    if not poe_ok:
        send(chat, f"❌ <b>PoE порта {esc(port)} @ <code>{sip}</code> НЕ "
                   f"вернулся</b> — проверь вручную: /poe {sip} !", )
        _audit("poe_fail", f"{ip} {sip}:{port} PoE не подтвердился")
        return
    try:
        import bot_metrics as mx
        mx.event_add(ip, "poe_reboot", f"{sip}:{port}"
                     + (f" up {back:.0f}s" if back else " no return"),
                     cooldown_h=0)
    except Exception:
        pass
    if back is not None:
        _audit("done", f"{ip} вернулась за {back:.0f}s")
        send(chat, f"✅ <b>PoE-ребут выполнен</b>: <code>{ip}</code> вернулась "
                   f"в сеть за <b>{back:.0f}с</b> (порт {esc(port)} @ "
                   f"<code>{sip}</code>, PoE включён ✅, save не вызывался).",
             markup={"inline_keyboard": [[
                 {"text": "📸 Снимок", "callback_data": f"shot:{ip}"},
                 {"text": "🩺 Диаг", "callback_data": f"diag:{ip}"}]]})
    else:
        _audit("no_return", f"{ip} не вернулась за {wait_s:.0f}s")
        send(chat, f"⚠️ PoE порта {esc(port)} включён ✅, но <code>{ip}</code> "
                   f"не ответила за {wait_s:.0f}с — камера может грузиться "
                   f"дольше. Проверь /diag {ip} через минуту.",
             markup={"inline_keyboard": [[
                 {"text": "🩺 Диаг", "callback_data": f"diag:{ip}"}]]})
    log(f"poe_reboot: {ip} {sip}:{port} завершён (back="
        f"{back and round(back)}s)", logging.WARNING)


def cb_poeq(chat, cq, ip):
    """Кнопка «⚡ PoE-ребут» под алертом падения: тот же путь, что /reboot —
    поиск порта, гарды (>1 MAC = отказ) и двухшаговое подтверждение."""
    answer_cq(cq.get("id"), "Готовлю PoE-ребут…")
    cmd_reboot(chat, arg=ip)


HANDLERS = {"/reboot": cmd_reboot}
ALIASES = {"/поребут": "/reboot", "/poe_reboot": "/reboot"}
CALLBACKS = {"poeR": cb_poer, "poeRy": cb_poery, "poeQ": cb_poeq}
