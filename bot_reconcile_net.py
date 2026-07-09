# -*- coding: utf-8 -*-
"""Волна F — сверки инвентаря с внешними источниками:
270 /diff3 — трёхсторонний дифф xlsx↔Sheets↔сеть, 271 /nosnap — камеры без
снимка в Drive-индексе (+кнопка «доснять»), 272 /sheetdrift — детект ручных
правок Google-таблицы, 273-276+290 /reconcile — «Неизвестные», сироты в сети,
порт↔камера по фактам, миграции, мульти-MAC на портах, 277 /cablecheck —
TDR-колонка vs факты (только чтение, TDR НЕ запускается), 298 /enrich —
очередь дообогащения (ONVIF-опрос по кнопке, запись по одной с подтверждением
и автобэкапом через bot_dq.apply_writes), 299 свежесть фактов во всех сверках.
Сеть — только read-only пробы; пароли камер НЕ меняются."""
import re
import json
import time
import threading
import collections

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_dq as dq
import bot_store as store
import bot_reconcile as rc
from bot_tg import send, send_chunks, answer_cq, chat_action
from bot_util import log, log_exc, esc, human_err

_LOCK = threading.Lock()   # single-flight для тяжёлых сверок


# ---------- 272: xlsx vs Google Sheets ----------
def sheets_vs_xlsx(max_diff=400) -> dict:
    """Сравнение листа «Все камеры» в Google и в xlsx: {'diffs': [(ip, поле,
    xlsx, sheets)], 'only_sheets': [...], 'only_xlsx': [...]}."""
    import google_api as g
    from urllib.parse import quote
    sid, sa = st.cget("sheet_id"), st.cget("sa_path")
    r = g.gjson("GET", "https://sheets.googleapis.com/v4/spreadsheets/"
                f"{sid}/values/{quote(inv.SHEET_MAIN)}", sa_path=sa, timeout=60)
    vals = r.get("values") or []
    if not vals:
        return {"diffs": [], "only_sheets": [], "only_xlsx": [], "empty": True}
    hdr = [str(h) for h in vals[0]]
    ipc = hdr.index("IP-адрес") if "IP-адрес" in hdr else None
    gs = {}
    for row in vals[1:]:
        ip = str(row[ipc]).strip() if ipc is not None and ipc < len(row) else ""
        if ip:
            gs[ip] = {h: (str(row[i]) if i < len(row) else "")
                      for i, h in enumerate(hdr)}
    d = dq.read_all().get(inv.SHEET_MAIN) or {"hdr": [], "rows": []}
    xl = {}
    for row in d["rows"]:
        ip = str(dq._cell(row, d["hdr"], "IP-адрес") or "").strip()
        if ip:
            xl[ip] = {h: ("" if (i >= len(row) or row[i] is None)
                          else str(row[i]))
                      for i, h in enumerate(d["hdr"]) if h}
    skip_pref = ("Online", "Проверка")  # колонки, которые дописывает /sync
    diffs = []
    for ip in sorted(set(gs) & set(xl)):
        for f in xl[ip]:
            if f.startswith(skip_pref):
                continue
            a, b = xl[ip].get(f, ""), gs[ip].get(f, "")
            if a != b:
                diffs.append((ip, f, a, b))
                if len(diffs) >= max_diff:
                    break
        if len(diffs) >= max_diff:
            break
    return {"diffs": diffs, "only_sheets": sorted(set(gs) - set(xl)),
            "only_xlsx": sorted(set(xl) - set(gs)), "empty": False}


