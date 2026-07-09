# -*- coding: utf-8 -*-
"""Волна J — I32 /snapall + U16 /clip.
I32: /snapall <подсеть|зона> — очередь снимков малыми порциями (snapall_workers
параллельно, деф. 5) → Drive через bot_gdrive2.upload_snapshot2 (папка дня,
md5-дедуп) → итог «снято X/Y» + список ошибок. Single-flight.
U16: /clip <ip> [сек] — проверка `ffmpeg -version`; без ffmpeg — честный отказ
с инструкцией; с ffmpeg — RTSP → mp4 (-c copy, 5-15с, жёсткий таймаут) →
sendVideo. Пароли камер не меняются, все операции с камерами read-only."""
import os
import re
import time
import tempfile
import threading
import subprocess
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

import bot_state as st
import bot_net as net
import bot_inventory as inv
from bot_tg import send, edit_message, chat_action, send_video
from bot_util import log, log_exc, esc, human_err

_CF = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SNAP_LOCK = threading.Lock()


# ---------- I32: /snapall ----------
def _targets(arg):
    """(ips, подпись) либо (None, текст ошибки)."""
    a = (arg or "").strip().rstrip(".")
    if not a:
        return None, ("Массовые снимки: <code>/snapall 10.20.50</code> "
                      "(подсеть) или <code>/snapall &lt;зона&gt;</code>.\n"
                      "Снимки уходят в Drive (папка дня, дубликаты по md5 "
                      "не заливаются).")
    if net.valid_prefix(a):
        ips = sorted((c["ip"] for c in inv.cams()
                      if c.get("ip") and c["ip"].startswith(a + ".")),
                     key=lambda x: int(x.split(".")[-1]))
        if not ips:
            return None, f"В инвентаре нет камер подсети {esc(a)}.x"
        return ips, f"{a}.x ({len(ips)} камер инвентаря)"
    try:
        import bot_zones
        z = bot_zones.resolve_zone(a)
        if z:
            ips = bot_zones.zone_ips(z)
            if not ips:
                return None, f"В зоне «{esc(z)}» нет камер с IP."
            return ips, f"зона «{z}» ({len(ips)} камер)"
    except Exception:
        log_exc("snapall: zones")
    return None, (f"«{esc(a)}» — не подсеть и не зона. "
                  f"Зоны: /zone · подсеть: <code>/snapall 10.20.50</code>")


def _snap_one(ip):
    """(ip, ok, err|None): снимок + заливка в Drive (дедуп md5 внутри)."""
    from onvif_snap import get_snapshot
    try:
        data, msg = get_snapshot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
        if not data:
            return ip, False, str(msg)[:60]
        import bot_gdrive2
        bot_gdrive2.upload_snapshot2(ip, data)
        return ip, True, None
    except Exception as e:
        return ip, False, f"{type(e).__name__}"


def cmd_snapall(chat, arg="", reply_to=None):
    ips, label = _targets(arg)
    if ips is None:
        send(chat, label, reply_to=reply_to)
        return
    cap = int(st.cget("snapall_max"))
    note = ""
    if len(ips) > cap:
        note = f"\n⚠️ Больше потолка {cap} — снимаю первые {cap}."
        ips = ips[:cap]
    if not SNAP_LOCK.acquire(blocking=False):
        send(chat, "⏳ /snapall уже идёт — дождись завершения.",
             reply_to=reply_to)
        return
    try:
        chat_action(chat, "upload_photo")
        r = send(chat, f"📸 <b>/snapall {esc(label)}</b>{note}\n"
                       f"0/{len(ips)} … (порциями по "
                       f"{st.cget('snapall_workers')})",
                 reply_to=reply_to, silent=True)
        mid = ((r or {}).get("result") or {}).get("message_id")
        t0 = time.time()
        done, ok_n, errs = 0, 0, []
        with ThreadPoolExecutor(
                max_workers=int(st.cget("snapall_workers"))) as ex:
            for ip, ok, err in ex.map(_snap_one, ips):
                done += 1
                if ok:
                    ok_n += 1
                else:
                    errs.append((ip, err))
                if mid and done % 10 == 0 and done < len(ips):
                    edit_message(chat, mid,
                                 f"📸 <b>/snapall {esc(label)}</b>\n"
                                 f"{done}/{len(ips)} · снято {ok_n} · "
                                 f"ошибок {len(errs)}")
        dt = time.time() - t0
        lines = [f"📸 <b>/snapall {esc(label)}</b>: снято и залито в Drive "
                 f"<b>{ok_n}/{len(ips)}</b> за {dt:.0f}с (I32, дедуп md5)."]
        if errs:
            lines.append(f"❌ Ошибки ({len(errs)}):")
            for ip, err in errs[:15]:
                lines.append(f"• <code>{ip}</code> — {esc(err or '?')}")
            if len(errs) > 15:
                lines.append(f"… и ещё {len(errs) - 15}")
        txt = "\n".join(lines)
        if mid:
            edit_message(chat, mid, txt)
        else:
            send(chat, txt)
        log(f"snapall {label}: {ok_n}/{len(ips)} за {dt:.0f}s, "
            f"ошибок {len(errs)}")
    finally:
        SNAP_LOCK.release()


