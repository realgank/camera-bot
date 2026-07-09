# -*- coding: utf-8 -*-
"""Волна H — мониторинг свитчей и портов (322-329, 355-359, 376, 380,
386-389, 400). Фон: ротация sw_mon_batch Cross-24 за тик (web-API, 3-4 GET
на свитч) — малые порции, парк за ~4 часа. Считаем:
376 сброс аптайма (перезагрузка) · 322/357/358 PoE-бюджет и алерт >85% ·
323 тренд PoE per порт (метрики SQLite) · 359 PoE-аномалии (линк есть,
0 Вт; скачок Вт) · 324 порты на грани · 327 деградация скорости 100→10 ·
400 история up/down портов (events) · 356 флаппинг портов · 328 переезд
камеры на другой порт · 329/365 >1 MAC на камерном порту · 362 новый MAC ·
380 CPU/память/температура · 386 /swtraffic · 387 аплинки · 389 /gw шлюзы ·
388 root-cause при массовом падении (вызывается из bot_health).
Интерактивные команды портов (/poe /flap /swtraffic /sw_env /port_history
/cable) — в bot_sw_cmds (лимит 500 строк). Huawei фоново не опрашивается. Запись на свитчи из этого модуля не выполняется, кроме 385 /cable
(TDR с PoE-off) — строго через двухшаговое подтверждение."""
import re
import time
import threading

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_store as store
import bot_metrics as mx
import bot_sw_api as sw
from bot_tg import send, send_chunks, chat_action, answer_cq
from bot_util import log, log_exc, esc

_confirm = sw.Confirm()


def _spath():
    return st.cget("sw_state_path")


def _state() -> dict:
    return store.jload(_spath(), {})


def _cam_macs() -> dict:
    return {c["nmac"]: c for c in inv.cams() if c.get("nmac")}


def _fmt_w(mw) -> str:
    return f"{(mw or 0) / 1000:.1f}"


