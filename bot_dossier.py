# -*- coding: utf-8 -*-
"""Волна D — досье камеры: 191 /baseline (эталонный снимок обзора),
192 «⚖️ С эталоном» (эталон + текущий кадр рядом), 193 фото-досье монтажа
(фото с подписью-именем → cam_docs/<имя>/), 194 голосовые заметки (.ogg в
досье), 195 /timeline (единая лента событий камеры). Пароли НЕ меняются."""
import os
import re
import time
import datetime

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_store as store
from bot_tg import (send, send_photo, send_media_group, chat_action,
                    answer_cq, get_file)
from bot_util import log, log_exc, esc


def _key(ip: str) -> str:
    rec = inv.get(ip)
    if rec and rec.get("name"):
        k = re.sub(r"[^\w.\-]", "_", str(rec["name"]))
        if k:
            return k
    return ip.replace(".", "-")


def _dir(ip: str, make=False) -> str:
    d = os.path.join(st.cget("cam_docs_dir"), _key(ip))
    if make:
        os.makedirs(d, exist_ok=True)
    return d


def _meta_path(ip: str) -> str:
    return os.path.join(_dir(ip), "meta.json")


def add_event(ip: str, kind: str, text: str, file: str = None) -> None:
    """Строка в timeline камеры (195): kind = note|photo|voice|check|baseline."""
    _dir(ip, make=True)
    def _fn(d):
        d["events"] = (d.get("events") or [])[-300:] + [{
            "ts": int(time.time()), "kind": kind, "text": text,
            **({"file": file} if file else {})}]
        return d
    store.jupdate(_meta_path(ip), {"events": []}, _fn)


def events(ip: str) -> list:
    return store.jload(_meta_path(ip), {"events": []}).get("events") or []


def _resolve(arg):
    a = (arg or "").strip()
    if net.valid_ip(a):
        return a
    return inv.resolve_ip(a)


# ---------- 191: /baseline ----------
def _baseline_path(ip: str) -> str:
    return os.path.join(_dir(ip), "baseline.jpg")


def has_baseline(ip: str) -> bool:
    try:
        return os.path.exists(_baseline_path(ip))
    except Exception:
        return False


def cmd_baseline(chat, arg="", reply_to=None):
    ip = _resolve(arg)
    if not ip:
        send(chat, "Эталонный снимок обзора: <code>/baseline 10.20.50.51</code>",
             reply_to=reply_to)
        return
    chat_action(chat, "upload_photo")
    from onvif_snap import get_snapshot
    data, msg = get_snapshot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
    if not data:
        send(chat, f"❌ Снимок с <code>{ip}</code> не получился ({esc(msg)}) — "
                   f"эталон не сохранён.", reply_to=reply_to)
        return
    _dir(ip, make=True)
    with open(_baseline_path(ip), "wb") as f:
        f.write(data)
    add_event(ip, "baseline", "сохранён эталон обзора")
    log(f"baseline: {ip} сохранён ({len(data) // 1024} КБ)")
    send_photo(chat, data,
               caption=f"⚖️ Эталон обзора {inv.label(ip) or ip} сохранён "
                       f"({datetime.date.today():%d.%m.%Y}).",
               markup={"inline_keyboard": [[
                   {"text": "⚖️ Сравнить сейчас", "callback_data": f"bline:{ip}"}]]})


# ---------- 192: сравнение с эталоном ----------
def cb_bline(chat, cq, ip):
    if not net.valid_ip(ip) or not has_baseline(ip):
        answer_cq(cq.get("id"), "Эталона нет — /baseline")
        return
    answer_cq(cq.get("id"), "⚖️ Снимаю и сравниваю…")
    chat_action(chat, "upload_photo")
    try:
        with open(_baseline_path(ip), "rb") as f:
            base = f.read()
    except OSError:
        send(chat, "❌ Файл эталона не читается.")
        return
    bts = ""
    try:
        bts = time.strftime("%d.%m.%Y",
                            time.localtime(os.path.getmtime(_baseline_path(ip))))
    except OSError:
        pass
    from onvif_snap import get_snapshot
    data, msg = get_snapshot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
    if not data:
        send(chat, f"❌ Текущий кадр с <code>{ip}</code> не получился "
                   f"({esc(msg)}) — показываю только эталон.")
        send_photo(chat, base, caption=f"⚖️ эталон {ip} от {bts}")
        return
    send_media_group(chat, [(base, f"⚖️ ЭТАЛОН {ip} · {bts}"),
                            (data, f"СЕЙЧАС {ip} · "
                                   f"{datetime.datetime.now():%d.%m %H:%M}")])


