# -*- coding: utf-8 -*-
"""Волна E — форензика процесса (bot_debug):
204 дамп состояния по файлу-флагу dump_now.flag · 205 faulthandler в
crash_native.log · 206 sys.excepthook + threading.excepthook · 208 /debug —
zip-архив диагностики · 209 /mem — tracemalloc · 237 /trace — tracert до
камеры · 242 снапшот окружения при старте · 245 крэш-репорты + /crashes ·
248 sha256 кода при старте · 249 /env — сетевая самодиагностика.
Хуки НЕ трогают SystemExit/KeyboardInterrupt — graceful shutdown и watchdog
работают как раньше. Пароли камер бот НИКОГДА не меняет."""
import os
import io
import sys
import json
import time
import socket
import hashlib
import logging
import zipfile
import threading
import traceback
import subprocess
import faulthandler

import bot_state as st
import bot_net as net
import bot_obs as obs
import bot_chan as chan
from bot_tg import send, send_document, chat_action
from bot_util import log, log_exc, esc, BOT_VERSION, LOG_PATH

BASE = st.BASE
DUMP_FLAG = os.path.join(BASE, "dump_now.flag")
CREATE_NO_WINDOW = 0x08000000

_installed = [False]
_fh_file = [None]          # файл faulthandler держим открытым весь срок жизни
_orig_hook = [None]
_orig_thread_hook = [None]
_mem_snap = [None]         # 209: прошлый снимок tracemalloc


# ---------- 245: крэш-репорты ----------
def crash_report(ctx: dict, exc: BaseException = None) -> str:
    """Сохраняет crash_*.json: контекст, traceback, хвост ring-buffer."""
    try:
        d = st.cget("crash_dir")
        os.makedirs(d, exist_ok=True)
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc,
                                                    exc.__traceback__))
        else:
            tb = traceback.format_exc()
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "version": BOT_VERSION,
               "ctx": ctx, "traceback": tb, "ring_tail": list(obs.RING)[-60:]}
        fn = os.path.join(d, f"crash_{time.strftime('%Y%m%d_%H%M%S')}_"
                             f"{int(time.time() * 1000) % 1000:03d}.json")
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1, default=str)
        _prune_crashes(d)
        return fn
    except Exception:
        return ""


def _prune_crashes(d: str) -> None:
    keep = int(st.cget("crash_keep"))
    try:
        files = sorted(fn for fn in os.listdir(d) if fn.startswith("crash_"))
        for fn in files[:-keep]:
            os.remove(os.path.join(d, fn))
    except OSError:
        pass


def cmd_crashes(chat, arg="", reply_to=None):
    """245: список последних крэш-репортов."""
    d = st.cget("crash_dir")
    try:
        files = sorted((fn for fn in os.listdir(d) if fn.startswith("crash_")),
                       reverse=True)[:10]
    except OSError:
        files = []
    if not files:
        send(chat, "✅ Крэш-репортов нет.", reply_to=reply_to)
        return
    lines = [f"💥 <b>Крэш-репорты</b> (последние {len(files)}), папка "
             f"<code>{esc(os.path.basename(d))}</code>:"]
    for fn in files:
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                rec = json.load(f)
            ctx = rec.get("ctx") or {}
            what = ctx.get("cmd") or ctx.get("where") or "update"
            last = (rec.get("traceback") or "").strip().splitlines()
            err = last[-1][:80] if last else "?"
            lines.append(f"• <code>{esc(fn)}</code> — {esc(str(what))}: {esc(err)}")
        except Exception:
            lines.append(f"• <code>{esc(fn)}</code>")
    lines.append("Полные файлы приедут в /debug-архиве.")
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- 205 + 206: faulthandler и excepthook'и ----------
def _excepthook(exc_type, exc, tb):
    if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
        (_orig_hook[0] or sys.__excepthook__)(exc_type, exc, tb)
        return
    try:
        log("НЕОБРАБОТАННОЕ исключение главного потока:\n"
            + "".join(traceback.format_exception(exc_type, exc, tb)),
            logging.CRITICAL)
        obs.ring_dump(reason="excepthook")
        crash_report({"where": "sys.excepthook"}, exc)
    except Exception:
        pass
    (_orig_hook[0] or sys.__excepthook__)(exc_type, exc, tb)


def _thread_excepthook(args):
    if args.exc_type is SystemExit:
        return
    try:
        log(f"НЕОБРАБОТАННОЕ исключение потока {args.thread.name if args.thread else '?'}:\n"
            + "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                 args.exc_traceback)),
            logging.ERROR)
        crash_report({"where": f"thread:{args.thread.name if args.thread else '?'}"},
                     args.exc_value)
    except Exception:
        pass
    if _orig_thread_hook[0]:
        try:
            _orig_thread_hook[0](args)
        except Exception:
            pass