# ---------- фоновый опрос одного Cross-24 ----------
def _poll_cross24(ip: str) -> None:
    sysd = sw.cross24_get(ip, "sys_sysinfo")
    poe = sw.cross24_get(ip, "poe_poe")
    panel = sw.cross24_get(ip, "panel_info")
    cpumem = {}
    try:
        cpumem = sw.cross24_get(ip, "sys_cpumem")
    except Exception:
        pass
    host = sysd.get("hostname") or ip
    now = int(time.time())
    old = _state().get(ip) or {}
    rec = {"host": host, "checked": now}

    # 376: сброс аптайма = перезагрузка без нашего ведома
    up = sw.c24_uptime_s({"sysUpTime": sysd.get("sysUpTime")})
    if up is not None:
        rec["uptime_s"] = up
        prev_up, prev_ts = old.get("uptime_s"), old.get("checked")
        if prev_up is not None and prev_ts and up < prev_up + (now - prev_ts) - 600:
            if mx.event_add(ip, "sw_reboot", f"{prev_up}->{up}", cooldown_h=6):
                mx.owner_alert(f"♻️ <b>Свитч перезагрузился</b>: {esc(host)} "
                               f"<code>{ip}</code> — аптайм сбросился "
                               f"(было {prev_up // 3600}ч, стало {up // 3600}ч). "
                               f"Питание/крэш? (376)")

    # 380: CPU/память/температура PoE-контроллера
    if cpumem:
        mx.metric_add(f"sw:{ip}", "cpu", cpumem.get("cpu") or 0, ts=now)
        mx.metric_add(f"sw:{ip}", "mem", cpumem.get("mem") or 0, ts=now)
        if (cpumem.get("cpu") or 0) >= 90 and mx.event_add(ip, "sw_cpu_high",
                                                           str(cpumem["cpu"])):
            mx.owner_alert(f"🌡 CPU свитча {esc(host)} <code>{ip}</code>: "
                           f"{cpumem['cpu']}% (380)")
    if poe.get("devTemp") is not None:
        mx.metric_add(f"sw:{ip}", "temp", poe["devTemp"], ts=now)
        if poe["devTemp"] >= 60 and mx.event_add(ip, "sw_temp_high",
                                                 str(poe["devTemp"])):
            mx.owner_alert(f"🌡 <b>Свитч греется</b>: {esc(host)} "
                           f"<code>{ip}</code> — {poe['devTemp']}°C (380)")

    # 322/357/358: PoE-бюджет
    total_mw = poe.get("devPower") or 0
    budget_w = float(st.cget("sw_poe_budget_w"))
    pct = 100.0 * total_mw / 1000 / budget_w if budget_w else 0
    rec["poe_mw"] = total_mw
    mx.metric_add(f"sw:{ip}", "poe_w", total_mw / 1000, ts=now)
    if pct >= float(st.cget("poe_budget_warn_pct")):
        if mx.event_add(ip, "poe_budget", f"{pct:.0f}%"):
            mx.owner_alert(f"⚡ <b>PoE-бюджет {esc(host)}</b> <code>{ip}</code>: "
                           f"{_fmt_w(total_mw)} Вт из {budget_w:.0f} "
                           f"({pct:.0f}%) — следующая камера может не "
                           f"подняться (358)")

    # per-port: PoE + линк + скорость
    ports_poe = poe.get("ports") or []
    ports_link = (panel.get("ports") or [])
    old_ports = old.get("ports") or {}
    new_ports, jump_w = {}, float(st.cget("poe_port_jump_w"))
    for i, pp in enumerate(ports_poe):
        pname = f"GE{i + 1}"
        li = ports_link[i] if i < len(ports_link) else {}
        cur_mw = pp.get("portPower") or 0
        p = {"up": bool(li.get("linkup")), "speed": li.get("speed"),
             "full": bool(li.get("dupFull")), "poe_mw": cur_mw,
             "poe_on": bool(pp.get("portStatus"))}
        new_ports[pname] = p
        o = old_ports.get(pname) or {}
        key = f"{ip}:{pname}"
        # 400: история up/down; 356: флаппинг
        if o and o.get("up") is not None and o["up"] != p["up"]:
            mx.event_add(key, "port_up" if p["up"] else "port_down",
                         host, cooldown_h=0)
            downs = len(mx.events(ip=key, kind="port_down", days=1))
            if downs >= 5 and mx.event_add(key, "port_flap", str(downs)):
                mx.owner_alert(f"📉 <b>Порт флапает</b>: {esc(host)} "
                               f"{pname} — {downs} падений за сутки (356). "
                               f"/port_history {ip} {pname}")
        # 323: тренд PoE per порт
        if cur_mw:
            mx.metric_add(f"sw:{key}", "poe_w", cur_mw / 1000, ts=now)
        # 359: линк есть, PoE 0 Вт; скачок потребления
        if p["up"] and o.get("poe_mw") and not cur_mw and not p["poe_on"]:
            if mx.event_add(key, "poe_zero", ""):
                mx.owner_alert(f"⚡ {esc(host)} {pname}: линк up, но PoE 0 Вт — "
                               f"деградация камеры/инжектора? (359)")
        if o.get("poe_mw") and cur_mw and \
                abs(cur_mw - o["poe_mw"]) / 1000 >= jump_w:
            mx.event_add(key, "poe_jump", f"{o['poe_mw']}->{cur_mw}")
        # 327: деградация скорости
        if p["up"] and str(p.get("speed")) == "10":
            if mx.event_add(key, "speed_low", "10M"):
                mx.owner_alert(f"🐌 {esc(host)} {pname}: линк поднялся на "
                               f"10 Мбит/с — кабель умирает? (327)")
    rec["ports"] = new_ports

    # 328/329/362: MAC-таблица (переезды, >1 MAC, новые MAC)
    try:
        _check_macs(ip, host, old)
    except Exception:
        log_exc(f"sw_mon: mac-чек {ip}")

    def _save(d):
        cur = d.get(ip) or {}
        cur.update(rec)
        d[ip] = cur
        return d
    store.jupdate(_spath(), {}, _save)