def cmd_sheetdrift(chat, arg="", reply_to=None):
    chat_action(chat)
    send(chat, "🔍 Читаю Google-таблицу и сравниваю с xlsx…", silent=True,
         reply_to=reply_to)
    try:
        r = sheets_vs_xlsx()
    except Exception as e:
        log_exc("/sheetdrift")
        send(chat, human_err("Не смог прочитать Google-таблицу", e))
        return
    if r.get("empty"):
        send(chat, "Google-лист пуст — сначала /sync.", reply_to=reply_to)
        return
    n = len(r["diffs"])
    if not (n or r["only_sheets"] or r["only_xlsx"]):
        send(chat, "🔍 Google-таблица совпадает с xlsx ✅ — ручных правок нет.",
             reply_to=reply_to)
        return
    lines = [f"🔍 <b>Расхождения Google ↔ xlsx</b>: ячеек {n} · строк только "
             f"в Google: {len(r['only_sheets'])} · только в xlsx: "
             f"{len(r['only_xlsx'])}"]
    for ip, f, a, b in r["diffs"][:20]:
        lines.append(f"• <code>{ip}</code> {esc(f)}: xlsx «{esc(a[:28])}» ≠ "
                     f"таблица «{esc(b[:28])}»")
    if n > 20:
        lines.append(f"… и ещё {n - 20}")
    lines.append("\n⚠️ /sync затрёт таблицу значениями xlsx. Принять правку в "
                 "xlsx можно точечно: /note <ip> … (или руками).")
    send_chunks(chat, lines)
    send(chat, "Затереть таблицу данными xlsx?", silent=True,
         markup={"inline_keyboard": [[
             {"text": "☁️ Да, /sync", "callback_data": "drsync:go"},
             {"text": "✖️ Оставить", "callback_data": "cancel"}]]})


def cb_drsync(chat, cq, _payload):
    answer_cq(cq.get("id"), "☁️ Запускаю /sync…")
    import bot_handlers_inv
    bot_handlers_inv.cmd_sync(chat, "")


# ---------- 270: /diff3 ----------
def cmd_diff3(chat, arg="", reply_to=None):
    if not _LOCK.acquire(blocking=False):
        send(chat, "⏳ Сверка уже идёт — подожди.", reply_to=reply_to)
        return
    try:
        chat_action(chat)
        prefixes = [p for p in ((arg or "").split() or st.cget("diff_subnets"))
                    if net.valid_prefix(p) and net.prefix_allowed(p)]
        send(chat, f"🔺 Трёхсторонний дифф xlsx ↔ Sheets ↔ сеть "
                   f"({esc(' '.join(prefixes))}) — скан + чтение таблицы…",
             silent=True, reply_to=reply_to)
        live = {}
        for p in prefixes:
            live.update(net.scan_subnet(p))
        xl_ips = {c["ip"] for c in inv.cams() if c.get("ip")
                  and any(c["ip"].startswith(p + ".") for p in prefixes)}
        try:
            gs = sheets_vs_xlsx()
        except Exception as e:
            log_exc("/diff3 sheets")
            gs = {"diffs": [], "only_sheets": [], "only_xlsx": [],
                  "err": str(e)}
        only_net = sorted(set(live) - xl_ips,
                          key=lambda x: tuple(map(int, x.split("."))))
        only_xlsx_net = sorted(xl_ips - set(live))
        lines = [f"🔺 <b>xlsx ↔ Sheets ↔ сеть</b>",
                 f"в xlsx: {len(xl_ips)} · живых в сети: {len(live)} · "
                 f"конфликтных ячеек Sheets: {len(gs['diffs'])}"]
        if gs.get("err"):
            lines.append(f"⚠️ Sheets не прочитались: {esc(gs['err'][:120])}")
        if only_net:
            lines.append(f"\n🆕 <b>Только в сети</b> (сироты, {len(only_net)}):")
            lines += [f"• <code>{ip}</code> {esc(live[ip])}"
                      for ip in only_net[:15]]
        if only_xlsx_net:
            lines.append(f"\n❌ <b>Только в xlsx</b> (в сети молчат, "
                         f"{len(only_xlsx_net)}):")
            lines += [f"• <code>{ip}</code> {esc(inv.label(ip) or '')}"
                      for ip in only_xlsx_net[:15]]
        if gs["only_sheets"]:
            lines.append(f"\n📗 <b>Только в Sheets</b> (ручная правка?): "
                         + ", ".join(f"<code>{i}</code>"
                                     for i in gs["only_sheets"][:10]))
        if gs["diffs"]:
            lines.append(f"\n✏️ <b>Конфликты значений</b> "
                         f"({len(gs['diffs'])}, детали /sheetdrift):")
            lines += [f"• <code>{ip}</code> {esc(f)}" for ip, f, _a, _b
                      in gs["diffs"][:10]]
        fresh = rc.freshness_note()
        if fresh:
            lines.append("\n" + fresh)
        if len(lines) == 2:
            lines.append("✅ Три источника согласованы.")
        send_chunks(chat, lines)
    finally:
        _LOCK.release()