def install_hooks() -> None:
    """205/206: включается один раз при старте из camera_bot.main()."""
    if _installed[0]:
        return
    _installed[0] = True
    try:
        f = open(os.path.join(BASE, "crash_native.log"), "a",
                 encoding="utf-8", errors="replace")
        f.write(f"\n=== start pid={os.getpid()} v{BOT_VERSION} "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
        _fh_file[0] = f
    except Exception:
        log_exc("debug: faulthandler не включился")
    _orig_hook[0] = sys.excepthook
    sys.excepthook = _excepthook
    _orig_thread_hook[0] = threading.excepthook
    threading.excepthook = _thread_excepthook
    log("debug: faulthandler + excepthook'и включены")


# ---------- 204: дамп состояния по файлу-флагу ----------
def write_state_dump(reason: str = "") -> str:
    fn = os.path.join(BASE, f"state_dump_{time.strftime('%Y%m%d_%H%M%S')}.txt")
    try:
        with open(fn, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"reason={reason} ts={time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"pid={os.getpid()} v{BOT_VERSION}\n\n=== СТЕКИ ПОТОКОВ ===\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
            f.write("\n\n=== STATS ===\n")
            f.write(json.dumps(st.stats_snapshot(), ensure_ascii=False,
                               indent=1, default=str))
            f.write("\n\n=== PENDING ===\n")
            f.write(json.dumps(getattr(st, "_pending", {}), ensure_ascii=False,
                               default=str))
            f.write("\n\n=== RECENT ===\n" + json.dumps(list(st.RECENT)))
            f.write(f"\n\n=== OFFSET ===\n{st.load_offset()}\n")
        log(f"debug: дамп состояния -> {os.path.basename(fn)}")
        return fn
    except Exception:
        log_exc("debug: дамп состояния не записался")
        return ""


def _tick() -> None:
    """204: появление dump_now.flag -> дамп (сигналы в Task Scheduler ненадёжны)."""
    if os.path.exists(DUMP_FLAG):
        try:
            os.remove(DUMP_FLAG)
        except OSError:
            pass
        write_state_dump("dump_now.flag")


# ---------- 242: снапшот окружения ----------
def env_snapshot() -> dict:
    import platform
    try:
        import requests
        rq = requests.__version__
    except Exception:
        rq = "?"
    return {"python": sys.version.split()[0], "requests": rq,
            "host": socket.gethostname(), "platform": platform.platform(),
            "tz": time.strftime("%z"),
            "console_enc": getattr(sys.stdout, "encoding", None) or "?",
            "exe": sys.executable, "cwd": os.getcwd(), "pid": os.getpid(),
            "local_ips": chan.local_ips(), "out_ip": chan.out_ip()}


def log_env_snapshot() -> dict:
    snap = env_snapshot()
    log("ENV " + json.dumps(snap, ensure_ascii=False, default=str))
    obs.jlog("env", **snap)
    return snap


# ---------- 248: контроль целостности кода ----------
def _code_files() -> list:
    import glob
    out = [os.path.join(BASE, "camera_bot.py"), os.path.join(BASE, "onvif_snap.py")]
    out += sorted(glob.glob(os.path.join(BASE, "bot_*.py")))
    return [f for f in dict.fromkeys(out) if os.path.exists(f)]


def check_code_hashes() -> list:
    """sha256 модулей в лог + сравнение с прошлым запуском. Возвращает изменённые."""
    cur = {}
    for fn in _code_files():
        try:
            with open(fn, "rb") as f:
                cur[os.path.basename(fn)] = hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError:
            pass
    path = st.cget("code_hash_path")
    prev = {}
    try:
        with open(path, encoding="utf-8") as f:
            prev = (json.load(f) or {}).get("hashes") or {}
    except Exception:
        pass
    changed = sorted(f for f in cur if f in prev and prev[f] != cur[f])
    added = sorted(f for f in cur if f not in prev)
    try:
        st._atomic_write(path, {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "hashes": cur})
    except Exception:
        pass
    total = hashlib.sha256("".join(sorted(cur.values())).encode()).hexdigest()[:12]
    log(f"CODE: {len(cur)} модулей, сводный sha {total}"
        + (f"; ИЗМЕНИЛИСЬ с прошлого запуска: {', '.join(changed)}" if changed
           else "") + (f"; новые: {', '.join(added)}" if added else ""))
    if changed:
        obs.jlog("code_changed", level="WARNING", files=changed)
    return changed


# ---------- helpers для /debug и /env ----------
def _run(cmd: list, timeout: int = None) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True,
                           timeout=timeout or st.cget("subproc_timeout_s"),
                           creationflags=CREATE_NO_WINDOW)
        return (r.stdout or b"").decode("cp866", "replace")
    except Exception as e:
        return f"<{type(e).__name__}: {e}>"


def _tail_bytes(path: str, n: int = 200 * 1024) -> bytes:
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - n))
            return f.read()
    except OSError:
        return b""


