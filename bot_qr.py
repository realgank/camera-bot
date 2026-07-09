# -*- coding: utf-8 -*-
"""Волна D — QR и наклейки: 172 /qr (PNG через segno, deep-link
t.me/<бот>?start=cam_<id>), 173 /qrsheet (QR всей зоны альбомами — без единого
листа: нет PIL для компоновки), 174 /label (текст наклейки для термопринтера),
175 deep-link /start cam_<id> → карточка камеры. Пароли камер НЕ меняются."""
import io

import bot_state as st
import bot_net as net
import bot_inventory as inv
from bot_tg import send, send_photo, send_media_group, send_document, chat_action
from bot_util import log_exc, esc


def _cam_id(rec) -> str:
    """id для deep-link: инвентарный № либо IP с дефисами."""
    if rec.get("n") not in (None, ""):
        return str(rec["n"])
    return str(rec.get("ip") or "").replace(".", "-")


def deep_link(rec) -> str:
    return f"https://t.me/{st.cget('bot_username')}?start=cam_{_cam_id(rec)}"


def qr_png(text: str, scale: int = 8) -> bytes:
    import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=scale, border=2)
    return buf.getvalue()


def _rec_by_arg(arg):
    a = (arg or "").strip()
    if net.valid_ip(a):
        return inv.get(a)
    res = inv.search(a)
    return res[0] if len(res) == 1 else None


def rec_by_cam_id(cam_id: str):
    """175: cam_<id> из QR → запись инвентаря (№ либо IP-с-дефисами)."""
    cid = (cam_id or "").strip()
    ip = cid.replace("-", ".")
    if net.valid_ip(ip):
        return inv.get(ip)
    for rec in inv.cams():
        if str(rec.get("n")) == cid:
            return rec
    return None


# ---------- 172: /qr ----------
def cmd_qr(chat, arg="", reply_to=None):
    rec = _rec_by_arg(arg)
    if not rec:
        send(chat, "QR камеры: <code>/qr 10.20.50.51</code> или "
                   "<code>/qr AS-7C.01</code>", reply_to=reply_to)
        return
    chat_action(chat, "upload_photo")
    try:
        data = qr_png(deep_link(rec))
    except Exception as e:
        log_exc("qr")
        send(chat, f"❌ QR не сгенерировался: {esc(e)}", reply_to=reply_to)
        return
    name = rec.get("name") or rec.get("ip") or "?"
    send_photo(chat, data,
               caption=f"🔳 {name} · {rec.get('ip') or '—'}\n"
                       f"Скан телефоном → карточка в боте.")
    send_document(chat, data, f"qr_{inv.norm_name(name) or 'cam'}.png",
                  caption="📎 PNG для печати (без сжатия)")


# ---------- 173: /qrsheet ----------
def cmd_qrsheet(chat, arg="", reply_to=None):
    import bot_zones as bz
    what, recs = bz.cams_by_arg(arg)
    if not recs:
        send(chat, "Лист QR по зоне: <code>/qrsheet атриум</code> "
                   "(или корпус 7C)", reply_to=reply_to)
        return
    recs = recs[:30]
    send(chat, f"🔳 Генерирую {len(recs)} QR ({esc(what)}) — альбомами по 10 "
               f"(единый лист не собрать без PIL, печатай сеткой из галереи)…",
         silent=True, reply_to=reply_to)
    chat_action(chat, "upload_photo")
    batch = []
    for rec in recs:
        try:
            png = qr_png(deep_link(rec), scale=6)
        except Exception:
            log_exc(f"qrsheet: {rec.get('name')}")
            continue
        batch.append((png, f"{rec.get('name') or '?'} · {rec.get('ip') or '—'}"))
        if len(batch) == 10:
            send_media_group(chat, batch)
            batch = []
    if len(batch) == 1:
        send_photo(chat, batch[0][0], caption=batch[0][1])
    elif batch:
        send_media_group(chat, batch)


# ---------- 174: /label ----------
def cmd_label(chat, arg="", reply_to=None):
    rec = _rec_by_arg(arg)
    if not rec:
        send(chat, "Наклейка: <code>/label 10.20.50.51</code>", reply_to=reply_to)
        return
    w = 32  # ширина строки термопринтера
    def row(k, v):
        v = str(v or "—")[:w - len(k) - 1]
        return f"{k} {v}"
    lines = [row("КАМ:", rec.get("name")),
             row("IP: ", rec.get("ip")),
             row("ИНВ:", rec.get("n")),
             row("SW: ", f"{rec.get('switch') or '—'} п.{rec.get('port') or '—'}"),
             row("MAC:", rec.get("mac"))]
    send(chat, "🏷 Наклейка (моноширина, копируй в софт принтера):\n"
               f"<code>{esc(chr(10).join(lines))}</code>", reply_to=reply_to)


# ---------- 175: deep-link (вызывается из bot_handlers.cmd_start) ----------
def start_cam(chat, cam_id: str) -> bool:
    rec = rec_by_cam_id(cam_id)
    if not rec:
        send(chat, f"По QR cam_{esc(cam_id)} камера в инвентаре не найдена.")
        return True
    ip = rec.get("ip")
    kb = None
    if ip:
        kb = {"inline_keyboard": [
            [{"text": "📸 Снимок", "callback_data": f"shot:{ip}"},
             {"text": "🩺 Диаг", "callback_data": f"diag:{ip}"}],
            [{"text": "🎫 Тикет", "callback_data": f"tck:{ip}"}]]}
    send(chat, "🔳 По QR:\n" + inv.card_text(rec), markup=kb)
    return True


HANDLERS = {"/qr": cmd_qr, "/qrsheet": cmd_qrsheet, "/label": cmd_label}
ALIASES = {"/наклейка": "/label"}
CALLBACKS = {}
