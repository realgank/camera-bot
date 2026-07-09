# -*- coding: utf-8 -*-
"""Волна H — топология сети (351-354, 374-375, 395):
351 /topo <ip|имя> — цепочка «камера → порт → свитч → аплинк → ядро»;
352 карта межсвитчевых связей из LLDP (chassisId ↔ sysMac) деревом;
353 /topo_map — SVG-схема (самописный layout, stdlib), документом;
354 /topo unknown — неизвестные LLDP-соседи (пустой sysName) с OUI;
374 /sw — карточка свитча; 375 /sw_list — реестр с доступностью;
395 /patchpanel — xlsx «свитч/порт → камера → локация».
Всё read-only, источник — _facts_switches.json + «Лист1» + инвентарь."""
import io
import re
import time
import datetime

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_sw_api as sw
from bot_tg import send, send_chunks, send_document, chat_action
from bot_util import log_exc, esc

_PORT_ID_RE = re.compile(r"^(gi|te|ge|xge)\d+", re.I)


# ---------- 352: связи между свитчами ----------
def links() -> list:
    """[(ip_a, port_a, ip_b, port_b)] из LLDP-соседств (дедуп по паре ip)."""
    macs = sw.switch_macs()
    hosts = {str(e["host"]): e["ip"] for e in sw.facts() if e.get("host")}
    out, seen = [], set()
    for e in sw.facts():
        if not e["ok"]:
            continue
        for n in e["lldp"]:
            other = macs.get(n["id"]) or hosts.get(n["name"])
            if not other or other == e["ip"]:
                continue
            key = tuple(sorted((e["ip"], other)))
            if key in seen:
                continue
            seen.add(key)
            out.append((e["ip"], n["port"], other, n["port_id"]))
    return out


def neighbors_map() -> dict:
    nb: dict = {}
    for a, pa, b, pb in links():
        nb.setdefault(a, []).append((pa, b, pb))
        nb.setdefault(b, []).append((pb, a, pa))
    return nb


def core_ip():
    """Ядро = свитч с максимумом межсвитчевых связей."""
    nb = neighbors_map()
    if not nb:
        return None
    return max(nb, key=lambda ip: len(nb[ip]))


def uplink_ports(ip: str) -> list:
    """Порты свитча, ведущие к другим свитчам (аплинки): по связям links()
    плюс LLDP-соседи, похожие на свитч (portId вида gi14/te2 или OUI
    Cross-24 AE:31:9D — свитч, которого нет в фактах)."""
    out = [pa for a, pa, _b, _pb in links() if a == ip] + \
          [pb for _a, _pa, b, pb in links() if b == ip]
    e = sw.by_ip(ip)
    for n in (e or {}).get("lldp") or []:
        if _PORT_ID_RE.match(n.get("port_id") or "") \
                or (n.get("id") or "").startswith("AE:31:9D"):
            out.append(n["port"])
    return sorted(set(out))


def cams_by_switch() -> dict:
    """sw_ip -> [записи инвентаря камер] (по колонке «IP коммутатора»)."""
    out: dict = {}
    for r in inv.cams():
        sip = str(r.get("sw_ip") or "").strip()
        if sip:
            out.setdefault(sip, []).append(r)
    return out


def host_of(ip: str) -> str:
    e = sw.by_ip(ip)
    return (e or {}).get("host") or ip


def tree_lines() -> list:
    """352: дерево свитчей от ядра, с числом камер на каждом."""
    nb = neighbors_map()
    root = core_ip()
    if not root:
        return ["Связи LLDP между свитчами не найдены — обнови факты (/facts_refresh)."]
    cams = cams_by_switch()
    lines, seen = [], set()

    def walk(ip, depth, via):
        seen.add(ip)
        pre = "  " * depth + ("└ " if depth else "◉ ")
        n = len(cams.get(ip, []))
        lines.append(f"{pre}{host_of(ip)} {ip}{via}"
                     + (f" · 📷{n}" if n else ""))
        for pa, b, pb in sorted(nb.get(ip, [])):
            if b not in seen:
                walk(b, depth + 1, f" [{pa}→{pb}]")

    walk(root, 0, " (ядро)")
    rest = [ip for ip in {e['ip'] for e in sw.facts() if e['ok']} if ip not in seen]
    for ip in sorted(rest):
        n = len(cams.get(ip, []))
        lines.append(f"· {host_of(ip)} {ip} (без LLDP-связей)"
                     + (f" · 📷{n}" if n else ""))
    return lines