# ---------- 208: /debug — диагностический архив ----------
def build_debug_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path, n in (
                ("camera_bot.log", LOG_PATH, 300 * 1024),
                ("bot_stderr.log", os.path.join(BASE, "bot_stderr.log"), 100 * 1024),
                ("camera_bot.jsonl", st.cget("obs_jsonl_path"), 300 * 1024),
                ("slow.log", st.cget("slow_log_path"), 50 * 1024),
                ("audit.log", st.cget("audit_path"), 100 * 1024),
                ("metrics.csv", st.cget("metrics_csv_path"), 100 * 1024),
                ("restarts.csv", st.cget("restarts_csv"), 50 * 1024)):
            data = _tail_bytes(path, n)
            if data:
                z.writestr(name, data)
        z.writestr("events_ring.json",
                   json.dumps({"events": list(obs.RING)}, ensure_ascii=False,
                              default=str))
        cfg = {k: v for k, v in st.CFG.items() if k != "token"}  # без токена!
        cfg["token"] = "<скрыт>"
        z.writestr("config_no_token.json",
                   json.dumps(cfg, ensure_ascii=False, indent=1, default=str))
        z.writestr("env_snapshot.json",
                   json.dumps(env_snapshot(), ensure_ascii=False, indent=1,
                              default=str))
        z.writestr("chan_status.json",
                   json.dumps(chan.status(), ensure_ascii=False, indent=1))
        z.writestr("tg_fronts.json",
                   json.dumps(chan.probe_fronts(), ensure_ascii=False, indent=1))
        z.writestr("poll_stats.json",
                   json.dumps({"poll": obs.poll_stats(), "tg": obs.tg_hist(),
                               "chan": obs.channel_state(),
                               "drift_s": obs.drift_s()}, ensure_ascii=False))
        z.writestr("ipconfig_all.txt", _run(["ipconfig", "/all"]))
        z.writestr("route_print.txt", _run(["route", "print", "-4"]))
        import glob
        d = st.cget("crash_dir")
        for fn in sorted(glob.glob(os.path.join(d, "crash_*.json")))[-3:]:
            z.writestr("crash/" + os.path.basename(fn), _tail_bytes(fn, 200 * 1024))
        for fn in sorted(glob.glob(os.path.join(BASE, "state_dump_*.txt")))[-2:]:
            z.writestr("dumps/" + os.path.basename(fn), _tail_bytes(fn, 200 * 1024))
    return buf.getvalue()


def cmd_debug(chat, arg="", reply_to=None):
    """208: один тап вместо RDP — логи, ring, конфиг без токена, сеть, версии."""
    chat_action(chat, "upload_document")
    send(chat, "🧰 Собираю диагностический архив…", silent=True, reply_to=reply_to)
    t0 = time.time()
    data = build_debug_zip()
    fn = f"debug_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    send_document(chat, data, fn,
                  caption=f"🧰 Диагностика v{BOT_VERSION} · "
                          f"{len(data) // 1024} КБ · за {time.time() - t0:.1f}s")


