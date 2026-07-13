# -*- coding: utf-8 -*-
"""Волна D — полевой режим: 176 /mode field (клавиатура «одной рукой со
стремянки», ответы в 1-2 строки), 177 контекст «я у камеры» (/at AS-7C.01 —
дальше 📸/🩺/заметки без ввода IP, TTL 30 мин), 178 контекст зоны
(/at 7C — короткие номера «01», «14» резолвятся внутри зоны).
Свободный текст диспетчеризуется через try_text() из camera_bot."""
import time
import threading

import bot_state as st
import bot_net as net
import bot_inventory as inv
from bot_tg import send
from bot_util import log, esc

_ctx = {"mode": "normal", "cam": None, "cam_ts": 0.0, "zone": None}
_lock = threading.Lock()

FIELD_KB = {
    "keyboard": [
        ["📸 СНИМОК", "🩺 ПРОВЕРКА"],
        ["✅ РАБОТАЕТ", "❌ НЕ РАБОТАЕТ"],
        ["➡️ СЛЕДУЮЩАЯ", "🏠 ОБЫЧНЫЙ РЕЖИМ"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def ctx_cam():
    """177: текущая камера контекста (с TTL) или None."""
    with _lock:
        if not _ctx["cam"]:
            return None
        if time.time() - _ctx["cam_ts"] > float(st.cget("field_ctx_ttl_min")) * 60:
            _ctx["cam"] = None
            return None
        return _ctx["cam"]


def ctx_zone():
    with _lock:
        return _ctx["zone"]


def set_cam(ip):
    with _lock:
        _ctx["cam"] = ip
        _ctx["cam_ts"] = time.time()


def in_field():
    with _lock:
        return _ctx["mode"] == "field"


# ---------- 176: /mode ----------
def cmd_mode(chat, arg="", reply_to=None):
    a = (arg or "").strip().lower()
    if a in ("field", "поле", "монтажник"):
        with _lock:
            _ctx["mode"] = "field"
        send(chat, "🧗 <b>Режим монтажника</b>. Скажи, у какой ты камеры: "
                   "<code>/at AS-7C.01</code> (или зона: <code>/at 7C</code>) — "
                   "дальше только большие кнопки.", markup=FIELD_KB,
             reply_to=reply_to)
        return
    if a in ("normal", "обычный", "офис"):
        import bot_handlers as h
        with _lock:
            _ctx["mode"] = "normal"
            _ctx["cam"] = None
            _ctx["zone"] = None
        send(chat, "🏠 Обычный режим.", markup=h.MAIN_KB, reply_to=reply_to)
        return
    send(chat, "Режим: <code>/mode field</code> — монтажник · "
               "<code>/mode normal</code> — обычный", reply_to=reply_to)


# ---------- 177/178: /at ----------
def cmd_at(chat, arg="", reply_to=None):
    import bot_zones as bz
    a = (arg or "").strip()
    if not a:
        cam, zone = ctx_cam(), ctx_zone()
        send(chat, "📍 Контекст: камера "
                   + (f"<code>{cam}</code>" if cam else "—")
                   + " · зона " + (esc(zone) if zone else "—")
                   + "\n<code>/at AS-7C.01</code> — я у камеры · "
                     "<code>/at 7C</code> — обход корпуса/зоны · "
                     "<code>/at off</code> — сброс", reply_to=reply_to)
        return
    if a.lower() in ("off", "сброс", "-"):
        with _lock:
            _ctx["cam"] = None
            _ctx["zone"] = None
        send(chat, "📍 Контекст сброшен.", reply_to=reply_to)
        return
    what, recs = bz.cams_by_arg(a)
    if what and len(recs) > 1:  # зона или корпус
        with _lock:
            _ctx["zone"] = a
            _ctx["cam"] = None
        send(chat, f"📍 Контекст: <b>{esc(what)}</b> ({len(recs)} камер).\n"
                   f"Теперь шли короткие номера — «01», «14» — они резолвятся "
                   f"внутри зоны.", reply_to=reply_to)
        return
    ip = a if net.valid_ip(a) else inv.resolve_ip(a)
    if not ip and len(recs) == 1:
        ip = recs[0].get("ip")
    if not ip:
        send(chat, f"«{esc(a)}» — не нашёл ни зону, ни камеру.", reply_to=reply_to)
        return
    set_cam(ip)
    lbl = inv.label(ip)
    send(chat, f"📍 Ты у камеры <code>{ip}</code>"
               + (f" — {esc(lbl)}" if lbl else "")
               + f"\n📸/🩺/фото/голос идут к ней (таймаут "
                 f"{st.cget('field_ctx_ttl_min')} мин).",
         markup=FIELD_KB if in_field() else None, reply_to=reply_to)


def _short_diag(chat, ip):
    """176: проверка в 1-2 строки для полевого режима."""
    ports = net.open_ports(ip, ports=(80, 554))
    lbl = inv.label(ip)
    if ports:
        send(chat, f"🟢 <code>{ip}</code>"
                   + (f" {esc(lbl)}" if lbl else "")
                   + f" — отвечает, порты {','.join(map(str, ports))}")
    else:
        alive = net.ping(ip) is not None
        send(chat, f"🔴 <code>{ip}</code> — "
                   + ("ping есть, порты закрыты" if alive else "не отвечает"))


def _need_cam(chat) -> str:
    ip = ctx_cam()
    if not ip:
        send(chat, "Сначала скажи, где ты: <code>/at AS-7C.01</code>")
    return ip


def _mark(chat, ip, ok: bool):
    """✅/❌ с большой кнопки: отметка в досье + журнал проблем."""
    try:
        import bot_dossier
        bot_dossier.add_event(ip, "note",
                              "✅ работает (осмотр)" if ok else
                              "❌ не работает (осмотр)")
    except Exception:
        pass
    if ok:
        send(chat, f"✅ <code>{ip}</code> — отмечено «работает».")
    else:
        import bot_issues as iss
        iss.open_issue(ip)
        send(chat, f"❌ <code>{ip}</code> — проблема заведена в журнал.",
             markup={"inline_keyboard": [[
                 {"text": "🎫 Тикет", "callback_data": f"tck:{ip}"},
                 {"text": "🔧 Взял в работу", "callback_data": f"iw:{ip}"}]]})


def try_text(chat, text, reply_to=None) -> bool:
    """Хук свободного текста из camera_bot (после кнопок/команд/IP).
    True — обработано."""
    t = (text or "").strip()
    try:  # ПНР: ждём название новой камеры для /provision
        import bot_provision
        if bot_provision.capture_name(chat, t):
            return True
    except Exception:
        pass
    try:  # заметка к шагу обхода/чек-листа (182)
        import bot_check
        if bot_check.capture_note(chat, t):
            return True
    except Exception:
        pass
    if t == "📸 СНИМОК":
        ip = _need_cam(chat)
        if ip:
            import bot_handlers as h
            h.run_action(chat, "shot", ip)
        return True
    if t == "🩺 ПРОВЕРКА":
        ip = _need_cam(chat)
        if ip:
            _short_diag(chat, ip)
        return True
    if t == "✅ РАБОТАЕТ":
        ip = _need_cam(chat)
        if ip:
            _mark(chat, ip, True)
        return True
    if t == "❌ НЕ РАБОТАЕТ":
        ip = _need_cam(chat)
        if ip:
            _mark(chat, ip, False)
        return True
    if t == "➡️ СЛЕДУЮЩАЯ":
        with _lock:
            _ctx["cam"] = None
        send(chat, "📍 Ок, у какой камеры теперь? Пришли номер (в зоне), "
                   "имя или IP.")
        return True
    if t == "🏠 ОБЫЧНЫЙ РЕЖИМ":
        cmd_mode(chat, "normal")
        return True
    # 178: короткий номер в контексте зоны
    zone = ctx_zone()
    if zone and t.isdigit() and len(t) <= 3:
        import bot_zones as bz
        import bot_handlers as h
        num = int(t)
        _w, recs = bz.cams_by_arg(zone)
        hits = [r for r in recs
                if (bz.parse_cam_name(r.get("name")) or {}).get("num") == num]
        if len(hits) == 1 and hits[0].get("ip"):
            ip = hits[0]["ip"]
            set_cam(ip)
            log(f"field: «{t}» в зоне {zone} -> {ip}")
            send(chat, inv.card_text(hits[0]), markup=h.actions_kb(ip))
            return True
        if len(hits) > 1:
            send(chat, f"Номер {t} в «{esc(zone)}» неоднозначен: "
                       + ", ".join(str(r.get("name")) for r in hits[:6]))
            return True
        send(chat, f"Номера {t} в «{esc(zone)}» нет.")
        return True
    # в полевом режиме пробуем текст как имя камеры
    if in_field():
        ip = inv.resolve_ip(t)
        if ip:
            set_cam(ip)
            send(chat, f"📍 Ок, ты у <code>{ip}</code> "
                       f"({esc(inv.label(ip) or '')}).")
            return True
    return False


HANDLERS = {"/mode": cmd_mode, "/at": cmd_at}
ALIASES = {"/режим": "/mode", "/тут": "/at", "/я": "/at"}
CALLBACKS = {}