# ---------- 354: неизвестные LLDP-соседи ----------
def unknown_neighbors() -> list:
    """[{sw, port, mac, vendor, port_id}] — соседи без sysName и не наши."""
    macs = sw.switch_macs()
    inv_macs = {c["nmac"] for c in inv.cams() if c.get("nmac")}
    out = []
    for e in sw.facts():
        for n in e["lldp"]:
            if n["name"]:
                continue
            mid = n["id"]
            if mid in macs or inv.norm_mac(mid) in inv_macs:
                continue
            out.append({"sw": e["host"], "sw_ip": e["ip"], "port": n["port"],
                        "mac": mid, "vendor": net.vendor(mid.lower()),
                        "port_id": n["port_id"]})
    return out


# ---------- 351: /topo ----------
def chain_for_cam(ip: str) -> list:
    """Цепочка камеры до ядра: [строки]."""
    rec = inv.get(ip) or {}
    lines = [f"📷 <b>{esc(rec.get('name') or ip)}</b> <code>{ip}</code>"]
    hits = inv.switch_ports(rec.get("mac")) if rec.get("mac") else []
    if hits:
        h = hits[0]
        lines.append(f"└ 🔌 порт <b>{esc(h['port'])}</b> @ {esc(h['host'])} "
                     f"<code>{h['sw_ip']}</code> (по фактам, MAC на порту)")
        cur = h["sw_ip"]
    elif rec.get("sw_ip"):
        lines.append(f"└ 🔌 порт {esc(rec.get('port') or '?')} @ "
                     f"<code>{esc(rec['sw_ip'])}</code> (по инвентарю, "
                     f"в фактах MAC не найден)")
        cur = str(rec["sw_ip"]).strip()
    else:
        lines.append("└ ❔ свитч неизвестен (нет MAC в фактах и порта в инвентаре)")
        return lines
    nb = neighbors_map()
    root = core_ip()
    depth, seen = 0, {cur}
    while cur != root and depth < 8:
        nxt = None
        for pa, b, pb in nb.get(cur, []):
            if b not in seen:
                nxt = (pa, b, pb)
                break
        if not nxt:
            break
        pa, b, pb = nxt
        seen.add(b)
        mark = " (ядро)" if b == root else ""
        lines.append("  " * (depth + 1)
                     + f"└ ⬆️ {esc(pa)} → {esc(host_of(b))} <code>{b}</code> "
                       f"п.{esc(pb)}{mark}")
        cur = b
        depth += 1
    return lines