# ---------- 193/194: фото и голос в досье ----------
def on_media(msg: dict) -> None:
    """Вызывается из camera_bot для сообщений с photo/voice (только владелец)."""
    chat = (msg.get("chat") or {}).get("id")
    if chat != st.cget("owner_chat_id"):
        return
    cap = (msg.get("caption") or "").strip()
    ip = _resolve(cap.split()[0]) if cap else None
    if not ip:
        try:
            import bot_field
            ip = bot_field.ctx_cam()
        except Exception:
            ip = None
    if not ip:
        send(chat, "К какой камере это? Подпиши фото именем/IP или задай "
                   "контекст: <code>/at AS-7C.01</code>")
        return
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if msg.get("photo"):
        fid = msg["photo"][-1].get("file_id")  # самое большое фото
        data = get_file(fid)
        if not data:
            send(chat, "❌ Не смог скачать фото из Telegram — повтори.")
            return
        fn = f"photo_{ts}.jpg"
        _dir(ip, make=True)
        with open(os.path.join(_dir(ip), fn), "wb") as f:
            f.write(data)
        note = cap[len(cap.split()[0]):].strip() if cap else ""
        add_event(ip, "photo", f"📷 фото монтажа" + (f": {note}" if note else ""),
                  file=fn)
        send(chat, f"📷 Фото сохранено в досье "
                   f"<code>cam_docs/{_key(ip)}/</code> ({inv.label(ip) or ip}).",
             silent=True)
        return
    if msg.get("voice"):
        data = get_file(msg["voice"].get("file_id"))
        if not data:
            send(chat, "❌ Не смог скачать голосовую — повтори.")
            return
        fn = f"voice_{ts}.ogg"
        _dir(ip, make=True)
        with open(os.path.join(_dir(ip), fn), "wb") as f:
            f.write(data)
        dur = msg["voice"].get("duration") or 0
        add_event(ip, "voice", f"🎤 голосовая заметка ({dur}с)", file=fn)
        send(chat, f"🎤 Заметка ({dur}с) сохранена в досье "
                   f"{inv.label(ip) or ip}.", silent=True)


# ---------- досье и 195: /timeline ----------
def cmd_dossier(chat, arg="", reply_to=None):
    ip = _resolve(arg)
    if not ip:
        send(chat, "Досье камеры: <code>/dossier 10.20.50.51</code>\n"
                   "Фото/голос с подписью-именем попадают сюда сами.",
             reply_to=reply_to)
        return
    d = _dir(ip)
    try:
        files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    except OSError:
        files = []
    files = [f for f in files if f != "meta.json"]
    if not files:
        send(chat, f"📁 Досье {esc(inv.label(ip) or ip)} пусто. Пришли фото "
                   f"шкафа/трассы с подписью «{esc((inv.get(ip) or {}).get('name') or ip)}».",
             reply_to=reply_to)
        return
    lines = [f"📁 <b>Досье {esc(inv.label(ip) or ip)}</b> — {len(files)} файлов "
             f"в <code>cam_docs/{esc(_key(ip))}/</code>:"]
    lines += [f"• {esc(f)}" for f in files[:30]]
    send(chat, "\n".join(lines), reply_to=reply_to)
    photos = [f for f in files if f.endswith(".jpg")][-10:]
    batch = []
    for f in photos:
        try:
            with open(os.path.join(d, f), "rb") as fh:
                batch.append((fh.read(), f))
        except OSError:
            continue
    if len(batch) == 1:
        send_photo(chat, batch[0][0], caption=batch[0][1])
    elif batch:
        send_media_group(chat, batch)


def timeline(ip: str, days: float = 90) -> list:
    """195: [(ts, текст)] — health + проблемы + lifecycle + досье."""
    out = []
    import bot_health as bh
    for e in bh.history_events(days):
        if e.get("ip") != ip:
            continue
        if e["ev"] == "down":
            out.append((e["ts"], "🔴 упала"))
        else:
            out.append((e["ts"], "🟢 ожила"
                        + (f" (лежала {bh._fmt_dur(e['dur'])})"
                           if e.get("dur") else "")))
    import bot_issues as iss
    for it in iss.data()["issues"]:
        if it["ip"] != ip:
            continue
        out.append((it["opened"], f"🗂 проблема #{it['id']} открыта"))
        if it.get("taken"):
            out.append((it["taken"], f"🔧 #{it['id']} взята в работу"))
        if it.get("fixed"):
            out.append((it["fixed"], f"✅ #{it['id']} починена"))
    try:
        from bot_lifecycle import lc_events, _ST_RU
        for e in lc_events(ip):
            out.append((e["ts"], f"♻️ статус: {_ST_RU.get(e['status'], e['status'])}"))
    except Exception:
        pass
    for e in events(ip):
        out.append((e["ts"], e.get("text") or e.get("kind") or "?"))
    try:
        acts = store.jload(st.cget("acts_path"), {"acts": []}).get("acts") or []
        for a in acts:
            if a.get("ip") == ip:
                out.append((a["ts"], f"📄 акт №{a['id']}: {a.get('verdict', '')[:60]}"))
    except Exception:
        pass
    return sorted(out, key=lambda x: x[0])


def cmd_timeline(chat, arg="", reply_to=None):
    ip = _resolve(arg)
    if not ip:
        send(chat, "Лента событий: <code>/timeline 10.20.50.51</code>",
             reply_to=reply_to)
        return
    tl = timeline(ip)
    if not tl:
        send(chat, f"🧾 По {esc(inv.label(ip) or ip)} событий не накопилось.",
             reply_to=reply_to)
        return
    lines = [f"🧾 <b>Биография {esc(inv.label(ip) or ip)}</b> "
             f"(события за 90 дн., всего {len(tl)}):"]
    for ts, txt in tl[-40:]:
        lines.append(time.strftime("%d.%m %H:%M", time.localtime(ts))
                     + f" — {txt if txt.startswith(('🔴', '🟢', '🗂', '🔧', '✅', '♻️', '📄')) else esc(txt)}")
    from bot_tg import send_chunks
    send_chunks(chat, lines)


HANDLERS = {
    "/baseline": cmd_baseline, "/timeline": cmd_timeline,
    "/dossier": cmd_dossier,
}
ALIASES = {
    "/эталон": "/baseline", "/лента": "/timeline", "/досье": "/dossier",
}
CALLBACKS = {"bline": cb_bline}
