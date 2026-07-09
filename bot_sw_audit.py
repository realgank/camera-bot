# -*- coding: utf-8 -*-
"""Волна H — аудит свитчей (360-370, 377-378, 381-384, 393-394, 379/383
записи с подтверждением):
/sw_audit ports — 360 «MAC есть, камеры в инвентаре нет» + 361 «камера в
инвентаре, порт пуст/погашен»; sec — 362-365 (новые MAC/OUI/мульти-MAC из
событий фоновой ротации); vlan — 368-370 VLAN-сводка и сверка с эталоном;
trunk — 369 транки аплинков; fw — 377 разброс прошивок; time — 378 часы
свитчей; svc — 381-382 сервисы управления и SNMP; contact — 383 пустые
sysContact/Location; /stp <sw> — 367 живой аудит STP + 366 storm-control;
/duplex — 384 скорость/дуплекс камерных портов (393/394 — в bot_netcheck).
Запись: 379 /sw_ntp fix и 383 /sw_contact fix — dry-run → двухшаговое
подтверждение (TTL) → set.cgi БЕЗ save (save в этой волне запрещён)."""
import re
import time
import collections

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_metrics as mx
import bot_sw_api as sw
from bot_tg import send, send_chunks, chat_action, answer_cq
from bot_util import log, log_exc, esc

_confirm = sw.Confirm()
CAM_OUI = ("e0:7f:88",)   # Apix/EVIDENCE


def _uplinks(ip):
    import bot_topo
    return set(bot_topo.uplink_ports(ip))


# ---------- 360/361: порты ↔ инвентарь ----------
def audit_ports():
    """(without_inv, inv_dead): 360 MAC на access-порту без камеры в
    инвентаре; 361 камера в инвентаре, а её MAC в фактах не виден."""
    cam_macs = {c["nmac"]: c for c in inv.cams() if c.get("nmac")}
    seen_macs = set()
    without_inv = []
    for e in sw.facts():
        if not e["ok"]:
            continue
        ups = _uplinks(e["ip"])
        for m in e["mac_table"]:
            nm = inv.norm_mac(m.get("mac"))
            seen_macs.add(nm)
            port = m.get("port")
            if port in ups or nm in cam_macs:
                continue
            mac = (m.get("mac") or "").lower()
            if mac[:8] in CAM_OUI:      # камера-вендор без записи = кандидат
                without_inv.append({"sw": e["host"], "sw_ip": e["ip"],
                                    "port": port, "mac": m.get("mac")})
    inv_dead = [c for c in inv.cams()
                if c.get("nmac") and c.get("sw_ip") and c["nmac"] not in seen_macs]
    return without_inv, inv_dead


# ---------- 368-370: VLAN ----------
def vlan_summary():
    """{vlan: {'switches': n, 'ports': n}} из фактов (vlan в mac_table)."""
    agg: dict = {}
    for e in sw.facts():
        if not e["ok"]:
            continue
        per_sw = set()
        for m in e["mac_table"]:
            v = m.get("vlan")
            if v is None:
                continue
            a = agg.setdefault(v, {"switches": set(), "ports": set()})
            a["switches"].add(e["ip"])
            a["ports"].add((e["ip"], m.get("port")))
            per_sw.add(v)
    return {v: {"switches": len(a["switches"]), "ports": len(a["ports"])}
            for v, a in agg.items()}


def vlan_violations():
    """368: расхождения с эталоном vlan_port_ref (пусто = не задан)."""
    ref = st.cget("vlan_port_ref") or {}
    if not ref:
        return None
    out = []
    for e in sw.facts():
        want = ref.get(e["ip"])
        if not want or not e["ok"]:
            continue
        got: dict = {}
        for m in e["mac_table"]:
            got.setdefault(m.get("port"), set()).add(m.get("vlan"))
        for port, v in want.items():
            g = got.get(port)
            if g and v not in g:
                out.append((e["host"], e["ip"], port, v, sorted(g)))
    return out


def trunk_audit():
    """369: VLAN'ы на обоих концах аплинка не совпадают."""
    import bot_topo
    pv: dict = {}
    for e in sw.facts():
        if not e["ok"]:
            continue
        for m in e["mac_table"]:
            pv.setdefault((e["ip"], m.get("port")), set()).add(m.get("vlan"))
    out = []
    for a, pa, b, pb in bot_topo.links():
        va, vb = pv.get((a, pa)) or set(), pv.get((b, pb)) or set()
        if va and vb and va != vb:
            out.append((a, pa, sorted(va), b, pb, sorted(vb)))
    return out