def cmd_topo(chat, arg="", reply_to=None):
    a = (arg or "").strip()
    if a.lower() in ("unknown", "неизвестные"):
        u = unknown_neighbors()
        lines = [f"❔ <b>Неизвестные LLDP-соседи</b> (354): {len(u)} · {esc(sw.age_note())}"]
        for x in u[:30]:
            lines.append(f"• {esc(x['sw'])} п.{esc(x['port'])}: "
                         f"<code>{esc(x['mac'])}</code> {esc(x['vendor'])}"
                         + (f" · {esc(x['port_id'])}"
                            if x["port_id"] and x["port_id"] != x["mac"] else ""))
        if len(u) > 30:
            lines.append(f"… и ещё {len(u) - 30}")
        send_chunks(chat, lines)
        return
    if not a:
        lines = [f"🕸 <b>Топология свитчей</b> (LLDP) · {esc(sw.age_note())}",
                 f"<pre>{esc(chr(10).join(tree_lines()))}</pre>",
                 "Цепочка камеры: /topo <code>ip|имя</code> · SVG: /topo_map · "
                 "/topo unknown"]
        send(chat, "\n".join(lines), reply_to=reply_to)
        return
    ip = a if net.valid_ip(a) else inv.resolve_ip(a)
    if not ip:
        send(chat, f"Камера «{esc(a)}» не найдена. /topo — дерево свитчей.",
             reply_to=reply_to)
        return
    lines = chain_for_cam(ip)
    lines.append(f"🕒 {esc(sw.age_note())}")
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- 353: /topo_map (SVG) ----------
def svg_map() -> str:
    """SVG-схема дерева свитчей (ядро сверху, ярусы BFS)."""
    nb = neighbors_map()
    root = core_ip()
    cams = cams_by_switch()
    levels, seen = [], set()
    cur = [root] if root else []
    seen.update(cur)
    while cur:
        levels.append(cur)
        nxt = []
        for ip in cur:
            for _pa, b, _pb in sorted(nb.get(ip, [])):
                if b not in seen:
                    seen.add(b)
                    nxt.append(b)
        cur = nxt
    rest = sorted(ip for ip in {e['ip'] for e in sw.facts() if e['ok']}
                  if ip not in seen)
    if rest:
        levels.append(rest)
    bw, bh, gx, gy = 150, 40, 14, 70
    width = max((len(lv) for lv in levels), default=1) * (bw + gx) + gx
    height = len(levels) * (bh + gy) + gy
    pos = {}
    for li, lv in enumerate(levels):
        x0 = (width - len(lv) * (bw + gx)) / 2
        for i, ip in enumerate(lv):
            pos[ip] = (x0 + i * (bw + gx), gy / 2 + li * (bh + gy))
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
         f'height="{height:.0f}" font-family="monospace" font-size="11">',
         '<rect width="100%" height="100%" fill="white"/>']
    for a, pa, b, pb in links():
        if a in pos and b in pos:
            xa, ya = pos[a][0] + bw / 2, pos[a][1] + bh
            xb, yb = pos[b][0] + bw / 2, pos[b][1]
            if ya > yb:
                (xa, ya), (xb, yb) = (xb, yb), (xa, ya)
            p.append(f'<line x1="{xa:.0f}" y1="{ya:.0f}" x2="{xb:.0f}" '
                     f'y2="{yb:.0f}" stroke="#888"/>')
    for ip, (x, y) in pos.items():
        n = len(cams.get(ip, []))
        fill = "#dbeafe" if ip == root else ("#dcfce7" if n else "#f3f4f6")
        p.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{bw}" height="{bh}" '
                 f'rx="6" fill="{fill}" stroke="#555"/>')
        p.append(f'<text x="{x + 6:.0f}" y="{y + 16:.0f}">{host_of(ip)}</text>')
        p.append(f'<text x="{x + 6:.0f}" y="{y + 32:.0f}" fill="#555">'
                 f'{ip}{" · 📷" + str(n) if n else ""}</text>')
    p.append("</svg>")
    return "\n".join(p)


def cmd_topo_map(chat, arg="", reply_to=None):
    chat_action(chat, "upload_document")
    data = svg_map().encode("utf-8")
    fn = f"topo_{datetime.date.today().isoformat()}.svg"
    send_document(chat, data, fn,
                  caption=f"🕸 Схема топологии (LLDP) · {sw.age_note()}")