# ---------- U16: /clip ----------
def ffmpeg_version():
    """Строка версии ffmpeg или None (не установлен)."""
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                           text=True, timeout=10, creationflags=_CF)
        first = (r.stdout or "").splitlines()
        return first[0][:60] if r.returncode == 0 and first else None
    except (OSError, subprocess.SubprocessError):
        return None


def _rtsp_with_creds(uri, user, pwd):
    """rtsp://host/… -> rtsp://user:pwd@host/… (если кредов ещё нет)."""
    if "@" in uri.split("://", 1)[-1].split("/", 1)[0]:
        return uri
    return re.sub(r"^rtsp://", f"rtsp://{quote(user, safe='')}:"
                               f"{quote(pwd, safe='')}@", uri)


def record_clip(url, seconds, out_path):
    """ffmpeg RTSP -> mp4 (-c copy, без звука). (ok, err|None)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-rtsp_transport", "tcp",
           "-i", url, "-t", str(int(seconds)), "-c:v", "copy", "-an",
           "-movflags", "+faststart", out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=int(seconds) + 30, creationflags=_CF)
    except subprocess.TimeoutExpired:
        return False, "таймаут ffmpeg (камера не отдаёт поток?)"
    except OSError as e:
        return False, f"запуск ffmpeg: {type(e).__name__}"
    if r.returncode != 0 or not os.path.exists(out_path) \
            or os.path.getsize(out_path) < 1024:
        tail = (r.stderr or "").strip().splitlines()[-1:] or ["?"]
        return False, f"ffmpeg: {tail[0][:120]}"
    return True, None


def cmd_clip(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    a = parts[0] if parts else ""
    ip = a if net.valid_ip(a) else (inv.resolve_ip(a) if a else None)
    if not ip:
        send(chat, "Видеоклип с камеры: <code>/clip 10.20.50.51 [сек]</code> "
                   "(5-15с, нужен ffmpeg).", reply_to=reply_to)
        return
    try:
        sec = int(parts[1])
    except (IndexError, ValueError):
        sec = int(st.cget("clip_seconds"))
    sec = max(5, min(sec, int(st.cget("clip_max_s"))))
    ver = ffmpeg_version()
    if not ver:
        send(chat, "🎬 <b>ffmpeg не установлен</b> — /clip не может писать "
                   "RTSP.\nПоставь и перезапусти бота:\n"
                   "<code>winget install Gyan.FFmpeg</code>\n"
                   "(или choco install ffmpeg; проверка: "
                   "<code>ffmpeg -version</code>).", reply_to=reply_to)
        return
    chat_action(chat, "record_video")
    send(chat, f"🎬 Пишу {sec}с с <code>{ip}</code> … (RTSP, {esc(ver)})",
         silent=True, reply_to=reply_to)
    from onvif_snap import rtsp_uri
    uri, err = rtsp_uri(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
    if not uri:
        send(chat, human_err(f"RTSP-URL с <code>{ip}</code> не получен", err),
             reply_to=reply_to)
        return
    url = _rtsp_with_creds(uri, st.CAM_USER, st.CAM_PASS)
    out = os.path.join(tempfile.gettempdir(),
                       f"clip_{ip.replace('.', '_')}_{int(time.time())}.mp4")
    try:
        ok, err = record_clip(url, sec, out)
        if not ok:
            send(chat, human_err(f"Клип с <code>{ip}</code> не записался", err),
                 reply_to=reply_to)
            return
        with open(out, "rb") as f:
            data = f.read()
        lbl = inv.label(ip)
        send_video(chat, data, f"{ip}_{sec}s.mp4",
                   caption=f"🎬 {ip} · {sec}с · {len(data) // 1024} КБ"
                           + (f"\n📒 {lbl}" if lbl else ""))
        log(f"clip: {ip} {sec}s {len(data) // 1024}КБ")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


HANDLERS = {"/snapall": cmd_snapall, "/clip": cmd_clip}
ALIASES = {"/клип": "/clip", "/снятьвсе": "/snapall"}
CALLBACKS = {}