# ---------- 273-276 + 290: /reconcile ----------
def _rec_unknown(lines) -> list:
    """273: MAC из «Неизвестных» уже в «Все камеры» -> кандидаты + writes."""
    inv_macs = {c["nmac"] for c in inv.cams() if c.get("nmac")}
    hdr, rows = inv.unknown_devices()
    writes = []
    try:
        mac_i = hdr.index("MAC-адрес")
        note_i = hdr.index("Примечание") if "Примечание" in hdr else None
    except ValueError:
        lines.append("Лист «Неизвестные устройства» без колонки MAC.")
        return []
    found = []
    for rn, row in enumerate(rows, start=2):
        m = inv.norm_mac(row[mac_i] if mac_i < len(row) else "")
        if m and m in inv_macs:
            note = str(row[note_i] or "") if note_i is not None \
                and note_i < len(row) else ""
            found.append((rn, row[mac_i], note))
            if "идентифицирован" not in note.lower():
                writes.append(("Неизвестные устройства", rn, "Примечание",
                               note, (note + "; " if note else "")
                               + "идентифицирован (есть в «Все камеры»)"))
    lines.append(f"❔→📒 <b>«Неизвестные» уже в инвентаре</b>: {len(found)} "
                 f"из {len(rows)} строк — кандидаты на удаление из листа:")
    for rn, mac, _n in found[:15]:
        lbl = next((c for c in inv.cams()
                    if c.get("nmac") == inv.norm_mac(mac)), {})
        lines.append(f"• стр.{rn} <code>{esc(mac)}</code> = "
                     f"{esc(lbl.get('name') or lbl.get('ip') or '?')}")
    if len(found) > 15:
        lines.append(f"… и ещё {len(found) - 15}")
    return writes


def _rec_ports(lines) -> None:
    """275: MAC камеры реально числится на записанном порту? (по фактам)"""
    bad, checked = [], 0
    for c in inv.cams():
        if not (c.get("mac") and c.get("sw_ip") and c.get("port")):
            continue
        hits = inv.switch_ports(c["mac"])
        if not hits:
            continue
        checked += 1
        norm = lambda p: re.sub(r"\s", "", str(p or "")).upper()
        if not any(str(h["sw_ip"]) == str(c["sw_ip"])
                   and norm(h["port"]) == norm(c["port"]) for h in hits):
            best = hits[0]
            bad.append((c, best))
    lines.append(f"\n🔌 <b>Порт↔камера по фактам</b>: проверено {checked}, "
                 f"расхождений <b>{len(bad)}</b>"
                 + (" (перепатчили или ошибка журнала):" if bad else " ✅"))
    for c, h in bad[:15]:
        lines.append(f"• <code>{c['ip']}</code> {esc(c.get('name') or '')}: "
                     f"в xlsx {esc(c.get('switch') or c.get('sw_ip'))} "
                     f"п.{esc(c.get('port'))}, факты видят "
                     f"{esc(h['host'])} п.{esc(h['port'])}")
    if len(bad) > 15:
        lines.append(f"… и ещё {len(bad) - 15}")


def _rec_moves(lines) -> None:
    """276: миграции MAC между срезами фактов (нужен _facts_switches.prev.json)."""
    prev_path = st.cget("facts_switches").replace(".json", ".prev.json")
    try:
        with open(prev_path, encoding="utf-8") as f:
            prev = json.load(f)
    except OSError:
        lines.append("\n🚚 <b>Миграции камер</b>: нет данных — прежнего среза "
                     f"фактов нет (<code>{esc(prev_path.rsplit(chr(92), 1)[-1])}"
                     "</code>). Сохрани копию перед новым свипом.")
        return
    old = {}
    for e in prev:
        if not e.get("ok"):
            continue
        for m in e.get("mac_table") or []:
            nm = inv.norm_mac(m.get("mac"))
            if nm:
                old.setdefault(nm, set()).add((e["ip"], str(m.get("port"))))
    moves = []
    for c in inv.cams():
        if not c.get("nmac") or c["nmac"] not in old:
            continue
        cur = {(h["sw_ip"], str(h["port"]))
               for h in inv.switch_ports(c.get("mac"))}
        if cur and not (cur & old[c["nmac"]]):
            was = ", ".join(f"{s} п.{p}" for s, p in sorted(old[c["nmac"]])[:2])
            now = ", ".join(f"{s} п.{p}" for s, p in sorted(cur)[:2])
            moves.append((c, was, now))
    lines.append(f"\n🚚 <b>Миграции камер</b> (старый срез vs текущий): "
                 f"{len(moves)}" + ("" if moves else " ✅"))
    for c, was, now in moves[:12]:
        lines.append(f"• <code>{c['ip']}</code> {esc(c.get('name') or '')}: "
                     f"переехала, было {esc(was)} → {esc(now)}")