def _check_macs(ip: str, host: str, old: dict) -> None:
    mt = sw.cross24_get(ip, "mac_dynamic")
    entries = mt.get("entries") or []
    import bot_topo
    ups = set(bot_topo.uplink_ports(ip))
    by_port: dict = {}
    for m in entries:
        p = m.get("port")
        if p and p not in ups:
            by_port.setdefault(p, []).append(m.get("macAddr") or "")
    cam_macs = _cam_macs()
    old_pm = old.get("portmac") or {}
    portmac = {}
    for p, macs in by_port.items():
        nmacs = sorted(inv.norm_mac(m) for m in macs if m)
        portmac[p] = nmacs
        key = f"{ip}:{p}"
        cams_here = [cam_macs[m] for m in nmacs if m in cam_macs]
        # 329/365: >1 MAC на камерном порту (>6 MAC = это явно аплинк
        # к нераспознанному свитчу, а не хаб за камерой — не спамим)
        if cams_here and 1 < len(nmacs) <= 6:
            if mx.event_add(key, "multi_mac", ",".join(nmacs)[:200]):
                mx.owner_alert(
                    f"🔀 <b>&gt;1 MAC на камерном порту</b> {esc(host)} {esc(p)}: "
                    f"{len(nmacs)} MAC — мини-свитч/хаб? (329)\n"
                    + "\n".join(f"• <code>{esc(m)}</code> "
                                f"{esc(net.vendor(':'.join(m[i:i+2] for i in range(0, 12, 2))))}"
                                for m in nmacs[:5]))
        # 362: новый MAC на порту против прошлого снапшота
        prev = set(old_pm.get(p) or [])
        if prev:
            for m in set(nmacs) - prev:
                if m in cam_macs:
                    continue
                if mx.event_add(f"{ip}:{p}", "mac_new", m):
                    mac_h = ":".join(m[i:i + 2] for i in range(0, 12, 2)).upper()
                    mx.owner_alert(f"🛡 <b>Новый MAC на порту</b> {esc(host)} "
                                   f"{esc(p)}: <code>{esc(mac_h)}</code> "
                                   f"{esc(net.vendor(mac_h.lower()))} — "
                                   f"самовольное подключение? (362)")
    # 328: переезд камеры (её MAC против фактов)
    facts_e = sw.by_ip(ip)
    facts_pm = {}
    if facts_e:
        for m in facts_e["mac_table"]:
            facts_pm[inv.norm_mac(m.get("mac"))] = m.get("port")
    for p, nmacs in portmac.items():
        for m in nmacs:
            rec = cam_macs.get(m)
            if not rec:
                continue
            was = facts_pm.get(m)
            if was and was != p and was not in ups:
                if mx.event_add(f"{ip}:{p}", "mac_move", f"{m}: {was}->{p}"):
                    mx.owner_alert(f"🚚 <b>Камера переехала</b>: "
                                   f"{esc(rec.get('name') or rec.get('ip'))} "
                                   f"на {esc(host)} была п.{esc(was)}, теперь "
                                   f"п.{esc(p)} — обнови инвентарь (328)")
    def _save(d):
        cur = d.get(ip) or {}
        cur["portmac"] = portmac
        d[ip] = cur
        return d
    store.jupdate(_spath(), {}, _save)


def _next_switches(n: int) -> list:
    ips = [e["ip"] for e in sw.facts() if e["ok"]]
    if not ips:
        return []
    cur = int(mx.kv_get("cursor:sw_mon") or 0) % len(ips)
    batch = [ips[(cur + i) % len(ips)] for i in range(min(n, len(ips)))]
    mx.kv_set("cursor:sw_mon", str((cur + len(batch)) % len(ips)))
    return batch


def _tick_mon() -> None:
    if not st.cget("sw_mon_enabled"):
        return
    if not mx.due("sw_mon", float(st.cget("sw_mon_period_min")),
                  first_delay_s=420):
        return
    t0 = time.time()
    for ip in _next_switches(int(st.cget("sw_mon_batch"))):
        try:
            _poll_cross24(ip)
        except Exception as e:
            log(f"sw_mon: {ip} не опросился: {e}")
    log(f"sw_mon: тик за {time.time() - t0:.1f}s")