# ---------- 377/378/381-383 из фактов ----------
def fw_report():
    cnt = collections.Counter((e.get("fw"), e.get("loader"))
                              for e in sw.facts() if e["ok"])
    return cnt.most_common()


def time_report():
    """378: (host, ip, time_str, drift_days) — против времени сборки фактов."""
    try:
        import os
        collected = os.path.getmtime(st.cget("facts_switches"))
    except OSError:
        collected = time.time()
    out = []
    for e in sw.facts():
        if not e["ok"] or not e.get("time_epoch"):
            continue
        drift = (e["time_epoch"] - collected) / 86400
        out.append((e["host"], e["ip"], e.get("time_str"), drift))
    return sorted(out, key=lambda x: x[3])


def svc_report():
    """381/382: telnet вкл; нет ssh; http без https; snmp выкл."""
    rows = []
    for e in sw.facts():
        if not e["ok"]:
            continue
        m = e.get("methods") or {}
        probs = []
        if m.get("telnet"):
            probs.append("telnet ВКЛ")
        if m.get("http") and not m.get("https"):
            probs.append("http без https")
        if not m.get("ssh"):
            probs.append("нет ssh")
        if not m.get("snmp"):
            probs.append("snmp выкл (мониторить нечем)")
        if probs:
            rows.append((e["host"], e["ip"], probs))
    return rows


def contact_report():
    """383: contact/location по умолчанию."""
    return [(e["host"], e["ip"], e.get("contact"), e.get("location"))
            for e in sw.facts()
            if e["ok"] and (e.get("contact") or "").lower() in ("", "default")]


