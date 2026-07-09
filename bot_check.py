# -*- coding: utf-8 -*-
"""Волна D — чек-листы и обход: 179 /lost (мастер «камера пропала» с
автопроверками), 180 /accept (приёмка после монтажа), 181 конфигурируемые
сценарии в checklists.json (шаги auto/manual без правки кода), 182 /patrol
(обход зоны по одной камере с отметками и прогрессом), 183 акт обхода в xlsx
документом. Пароли камер НЕ меняются."""
import io
import time
import threading
import datetime

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_store as store
from bot_tg import send, send_photo, send_document, chat_action, answer_cq
from bot_util import log, log_exc, esc

_BUILTIN = {
    "lost": {"title": "Камера пропала", "steps": [
        {"auto": "ping", "title": "Ping камеры"},
        {"auto": "arp", "title": "ARP-запись (виден ли MAC в сети)"},
        {"auto": "ports", "title": "TCP-порты 80/554/8080"},
        {"auto": "switch", "title": "Куда подключена (инвентарь)"},
        {"manual": True, "title": "Проверь линк физически",
         "hint": "LED на порту свитча, PoE-бюджет, патч-корд в шкафу"},
    ]},
    "accept": {"title": "Приёмка после монтажа", "steps": [
        {"auto": "ping", "title": "Ping камеры"},
        {"auto": "onvif", "title": "ONVIF отвечает (модель/прошивка)"},
        {"auto": "rtsp", "title": "RTSP-URL отдаётся"},
        {"auto": "snapshot", "title": "Снимок получается"},
        {"manual": True, "title": "Фокус и резкость картинки",
         "hint": "посмотри присланный снимок: нет мыла/засветки"},
        {"manual": True, "title": "Зона обзора соответствует ТЗ",
         "hint": "сравни с эталоном: /baseline, кнопка «⚖️ С эталоном»"},
    ]},
}


def checklists() -> dict:
    """181: встроенные сценарии + пользовательские из checklists.json."""
    cls = {k: dict(v) for k, v in _BUILTIN.items()}
    cls.update(store.jload(st.cget("checklists_path"), {}))
    return cls


# ---------- автопроверки ----------
def _chk_ping(ip):
    ok = net.ping(ip) is not None
    return ok, "отвечает" if ok else "НЕ отвечает"


def _chk_arp(ip):
    mac = net.arp_table().get(ip)
    return bool(mac), f"MAC {mac}" if mac else "в ARP нет (хост не в сети?)"


def _chk_ports(ip):
    p = net.open_ports(ip, ports=(80, 554, 8080))
    return bool(p), ("открыты: " + ",".join(map(str, p))) if p else "все закрыты"


def _chk_switch(ip):
    rec = inv.get(ip) or {}
    if rec.get("switch") or rec.get("port"):
        return True, (f"{rec.get('switch') or '?'} ({rec.get('sw_ip') or '?'}) "
                      f"порт {rec.get('port') or '?'}")
    return False, "в инвентаре нет свитча/порта"