def _rec_multimac(lines) -> None:
    """290: access-порты с несколькими MAC (нелегальные свичи/каскады)."""
    try:
        with open(st.cget("facts_switches"), encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        lines.append("\n👥 Мульти-MAC: фактов коммутаторов нет.")
        return
    inv_by_mac = {c["nmac"]: c for c in inv.cams() if c.get("nmac")}
    sus = []
    for e in data:
        if not e.get("ok"):
            continue
        host = (e.get("sys") or {}).get("hostname") or e.get("ip")
        per = collections.defaultdict(list)
        for m in e.get("mac_table") or []:
            per[str(m.get("port"))].append(inv.norm_mac(m.get("mac")))
        for port, macs in per.items():
            cam_macs = [m for m in macs if m in inv_by_mac]
            if cam_macs and 2 <= len(macs) <= 4:  # >4 — скорее uplink
                sus.append((host, e["ip"], port, macs, cam_macs))
    lines.append(f"\n👥 <b>Несколько MAC на камерном порту</b> (2-4, риск для "
                 f"PoE-операций): {len(sus)}" + ("" if sus else " ✅"))
    for host, ip, port, macs, cam_macs in sus[:12]:
        cams_s = ", ".join(inv_by_mac[m].get("ip") or "?" for m in cam_macs)
        lines.append(f"• {esc(host)} п.{esc(port)}: {len(macs)} MAC, "
                     f"камеры: {esc(cams_s)}")


def cmd_reconcile(chat, arg="", reply_to=None):
    sub = (arg or "").strip().lower()
    chat_action(chat)
    if sub in ("orphans", "сироты"):
        cmd_orphans(chat, "", reply_to=reply_to)
        return
    lines = ["♻️ <b>Реконсиляция инвентаря</b>"]
    writes = []
    if sub in ("", "unknown", "неизвестные"):
        writes = _rec_unknown(lines)
    if sub in ("", "ports", "порты"):
        _rec_ports(lines)
    if sub in ("", "moves", "миграции"):
        _rec_moves(lines)
    if sub in ("", "multi", "мультимак"):
        _rec_multimac(lines)
    fresh = rc.freshness_note()
    if fresh:
        lines.append("\n" + fresh)
    lines.append("\nРазделы: /reconcile [unknown|ports|moves|multi|orphans]")
    send_chunks(chat, lines)
    if writes:
        import bot_dq_cmds
        tok = bot_dq_cmds._stash("recunk", writes)
        send(chat, f"Пометить {len(writes)} строк «Неизвестных» как "
                   f"«идентифицирован» (с бэкапом)?", silent=True,
             markup={"inline_keyboard": [[
                 {"text": f"🏷 Пометить ({len(writes)})",
                  "callback_data": f"recu:{tok}"},
                 {"text": "✖️ Отмена", "callback_data": "cancel"}]]})


def cb_recu(chat, cq, tok):
    import bot_dq_cmds
    writes = bot_dq_cmds._take(tok, "recunk")
    if writes is None:
        answer_cq(cq.get("id"), "⌛ Устарело — повтори /reconcile")
        return
    answer_cq(cq.get("id"), "💾 Пишу с бэкапом…")
    try:
        bak = dq.apply_writes(writes, tag="reconcile", who=chat)
    except Exception as e:
        log_exc("reconcile unknown apply")
        send(chat, human_err("Пометка не записалась", e))
        return
    import os
    send(chat, f"🏷 Помечено {len(writes)} строк ✅ · бэкап "
               f"<code>{esc(os.path.basename(bak))}</code>. Удаление строк — "
               f"вручную в xlsx (бот строки не удаляет).")


def cmd_orphans(chat, arg="", reply_to=None):
    """274: живые IP камерных подсетей, которых нет ни в одном листе."""
    if not _LOCK.acquire(blocking=False):
        send(chat, "⏳ Сверка уже идёт — подожди.", reply_to=reply_to)
        return
    try:
        chat_action(chat)
        prefixes = [p for p in st.cget("diff_subnets")
                    if net.valid_prefix(p) and net.prefix_allowed(p)]
        send(chat, f"🕵️ Ищу сирот: TCP-скан {esc(' '.join(prefixes))} + ARP…",
             silent=True, reply_to=reply_to)
        live = {}
        for p in prefixes:
            live.update(net.scan_subnet(p))
        known_ips = {c["ip"] for c in inv.cams() if c.get("ip")}
        hdr, rows = inv.unknown_devices()
        unk_macs = set()
        if "MAC-адрес" in hdr:
            i = hdr.index("MAC-адрес")
            unk_macs = {inv.norm_mac(r[i]) for r in rows if i < len(r)} - {""}
        arp = net.arp_table()
        orphans = []
        for ip in sorted(set(live) - known_ips,
                         key=lambda x: tuple(map(int, x.split(".")))):
            mac = arp.get(ip) or (live.get(ip) if ":" in str(live.get(ip))
                                  else "") or ""
            nm = inv.norm_mac(mac)
            ports = inv.switch_ports(mac) if mac else []
            p0 = ports[0] if ports else {}
            orphans.append((ip, mac, net.vendor(mac) if mac else "?",
                            nm in unk_macs, p0))
        lines = [f"🕵️ <b>Сироты в сети</b>: {len(orphans)} живых IP вне "
                 f"инвентаря (живых всего {len(live)})"]
        for ip, mac, ven, in_unk, p0 in orphans[:25]:
            sw = (f" · {esc(p0.get('host') or '')} п.{esc(p0.get('port'))}"
                  if p0 else "")
            lines.append(f"• <code>{ip}</code> <code>{esc(mac or '?')}</code> "
                         f"{esc(ven)}{sw}"
                         + (" · есть в «Неизвестных»" if in_unk else ""))
        if len(orphans) > 25:
            lines.append(f"… и ещё {len(orphans) - 25}")
        if not orphans:
            lines.append("✅ Всё живое в этих подсетях учтено.")
        fresh = rc.freshness_note()
        if fresh:
            lines.append(fresh)
        send_chunks(chat, lines)
    finally:
        _LOCK.release()


# ---------- 277: /cablecheck ----------
def cmd_cablecheck(chat, arg="", reply_to=None):
    """Только чтение: колонка «Длина кабеля» vs TDR-факты, если они есть.
    TDR отсюда НЕ запускается (роняет линк — методика PoE-off отдельно)."""
    try:
        with open(st.cget("facts_switches"), encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        data = []
    tdr = {}
    for e in data:
        for t in (e.get("tdr") or []):
            m = inv.norm_mac(t.get("mac"))
            if m and t.get("len_m") is not None:
                tdr[m] = float(t["len_m"])
    cams = inv.cams()
    with_len = [c for c in cams if c.get("cable") not in (None, "")]
    lines = [f"📏 <b>Сверка длины кабеля</b>: колонка заполнена у "
             f"{len(with_len)}/{len(cams)}"]
    if not tdr:
        lines.append("TDR-замеров в фактах нет — «нет данных». Замер делается "
                     "отдельно (PoE-off методика из справочника), бот TDR "
                     "НЕ запускает.")
        send(chat, "\n".join(lines), reply_to=reply_to)
        return
    bad = []
    for c in with_len:
        m = tdr.get(c.get("nmac") or "")
        if m is None:
            continue
        try:
            jl = float(str(c["cable"]).replace(",", "."))
        except ValueError:
            continue
        if jl and abs(jl - m) / max(jl, 1) > 0.15:
            bad.append((c, jl, m))
    lines.append(f"TDR-замеров: {len(tdr)} · дельта >15%: <b>{len(bad)}</b>")
    for c, a, b in bad[:15]:
        lines.append(f"• <code>{c['ip']}</code> журнал {a:.0f} м ≠ TDR {b:.0f} м")
    fresh = rc.freshness_note()
    if fresh:
        lines.append(fresh)
    send_chunks(chat, lines)


HANDLERS = {
    "/diff3": cmd_diff3, "/sheetdrift": cmd_sheetdrift,
    "/reconcile": cmd_reconcile, "/orphans": cmd_orphans,
    "/cablecheck": cmd_cablecheck,
}
ALIASES = {"/сироты": "/orphans", "/реконсиляция": "/reconcile"}
CALLBACKS = {"drsync": cb_drsync, "recu": cb_recu}