# ---------- /sw_audit ----------
def cmd_sw_audit(chat, arg="", reply_to=None):
    a = (arg or "").strip().lower()
    chat_action(chat)
    note = esc(sw.age_note())
    if a in ("", "sum"):
        wo, dead = audit_ports()
        sv = svc_report()
        ct = contact_report()
        tr = time_report()
        stale = [t for t in tr if abs(t[3]) > 1]
        cnt = mx.event_counts(7)
        send_chunks(chat, [
            f"🛡 <b>Аудит свитчей</b> · {note}",
            f"• 360 камер-вендор БЕЗ инвентаря: <b>{len(wo)}</b> · "
            f"361 в инвентаре, но MAC не виден: <b>{len(dead)}</b> — "
            f"/sw_audit ports",
            f"• 362-365 события за 7 дн.: новые MAC {cnt.get('mac_new', 0)} · "
            f"переезды {cnt.get('mac_move', 0)} · мульти-MAC "
            f"{cnt.get('multi_mac', 0)} — /sw_audit sec",
            f"• 377 прошивки: {len(fw_report())} вариантов — /sw_audit fw",
            f"• 378 часы врут (>1 дня) у <b>{len(stale)}</b> свитчей — "
            f"/sw_audit time · настройка NTP: /sw_ntp",
            f"• 381-382 сервисы не по эталону: <b>{len(sv)}</b> — /sw_audit svc",
            f"• 383 sysContact=default: <b>{len(ct)}</b> — /sw_audit contact",
            "• VLAN: /sw_audit vlan · транки: /sw_audit trunk",
            "• /stp <sw> · /duplex [sw] · /ipplan · /dupip"])
        return
    if a == "ports":
        wo, dead = audit_ports()
        lines = [f"🔎 <b>Порты ↔ инвентарь</b> (360/361) · {note}",
                 f"\nКамеры-вендор на портах БЕЗ записи в инвентаре ({len(wo)}):"]
        lines += [f"• {esc(x['sw'])} п.{esc(x['port'])}: <code>{esc(x['mac'])}</code>"
                  for x in wo[:20]]
        lines.append(f"\nВ инвентаре есть, но MAC в фактах НЕ виден ({len(dead)}) — "
                     f"демонтирована/умерла/перепатчена:")
        lines += [f"• {esc(c.get('name') or '?')} <code>{esc(c.get('ip') or '—')}</code> "
                  f"({esc(c.get('switch') or '?')} п.{esc(c.get('port') or '?')})"
                  for c in dead[:20]]
        if len(dead) > 20:
            lines.append(f"… и ещё {len(dead) - 20}")
        send_chunks(chat, lines)
        return
    if a == "sec":
        lines = [f"🛡 <b>Безопасность доступа</b> (362-365) за 30 дн.:"]
        for kind, ttl in (("mac_new", "Новые MAC на портах (362)"),
                          ("mac_move", "Переезды/MAC-move (328/363)"),
                          ("multi_mac", ">1 MAC на камерном порту (329/365)")):
            ev = mx.events(kind=kind, days=30)
            lines.append(f"\n<b>{ttl}</b>: {len(ev)}")
            lines += [f"• {time.strftime('%d.%m', time.localtime(e['ts']))} "
                      f"{esc(e['ip'])}: {esc((e['info'] or '')[:60])}"
                      for e in ev[-8:]]
        # 364: OUI-фильтр по фактам
        bad = []
        for e in sw.facts():
            if not e["ok"]:
                continue
            ups = _uplinks(e["ip"])
            for m in e["mac_table"]:
                mac = (m.get("mac") or "").lower()
                if m.get("port") not in ups and mac[:8] not in CAM_OUI \
                        and not mac.startswith(("02:", "ae:31")):
                    bad.append((e["host"], m.get("port"), m.get("mac")))
        lines.append(f"\n<b>Не-камерные OUI на access-портах (364)</b>: {len(bad)}")
        lines += [f"• {esc(h)} п.{esc(p)}: <code>{esc(m)}</code> "
                  f"{esc(net.vendor((m or '').lower()))}" for h, p, m in bad[:12]]
        send_chunks(chat, lines)
        return
    if a == "vlan":
        vs = vlan_summary()
        lines = [f"🏷 <b>VLAN по парку</b> (370) · {note}"]
        for v, d in sorted(vs.items()):
            lines.append(f"• VLAN {v}: {d['switches']} свитчей, {d['ports']} портов")
        vio = vlan_violations()
        if vio is None:
            lines.append("368: эталон не задан — заполни vlan_port_ref в конфиге")
        else:
            lines.append(f"368: нарушений против эталона: {len(vio)}")
            lines += [f"• {esc(h)} п.{esc(p)}: ждали VLAN {v}, видим {g}"
                      for h, _ip, p, v, g in vio[:15]]
        send_chunks(chat, lines)
        return
    if a == "trunk":
        tr = trunk_audit()
        lines = [f"🔗 <b>Аудит транков-аплинков</b> (369) · {note}",
                 f"несовпадений VLAN на концах: {len(tr)}"]
        for a_, pa, va, b, pb, vb in tr[:15]:
            lines.append(f"• {esc(a_)} {esc(pa)} {va} ↔ {esc(b)} {esc(pb)} {vb}")
        if not tr:
            lines.append("на всех связках VLAN симметричны ✅ (по MAC-таблицам)")
        send_chunks(chat, lines)
        return
    if a == "fw":
        lines = [f"🧩 <b>Прошивки Cross-24</b> (377) · {note}"]
        for (fw, ld), n in fw_report():
            lines.append(f"• fw {esc(fw)} / loader {esc(ld)}: {n} шт.")
        hw_ips = st.cget("hw_switches") or []
        lines.append(f"Huawei ({len(hw_ips)} в конфиге): /sw_env <ip> покажет VRP")
        send_chunks(chat, lines)
        return
    if a == "time":
        tr = time_report()
        lines = [f"🕰 <b>Часы свитчей</b> (378) — дрейф против даты сборки "
                 f"фактов · {note}"]
        for h, ip, t, d in tr[:25]:
            lines.append(f"• {esc(h)} <code>{ip}</code>: {esc(t)} "
                         f"({d:+.0f} дн.)")
        lines.append("Массовая настройка NTP: /sw_ntp (379, с подтверждением)")
        send_chunks(chat, lines)
        return
    if a == "svc":
        rows = svc_report()
        lines = [f"🚪 <b>Сервисы управления</b> (381/382) · {note}: "
                 f"не по эталону {len(rows)}"]
        for h, ip, probs in rows[:30]:
            lines.append(f"• {esc(h)} <code>{ip}</code>: {esc(', '.join(probs))}")
        send_chunks(chat, lines)
        return
    if a == "contact":
        ct = contact_report()
        lines = [f"📇 <b>sysContact/Location</b> (383) · default у {len(ct)}:"]
        lines += [f"• {esc(h)} <code>{ip}</code> loc={esc(loc or '—')}"
                  for h, ip, _c, loc in ct[:25]]
        lines.append("Заливка location из «Лист1»: /sw_contact fix")
        send_chunks(chat, lines)
        return
    send(chat, "Разделы: /sw_audit [ports|sec|vlan|trunk|fw|time|svc|contact]",
         reply_to=reply_to)