# ---------- 389: шлюзы ----------
def gw_targets() -> list:
    lst = list(st.cget("gw_list") or [])
    if not lst:
        lst = [s + ".254" for s in st.cget("cam_subnets") or []] + ["10.20.5.1"]
    return lst


def _tick_gw() -> None:
    if not mx.due("gw_check", float(st.cget("gw_check_min")), first_delay_s=300):
        return
    arp = net.arp_table()
    old = _state().get("_gw") or {}
    cur = {}
    for gw in gw_targets():
        alive = net.ping(gw) is not None
        mac = arp.get(gw) or net.arp_table().get(gw, "")
        cur[gw] = {"alive": alive, "mac": mac}
        o = old.get(gw) or {}
        if o.get("alive") and not alive:
            if mx.event_add(gw, "gw_down", ""):
                mx.owner_alert(f"🚨 <b>Шлюз недоступен</b>: <code>{gw}</code> (389)")
        if mac and o.get("mac") and mac != o["mac"]:
            if mx.event_add(gw, "gw_mac_change", f"{o['mac']}->{mac}"):
                mx.owner_alert(f"🛡 <b>MAC шлюза сменился</b> <code>{gw}</code>: "
                               f"было <code>{esc(o['mac'])}</code>, стало "
                               f"<code>{esc(mac)}</code> — подмена/failover? (389)")
    def _save(d):
        d["_gw"] = cur
        return d
    store.jupdate(_spath(), {}, _save)


def cmd_gw(chat, arg="", reply_to=None):
    chat_action(chat)
    arp = net.arp_table()
    lines = ["🌐 <b>Шлюзы камерных подсетей</b> (389):"]
    for gw in gw_targets():
        alive = net.ping(gw) is not None
        mac = arp.get(gw, "—")
        lines.append(f"{'🟢' if alive else '🔴'} <code>{gw}</code> · MAC "
                     f"<code>{esc(mac)}</code> {esc(net.vendor(mac))}")
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- 388: root-cause при массовом падении (зовёт bot_health) ----------
def mass_context(ips: list) -> list:
    """Строки-приложение к алерту о массовом падении: жив ли общий свитч,
    его аплинк-сосед, PoE-бюджет (322/325/388)."""
    try:
        sws = {str((inv.get(i) or {}).get("sw_ip") or "").strip()
               for i in ips} - {""}
        if len(sws) != 1:
            return []
        sip = sws.pop()
        host = (sw.by_ip(sip) or {}).get("host") or sip
        lines = []
        alive = net.tcp_alive(sip, ports=(80, 22), t=1.0)
        lines.append(f"🔎 Root-cause: свитч {esc(host)} <code>{sip}</code> "
                     f"{'🟢 отвечает — похоже, камеры/PoE' if alive else '🔴 НЕ отвечает — виноват свитч/аплинк'}")
        if not alive:
            import bot_topo
            for _pa, b, pb in bot_topo.neighbors_map().get(sip, [])[:1]:
                up_ok = net.tcp_alive(b, ports=(80, 22), t=1.0)
                lines.append(f"  ⬆️ аплинк-сосед {esc(bot_topo.host_of(b))} "
                             f"<code>{b}</code> п.{esc(pb)}: "
                             f"{'🟢 жив — лег сам свитч' if up_ok else '🔴 тоже лежит — сегмент/питание'}")
        pw = mx.last_value(f"sw:{sip}", "poe_w")
        if alive and pw is not None:
            budget = float(st.cget("sw_poe_budget_w"))
            lines.append(f"  ⚡ PoE: {pw:.0f} Вт из {budget:.0f} "
                         f"({100 * pw / budget:.0f}%)"
                         + (" — бюджет на пределе, похоже на питание (322)"
                            if 100 * pw / budget >= 90 else ""))
        return lines
    except Exception:
        log_exc("sw_mon: mass_context")
        return []



def _tick():
    _tick_mon()
    _tick_gw()


try:
    import bot_health as _bh
    _bh.MINUTE_TICKS.append(_tick)
except Exception:
    pass

HANDLERS = {"/gw": cmd_gw}
ALIASES = {"/шлюзы": "/gw"}
CALLBACKS: dict = {}