def _chk_onvif(ip):
    from onvif_snap import device_info
    info = device_info(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
    if info.get("model"):
        return True, f"{info.get('manufacturer')} {info.get('model')} fw {info.get('firmware')}"
    return False, str(info.get("error"))[:80]


def _chk_rtsp(ip):
    try:
        from onvif_snap import rtsp_uri
        uri = rtsp_uri(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
        if isinstance(uri, tuple):
            uri = uri[0]
        return bool(uri), (str(uri)[:70] if uri else "URI не получен")
    except Exception as e:
        return False, str(e)[:80]


def _chk_snapshot(ip):
    from onvif_snap import get_snapshot
    data, msg = get_snapshot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
    return bool(data), (f"{len(data) // 1024} КБ" if data else str(msg)[:80])


_AUTO = {"ping": _chk_ping, "arp": _chk_arp, "ports": _chk_ports,
         "switch": _chk_switch, "onvif": _chk_onvif, "snapshot": _chk_snapshot,
         "rtsp": _chk_rtsp}

# ---------- мастер чек-листа (179/180/181) ----------
_wiz: dict = {}     # chat -> {"cl","ip","i","results":[(title, ok, note)]}
_lock = threading.Lock()


def _wiz_step(chat):
    with _lock:
        w = _wiz.get(chat)
    if not w:
        return
    cls = checklists()[w["cl"]]
    steps = cls["steps"]
    if w["i"] >= len(steps):
        _wiz_finish(chat)
        return
    s = steps[w["i"]]
    n, total = w["i"] + 1, len(steps)
    head = f"📋 <b>{esc(cls.get('title') or w['cl'])}</b> · шаг {n}/{total}"
    if s.get("auto") in _AUTO:
        chat_action(chat)
        try:
            ok, note = _AUTO[s["auto"]](w["ip"])
        except Exception as e:
            ok, note = False, f"проверка упала: {e}"
        w["results"].append((s.get("title") or s["auto"], ok, note))
        kb = {"inline_keyboard": [[
            {"text": "▶️ Дальше", "callback_data": "ck:next"},
            {"text": "⏹ Стоп", "callback_data": "ck:stop"}]]}
        send(chat, f"{head}\n{'🟢' if ok else '🔴'} {esc(s.get('title') or '')}: "
                   f"{esc(note)}", markup=kb)
    else:
        kb = {"inline_keyboard": [[
            {"text": "✅ Ок", "callback_data": "ck:ok"},
            {"text": "❌ Нет", "callback_data": "ck:no"},
            {"text": "⏹ Стоп", "callback_data": "ck:stop"}]]}
        send(chat, f"{head}\n👉 {esc(s.get('title') or '?')}"
                   + (f"\n💡 {esc(s['hint'])}" if s.get("hint") else ""),
             markup=kb)


def _wiz_finish(chat, stopped=False):
    with _lock:
        w = _wiz.pop(chat, None)
    if not w:
        return
    bad = [(t, note) for t, ok, note in w["results"] if not ok]
    lines = [f"📋 <b>Итог «{esc(checklists()[w['cl']].get('title') or w['cl'])}»</b>"
             f" — {esc(inv.label(w['ip']) or w['ip'])}"
             + (" (прервано)" if stopped else "")]
    for t, ok, note in w["results"]:
        lines.append(f"{'🟢' if ok else '🔴'} {esc(t)}: {esc(note)}")
    kb = None
    if w["cl"] == "accept":
        lines.append("\n" + ("✅ <b>Камера принята.</b>" if not bad else
                             f"⚠️ <b>Принята с замечаниями</b> ({len(bad)})."))
    elif bad:
        lines.append(f"\nВердикт: {len(bad)} проблемных пунктов — похоже на "
                     + ("линк/PoE" if any("порт" in t.lower() or "ping" in t.lower()
                                          for t, _n in bad) else "камеру")
                     + ". Черновик заявки — кнопкой ниже.")
        kb = {"inline_keyboard": [[
            {"text": "🎫 Тикет", "callback_data": f"tck:{w['ip']}"}]]}
    else:
        lines.append("\nВердикт: все проверки прошли — камера в порядке.")
    send(chat, "\n".join(lines), markup=kb)
    try:
        import bot_dossier
        bot_dossier.add_event(w["ip"], "check",
                              f"чек-лист {w['cl']}: {len(w['results']) - len(bad)}"
                              f"/{len(w['results'])} ок")
    except Exception:
        pass


def _start_wiz(chat, cl, arg, usage, reply_to=None):
    a = (arg or "").strip()
    ip = a if net.valid_ip(a) else inv.resolve_ip(a)
    if not ip:
        import bot_field
        ip = bot_field.ctx_cam()
    if not ip:
        send(chat, usage, reply_to=reply_to)
        return
    if cl not in checklists():
        send(chat, f"Сценария «{esc(cl)}» нет. Есть: "
                   + ", ".join(checklists()), reply_to=reply_to)
        return
    with _lock:
        _wiz[chat] = {"cl": cl, "ip": ip, "i": 0, "results": []}
    _wiz_step(chat)


def cmd_lost(chat, arg="", reply_to=None):
    _start_wiz(chat, "lost", arg,
               "Мастер «камера пропала»: <code>/lost 10.20.50.51</code>",
               reply_to)


def cmd_accept(chat, arg="", reply_to=None):
    _start_wiz(chat, "accept", arg,
               "Приёмка после монтажа: <code>/accept 10.20.50.51</code>",
               reply_to)


def cmd_checklist(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    if len(parts) < 2:
        send(chat, "Свой сценарий: <code>/checklist &lt;имя&gt; &lt;ip&gt;</code>\n"
                   "Есть: " + ", ".join(f"<code>{esc(k)}</code>"
                                        for k in checklists())
                   + "\nДобавить свой — в checklists.json.", reply_to=reply_to)
        return
    _start_wiz(chat, parts[0], parts[1], "…", reply_to)


def cb_ck(chat, cq, action):
    answer_cq(cq.get("id"))
    with _lock:
        w = _wiz.get(chat)
        if not w:
            return
        if action == "stop":
            pass
        elif action in ("ok", "no"):
            s = checklists()[w["cl"]]["steps"][w["i"]]
            w["results"].append((s.get("title") or "?", action == "ok",
                                 "подтверждено" if action == "ok" else "проблема"))
            w["i"] += 1
        else:  # next
            w["i"] += 1
    if action == "stop":
        _wiz_finish(chat, stopped=True)
    else:
        _wiz_step(chat)


# ---------- 182/183: /patrol ----------
_pt: dict = {}      # chat -> {"zone","recs","i","results",{},"started","wait"}


def cmd_patrol(chat, arg="", reply_to=None):
    import bot_zones as bz
    a = (arg or "").strip()
    if a.lower() in ("stop", "стоп", "end"):
        _patrol_finish(chat, stopped=True)
        return
    what, recs = bz.cams_by_arg(a)
    recs = [r for r in recs if r.get("ip")]
    if not recs:
        send(chat, "Обход зоны: <code>/patrol атриум</code> (или корпус 7C) · "
                   "<code>/patrol stop</code>", reply_to=reply_to)
        return
    recs = sorted(recs, key=lambda r: ((bz.cam_meta(r).get("floor") or 999),
                                       bz.cam_meta(r).get("num") or 0))
    with _lock:
        _pt[chat] = {"zone": what, "recs": recs, "i": 0, "results": {},
                     "started": time.time(), "wait": None}
    send(chat, f"🚶 <b>Обход: {esc(what)}</b> — {len(recs)} камер. Поехали!",
         reply_to=reply_to)
    _patrol_step(chat)


def _patrol_step(chat):
    with _lock:
        p = _pt.get(chat)
    if not p:
        return
    if p["i"] >= len(p["recs"]):
        _patrol_finish(chat)
        return
    rec = p["recs"][p["i"]]
    ip = rec["ip"]
    n, total = p["i"] + 1, len(p["recs"])
    kb = {"inline_keyboard": [
        [{"text": "✅ ОК", "callback_data": "pt:ok"},
         {"text": "⚠️ Замечание", "callback_data": "pt:warn"},
         {"text": "❌ Неисправна", "callback_data": "pt:bad"}],
        [{"text": "⏭ Пропустить", "callback_data": "pt:skip"},
         {"text": "⏹ Завершить", "callback_data": "pt:stop"}]]}
    cap = (f"🚶 {n}/{total} · {rec.get('name') or '?'} · {ip}\n"
           f"{rec.get('location') or ''}")
    if st.cget("patrol_snap"):
        chat_action(chat, "upload_photo")
        try:
            from onvif_snap import get_snapshot
            data, _m = get_snapshot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
            if data:
                send_photo(chat, data, caption=cap, markup=kb)
                return
        except Exception:
            log_exc(f"patrol: снимок {ip}")
    send(chat, f"🚶 <b>{n}/{total}</b> · {esc(rec.get('name') or '?')} · "
               f"<code>{ip}</code>\n📍 {esc(rec.get('location') or '—')}\n"
               f"(снимок не получился)", markup=kb)


def cb_pt(chat, cq, action):
    with _lock:
        p = _pt.get(chat)
    if not p:
        answer_cq(cq.get("id"), "Обход не идёт")
        return
    rec = p["recs"][min(p["i"], len(p["recs"]) - 1)]
    ip = rec["ip"]
    if action == "stop":
        answer_cq(cq.get("id"), "⏹ Завершаю")
        _patrol_finish(chat, stopped=True)
        return
    if action in ("ok", "skip"):
        answer_cq(cq.get("id"), "✅" if action == "ok" else "⏭")
        with _lock:
            p["results"][ip] = {"st": "ок" if action == "ok" else "пропущена",
                                "note": ""}
            p["i"] += 1
        _patrol_step(chat)
        return
    if action in ("warn", "bad"):
        answer_cq(cq.get("id"), "Жду текст замечания")
        with _lock:
            p["results"][ip] = {"st": "замечание" if action == "warn"
                                else "неисправна", "note": ""}
            p["wait"] = ip
        if action == "bad":
            import bot_issues as iss
            iss.open_issue(ip)
        send(chat, f"✍️ Замечание по <code>{ip}</code> одной строкой "
                   f"(или «-» чтобы пропустить):")


def capture_note(chat, text) -> bool:
    """Текст-замечание к текущему шагу обхода (вызывается из bot_field)."""
    with _lock:
        p = _pt.get(chat)
        if not p or not p.get("wait"):
            return False
        ip = p["wait"]
        p["results"][ip]["note"] = "" if text.strip() == "-" else text.strip()
        p["wait"] = None
        p["i"] += 1
    if text.strip() != "-":
        try:
            import bot_dossier
            bot_dossier.add_event(ip, "note", f"обход: {text.strip()}")
        except Exception:
            pass
    _patrol_step(chat)
    return True


def _patrol_xlsx(p) -> bytes:
    """183: акт обхода — xlsx в память."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Акт обхода"
    ws.append([f"Акт обхода: {p['zone']}"])
    ws.append([f"Дата: {datetime.datetime.now():%d.%m.%Y %H:%M} · "
               f"обходчик: оператор (Telegram-бот)"])
    ws.append([])
    ws.append(["№", "Камера", "IP", "Расположение", "Статус", "Замечание"])
    for i, rec in enumerate(p["recs"], 1):
        r = p["results"].get(rec["ip"]) or {"st": "не осмотрена", "note": ""}
        ws.append([i, rec.get("name"), rec["ip"], rec.get("location"),
                   r["st"], r["note"]])
    ok_n = sum(1 for r in p["results"].values() if r["st"] == "ок")
    bad = [r for r in p["results"].values() if r["st"] == "неисправна"]
    ws.append([])
    ws.append([f"Итого: осмотрено {len(p['results'])} из {len(p['recs'])}, "
               f"ок {ok_n}, замечаний "
               f"{sum(1 for r in p['results'].values() if r['st'] == 'замечание')}, "
               f"неисправно {len(bad)}"])
    for col, wd in (("A", 5), ("B", 16), ("C", 15), ("D", 40), ("E", 12), ("F", 40)):
        ws.column_dimensions[col].width = wd
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _patrol_finish(chat, stopped=False):
    with _lock:
        p = _pt.pop(chat, None)
    if not p:
        if stopped:
            send(chat, "Обход и так не шёл.")
        return
    dur = int(time.time() - p["started"])
    ok_n = sum(1 for r in p["results"].values() if r["st"] == "ок")
    warn = [(ip, r) for ip, r in p["results"].items() if r["st"] == "замечание"]
    bad = [(ip, r) for ip, r in p["results"].items() if r["st"] == "неисправна"]
    lines = [f"🏁 <b>Обход завершён</b>{' (прерван)' if stopped else ''}: "
             f"{esc(str(p['zone']))}",
             f"Осмотрено {len(p['results'])}/{len(p['recs'])} за "
             f"{dur // 60} мин · ✅ {ok_n} · ⚠️ {len(warn)} · ❌ {len(bad)}"]
    for ip, r in (warn + bad)[:15]:
        lines.append(f"{'⚠️' if r['st'] == 'замечание' else '❌'} "
                     f"{esc(inv.label(ip) or ip)}"
                     + (f": {esc(r['note'])}" if r.get("note") else ""))
    send(chat, "\n".join(lines))
    try:
        data = _patrol_xlsx(p)
        send_document(chat, data,
                      f"patrol_{datetime.datetime.now():%Y%m%d_%H%M}.xlsx",
                      caption="📄 Акт обхода — можно переслать начальству")
    except Exception:
        log_exc("patrol: xlsx")


HANDLERS = {
    "/lost": cmd_lost, "/accept": cmd_accept, "/checklist": cmd_checklist,
    "/patrol": cmd_patrol,
}
ALIASES = {
    "/пропала": "/lost", "/приемка": "/accept", "/приёмка": "/accept",
    "/обход": "/patrol",
}
CALLBACKS = {"ck": cb_ck, "pt": cb_pt}