# ---------- 367/366: /stp (живой, один свитч) ----------
def cmd_stp(chat, arg="", reply_to=None):
    r = sw.find_switch(arg)
    if not r or r.get("kind") != "cross24":
        send(chat, "Аудит STP: <code>/stp 10.10.60.52</code> (Cross-24, живой "
                   "опрос)", reply_to=reply_to)
        return
    chat_action(chat)
    g = sw.cross24_get(r["ip"], "stp_global")
    sc = sw.cross24_get(r["ip"], "storm_control")
    n_storm = sum(1 for p in sc.get("ports") or [] if p.get("state"))
    tc_last = sw.lang_args(g.get("tcLastTime"))
    send(chat, f"🌳 <b>STP {esc(r.get('host') or r['ip'])}</b> (367):\n"
               f"STP: {'✅ включён' if g.get('enable') else '⚠️ ВЫКЛЮЧЕН'} · "
               f"режим {esc(g.get('mode'))}\n"
               f"bridge {esc(g.get('bridgeId'))}\n"
               f"root   {esc((g.get('rootBridgeId') or '').strip())}\n"
               f"topology changes: {g.get('tcCnt')} (последняя "
               f"{tc_last if tc_last else '—'} назад)\n"
               f"storm-control (366): включён на {n_storm} портах"
               + ("" if n_storm else " ⚠️ (шторм не ограничен)"),
         reply_to=reply_to)


# ---------- 384: /duplex ----------
def cmd_duplex(chat, arg="", reply_to=None):
    chat_action(chat)
    r = sw.find_switch(arg) if (arg or "").strip() else None
    import bot_store as store
    d = store.jload(st.cget("sw_state_path"), {})
    rows = []
    for ip, e in d.items():
        if ip.startswith("_") or (r and ip != r["ip"]):
            continue
        for p, v in (e.get("ports") or {}).items():
            if v.get("up") and (str(v.get("speed")) == "10" or not v.get("full")):
                rows.append((e.get("host") or ip, ip, p, v.get("speed"),
                             "full" if v.get("full") else "HALF"))
    lines = [f"🐌 <b>Скорость/дуплекс не в норме</b> (384) — из фоновой "
             f"ротации ({len(rows)}):"]
    by_mac = {}
    for e in sw.facts():
        for m in e["mac_table"]:
            by_mac.setdefault((e["ip"], m.get("port")), m.get("mac"))
    for h, ip, p, spd, dup in rows[:25]:
        mac = by_mac.get((ip, p))
        cam = inv.search(mac)[0] if mac and inv.search(mac) else None
        lines.append(f"• {esc(h)} {esc(p)}: {esc(spd)}М {dup}"
                     + (f" — 📷 {esc(cam.get('name') or cam.get('ip'))}" if cam else ""))
    if not rows:
        lines.append("проблемных портов не замечено ✅ (парк обходится ротацией)")
    send_chunks(chat, lines)


# ---------- 379: /sw_ntp (запись с подтверждением) ----------
def cmd_sw_ntp(chat, arg="", reply_to=None):
    srv = str(st.cget("sw_ntp_server") or "").strip()
    tr = [t for t in time_report() if abs(t[3]) > 1]
    if (arg or "").strip().lower() != "fix":
        lines = [f"🕰 <b>NTP на свитчах</b> (379): часы врут более чем на день "
                 f"у {len(tr)} Cross-24 (/sw_audit time).",
                 f"Эталонный сервер (конфиг sw_ntp_server): "
                 f"<code>{esc(srv) or 'НЕ ЗАДАН'}</code>"]
        if srv and tr:
            lines.append(f"Включить SNTP на всех {len(tr)}: /sw_ntp fix "
                         f"(двухшаговое подтверждение)")
        send(chat, "\n".join(lines), reply_to=reply_to)
        return
    if not srv:
        send(chat, "Сначала задай sw_ntp_server в конфиге.", reply_to=reply_to)
        return
    ips = [ip for _h, ip, _t, _d in tr]
    if not ips:
        send(chat, "Часы везде в норме — настраивать нечего.", reply_to=reply_to)
        return
    _confirm.put("ntp", {"ips": ips, "srv": srv})
    send(chat, f"⚠️ <b>Dry-run NTP (379)</b>: на {len(ips)} свитчей будет "
               f"отправлено SET time_time (sntp=1, сервер {esc(srv)}).\n"
               + "\n".join(f"• <code>{ip}</code>" for ip in ips[:10])
               + (f"\n… и ещё {len(ips) - 10}" if len(ips) > 10 else "")
               + "\n⚠️ save НЕ выполняется (запрещён в этой волне) — настройка "
                 "живёт до ребута свитча.\nПодтверди:",
         markup={"inline_keyboard": [[
             {"text": "✅ Настроить NTP", "callback_data": "swntp:ntp"},
             {"text": "✖️ Отмена", "callback_data": "cancel"}]]})