# ---------- 374: /sw ----------
def cmd_sw(chat, arg="", reply_to=None):
    r = sw.find_switch(arg)
    if not r:
        send(chat, "Карточка свитча: <code>/sw 10.10.60.52</code> или по имени. "
                   "Реестр: /sw_list", reply_to=reply_to)
        return
    ip = r["ip"]
    e = sw.by_ip(ip)
    lines = [f"🔌 <b>{esc(r.get('host') or ip)}</b> — <code>{ip}</code> "
             f"({'Cross-24' if r.get('kind') == 'cross24' else 'Huawei'})"]
    if r.get("room") or r.get("floor") is not None:
        lines.append(f"📍 {esc(r.get('room') or '?')} · этаж {esc(r.get('floor'))}")
    if r.get("sn"):
        lines.append(f"#️⃣ sn <code>{esc(r['sn'])}</code>")
    if e and e["ok"]:
        up = e.get("uptime_s")
        up_s = f"{up // 86400}д {(up % 86400) // 3600}ч" if up else "?"
        lines.append(f"🧩 fw {esc(e.get('fw'))} · loader {esc(e.get('loader'))}")
        lines.append(f"⏱ аптайм на момент фактов: {up_s} · часы свитча: "
                     f"{esc(e.get('time_str') or '?')}")
        m = e.get("methods") or {}
        on = [k for k, v in m.items() if v]
        lines.append(f"🚪 сервисы: {esc(', '.join(on) or '—')}")
        lines.append(f"🧮 MAC в таблице: {e.get('macs_n')} · LLDP-соседей: "
                     f"{len(e.get('lldp') or [])}")
        ups = uplink_ports(ip)
        if ups:
            lines.append(f"⬆️ аплинки: {esc(', '.join(sorted(set(ups))))}")
    elif e:
        lines.append(f"⚠️ в фактах помечен недоступным: {esc(str(e.get('err'))[:120])}")
    cams = cams_by_switch().get(ip) or []
    if cams:
        lines.append(f"📷 камер по инвентарю: {len(cams)} — /switchcams {ip}")
    alive = net.tcp_alive(ip, ports=(80, 22), t=1.0)
    lines.append(f"{'🟢 отвечает (80/22)' if alive else '🔴 сейчас не отвечает'} "
                 f"· {esc(sw.age_note())}")
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- 375: /sw_list ----------
def cmd_sw_list(chat, arg="", reply_to=None):
    chat_action(chat)
    reg = sw.registry()
    ips = [r["ip"] for r in reg]
    pmap = net.probe_many(ips, ports=(80, 22))
    cams = cams_by_switch()
    rows, n_off = [], 0
    for r in reg:
        p = pmap.get(r["ip"]) or []
        ok = bool(p)
        n_off += 0 if ok else 1
        rows.append(f"{'OK ' if ok else '!! '}{(r.get('host') or '?')[:10]:<10} "
                    f"{r['ip']:<13} {('fw ' + str(r.get('fw')))[:12]:<12} "
                    f"📷{len(cams.get(r['ip'], [])):<3} "
                    f"{'web' if 80 in p else '':<3} {'ssh' if 22 in p else ''}")
    head = (f"🗂 <b>Реестр свитчей</b>: {len(reg)} "
            f"(недоступно {n_off}) · {esc(sw.age_note())}")
    buf = []
    for row in rows:
        buf.append(row)
        if len("\n".join(buf)) > 3200:
            send(chat, head + f"\n<pre>{esc(chr(10).join(buf))}</pre>")
            buf, head = [], "…"
    if buf:
        send(chat, head + f"\n<pre>{esc(chr(10).join(buf))}</pre>",
             reply_to=reply_to)


# ---------- 395: /patchpanel ----------
def cmd_patchpanel(chat, arg="", reply_to=None):
    """xlsx «свитч / порт → камера → локация» из фактов + инвентаря."""
    chat_action(chat, "upload_document")
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Кроссировка"
    ws.append(["Свитч", "IP свитча", "Порт", "VLAN", "Камера", "IP камеры",
               "Локация", "Источник"])
    by_mac = {c["nmac"]: c for c in inv.cams() if c.get("nmac")}
    n = 0
    for e in sw.facts():
        if not e["ok"]:
            continue
        ups = set(uplink_ports(e["ip"]))
        by_port: dict = {}
        for m in e["mac_table"]:
            by_port.setdefault(m.get("port"), []).append(m)
        for port in sorted(by_port, key=lambda p: (sw.port_index(p) is None,
                                                   sw.port_index(p) or 0)):
            if port in ups:
                continue
            for m in by_port[port]:
                rec = by_mac.get(inv.norm_mac(m.get("mac")))
                if rec:
                    ws.append([e["host"], e["ip"], port, m.get("vlan"),
                               rec.get("name"), rec.get("ip"),
                               rec.get("location"), "факты+инвентарь"])
                    n += 1
    for r in inv.cams():  # камеры, чей MAC не найден в фактах
        if r.get("sw_ip") and r.get("nmac") and r["nmac"] not in {
                inv.norm_mac(m.get("mac")) for e in sw.facts()
                for m in e["mac_table"]}:
            ws.append([r.get("switch"), r.get("sw_ip"), r.get("port"),
                       r.get("vlan"), r.get("name"), r.get("ip"),
                       r.get("location"), "только инвентарь"])
            n += 1
    ws.auto_filter.ref = ws.dimensions
    bio = io.BytesIO()
    wb.save(bio)
    fn = f"патч-панели_{datetime.date.today().isoformat()}.xlsx"
    send_document(chat, bio.getvalue(), fn,
                  caption=f"🔀 Кроссировка: {n} строк · {sw.age_note()}")


HANDLERS = {
    "/topo": cmd_topo, "/topo_map": cmd_topo_map,
    "/sw": cmd_sw, "/sw_list": cmd_sw_list, "/patchpanel": cmd_patchpanel,
}
ALIASES = {
    "/топо": "/topo", "/свитч": "/sw", "/свитчи": "/sw_list",
    "/кросс": "/patchpanel",
}
CALLBACKS: dict = {}