# ---------- 209: /mem — tracemalloc ----------
def cmd_mem(chat, arg="", reply_to=None):
    import tracemalloc
    from bot_util import mem_mb
    a = (arg or "").strip().lower()
    rss = mem_mb()
    rss_s = f"{rss:.0f} МБ" if rss is not None else "—"
    if a in ("on", "start", "вкл"):
        if not tracemalloc.is_tracing():
            tracemalloc.start(int(st.cget("tracemalloc_frames")))
        _mem_snap[0] = tracemalloc.take_snapshot()
        send(chat, f"🧠 tracemalloc ВКЛ (RSS {rss_s}). Погоняй бота и вызови "
                   f"/mem — покажу топ роста аллокаций.", reply_to=reply_to)
        return
    if a in ("off", "stop", "выкл"):
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        _mem_snap[0] = None
        send(chat, f"🧠 tracemalloc ВЫКЛ (RSS {rss_s}).", reply_to=reply_to)
        return
    if not tracemalloc.is_tracing():
        send(chat, f"🧠 RSS {rss_s}. tracemalloc выключен — включи "
                   f"<code>/mem on</code>, затем /mem покажет топ-10 "
                   f"роста аллокаций (ловля утечек).", reply_to=reply_to)
        return
    snap = tracemalloc.take_snapshot()
    lines = [f"🧠 <b>tracemalloc</b> · RSS {rss_s} · "
             f"трассируется {tracemalloc.get_traced_memory()[0] // 1024} КБ"]
    if _mem_snap[0] is not None:
        stats = snap.compare_to(_mem_snap[0], "lineno")[:10]
        lines.append("Топ-10 diff с прошлого снимка:")
        for s_ in stats:
            fr = s_.traceback[0]
            lines.append(f"<code>{esc(os.path.basename(fr.filename))}:{fr.lineno}"
                         f"</code> {s_.size_diff / 1024:+.0f} КБ "
                         f"({s_.count_diff:+d} блоков)")
    else:
        for s_ in snap.statistics("lineno")[:10]:
            fr = s_.traceback[0]
            lines.append(f"<code>{esc(os.path.basename(fr.filename))}:{fr.lineno}"
                         f"</code> {s_.size / 1024:.0f} КБ ({s_.count} блоков)")
    _mem_snap[0] = snap
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- 237: /trace — tracert до камеры ----------
def cmd_trace(chat, arg="", reply_to=None):
    ip = (arg or "").strip()
    if not net.valid_ip(ip):
        send(chat, "Сетевая трасса: <code>/trace 10.20.50.51</code> — "
                   "покажет, где рвётся маршрут до камеры.", reply_to=reply_to)
        return
    chat_action(chat)
    send(chat, f"🛰 tracert до <code>{ip}</code> (до ~60с)…", silent=True,
         reply_to=reply_to)
    out = _run(["tracert", "-d", "-w", "700", "-h", "12", ip], timeout=90)
    out = "\n".join(ln.rstrip() for ln in out.splitlines() if ln.strip())[:3500]
    send(chat, f"🛰 <b>Трасса до {ip}</b>\n<pre>{esc(out or 'пусто')}</pre>")


# ---------- 249: /env — сетевая самодиагностика ----------
def _route_excerpt() -> str:
    raw = _run(["route", "print", "-4"])
    keep = []
    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith("0.0.0.0") or s.startswith("10.20.") or s.startswith("10.10."):
            keep.append(s)
    return "\n".join(keep[:12]) or "маршруты не прочитались"


def _iface_to(dst: str):
    """Каким локальным IP уходим к dst (UDP-connect, пакеты не шлются)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect((dst, 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def cmd_env(chat, arg="", reply_to=None):
    """249: две NIC + Amnezia/WFP — главный источник «внезапно всё сломалось»."""
    chat_action(chat)
    cs = chan.status()
    fronts = chan.probe_fronts()
    ok_f = [f"{f['ip']} {f['ms']}мс" for f in fronts if f.get("ok")]
    bad_f = [f"{f['ip']} ✖{f.get('err', '')}" for f in fronts if not f.get("ok")]
    cam_if = _iface_to("10.20.50.254")
    tg_ip = (cs.get("dns_cache") or [None])[0]
    tg_if = _iface_to(tg_ip) if tg_ip else None
    hb_age = "—"
    try:
        hb_age = f"{time.time() - os.path.getmtime(st.HEARTBEAT_PATH):.0f}s"
    except OSError:
        pass
    lines = [
        "🌐 <b>Сеть машины</b>",
        f"Адаптеры: {esc(', '.join(cs.get('local_ips') or []) or '—')}",
        f"Исходящий IP (default): <code>{esc(cs.get('out_ip') or '—')}</code>",
        f"К камерам 10.20.x уходим с: <code>{esc(cam_if or 'нет маршрута!')}</code>",
        f"К api.telegram.org уходим с: <code>{esc(tg_if or '—')}</code>",
        f"DNS: {'⚠️ залип' if cs.get('dns_stuck') else 'OK'} · "
        f"кэш: {esc(', '.join(cs.get('dns_cache') or []) or 'пуст')}",
        f"Фронты TG: {esc('; '.join(ok_f) or 'ни один не ответил!')}"
        + (f" · мертвы: {esc('; '.join(bad_f))}" if bad_f else ""),
        f"Канал: {esc(obs.channel_state())} · канарейка "
        f"{cs.get('canary_ok_ago_s', '—')}s назад · heartbeat {esc(hb_age)}",
        f"<pre>{esc(_route_excerpt())}</pre>",
    ]
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- регистрация ----------
try:  # 204: проверка dump_now.flag в минутном тике
    import bot_health as _bh
    if _tick not in _bh.MINUTE_TICKS:
        _bh.MINUTE_TICKS.append(_tick)
except Exception:
    log("bot_debug: тик не зарегистрирован", logging.WARNING)

HANDLERS = {
    "/debug": cmd_debug, "/mem": cmd_mem, "/trace": cmd_trace,
    "/env": cmd_env, "/crashes": cmd_crashes,
}
ALIASES = {
    "/дебаг": "/debug", "/память": "/mem", "/трасса": "/trace",
    "/окружение": "/env", "/крэши": "/crashes",
}
CALLBACKS = {}