def cb_sw_ntp(chat, cq, payload):
    p = _confirm.take(payload)
    if not p:
        answer_cq(cq.get("id"), "⌛ Подтверждение устарело — повтори /sw_ntp fix")
        return
    answer_cq(cq.get("id"), "🕰 Настраиваю NTP…")
    ok, fail = [], []
    for ip in p["ips"]:
        try:
            sw.cross24_set(ip, "time_time",
                           {"sntp": 1, "srvHost": p["srv"], "port": 123,
                            "timezone": 180, "dlsType": 0})
            ok.append(ip)
            log(f"sw_audit: NTP настроен на {ip} (без save)")
        except Exception as e:
            fail.append(f"{ip}: {e}")
    send(chat, f"🕰 <b>NTP (379)</b>: успешно {len(ok)}, ошибок {len(fail)}\n"
               + ("\n".join(f"❌ {esc(f)[:80]}" for f in fail[:8]))
               + "\n⚠️ Конфиги НЕ сохранялись (save запрещён) — при ребуте "
                 "настройка слетит. Сохранить можно вручную через веб.")
    mx.event_add("sw", "ntp_push", f"ok={len(ok)} fail={len(fail)}", cooldown_h=0)


# ---------- 383: /sw_contact fix (запись с подтверждением) ----------
def cmd_sw_contact(chat, arg="", reply_to=None):
    ct = contact_report()
    rooms = {r["ip"]: r for r in sw.sheet_switches()}
    plan = []
    for h, ip, _c, loc in ct:
        room = (rooms.get(ip) or {}).get("room")
        if room and str(room).strip() and str(room) != str(loc):
            plan.append((ip, h, str(room).strip()))
    if (arg or "").strip().lower() != "fix":
        lines = [f"📇 <b>Заливка sysLocation/Contact</b> (383): default у "
                 f"{len(ct)}, из «Лист1» можно заполнить {len(plan)}:"]
        lines += [f"• <code>{ip}</code> {esc(h)}: location → «{esc(room)}», "
                  f"contact → «МФК Зарядье»" for ip, h, room in plan[:12]]
        if plan:
            lines.append("Применить: /sw_contact fix (подтверждение, без save)")
        send_chunks(chat, lines)
        return
    if not plan:
        send(chat, "Заполнять нечего.", reply_to=reply_to)
        return
    _confirm.put("contact", plan)
    send(chat, f"⚠️ <b>Dry-run (383)</b>: SET sys_sysinfo на {len(plan)} свитчей "
               f"(hostname сохраняется, location из «Лист1», contact "
               f"«МФК Зарядье»). save НЕ выполняется. Подтверди:",
         markup={"inline_keyboard": [[
             {"text": "✅ Залить", "callback_data": "swcontact:contact"},
             {"text": "✖️ Отмена", "callback_data": "cancel"}]]})


def cb_sw_contact(chat, cq, payload):
    plan = _confirm.take(payload)
    if not plan:
        answer_cq(cq.get("id"), "⌛ Устарело — повтори /sw_contact fix")
        return
    answer_cq(cq.get("id"), "📇 Заливаю…")
    ok, fail = 0, []
    for ip, _h, room in plan:
        try:
            cur = sw.cross24_get(ip, "sys_sysinfoEdit")
            sw.cross24_set(ip, "sys_sysinfo",
                           {"hostname": cur.get("hostname") or _h,
                            "location": room, "contact": "МФК Зарядье"})
            ok += 1
            log(f"sw_audit: sysinfo {ip}: location='{room}' (без save)")
        except Exception as e:
            fail.append(f"{ip}: {e}")
    send(chat, f"📇 <b>383</b>: обновлено {ok}, ошибок {len(fail)}\n"
               + "\n".join(f"❌ {esc(f)[:80]}" for f in fail[:8])
               + "\n⚠️ save не выполнялся — сохрани конфиги вручную при случае.")


HANDLERS = {
    "/sw_audit": cmd_sw_audit, "/stp": cmd_stp, "/duplex": cmd_duplex,
    "/sw_ntp": cmd_sw_ntp, "/sw_contact": cmd_sw_contact,
    "/vlan_report": lambda chat, arg="", reply_to=None:
        cmd_sw_audit(chat, "vlan", reply_to=reply_to),
}
ALIASES = {"/аудитсвитч": "/sw_audit", "/впланы": "/vlan_report"}
CALLBACKS = {"swntp": cb_sw_ntp, "swcontact": cb_sw_contact}
