# -*- coding: utf-8 -*-
"""Волна E — наблюдаемость процесса (bot_obs):
201 NDJSON-лог camera_bot.jsonl · 202 trace-id через contextvars ·
203 канонический event-лог «одна строка на команду» · 207 ring-buffer 500 ·
212 append-only аудит с hash-chain · 213 перцентили латентности getUpdates ·
214 машина состояний канала GOOD/DEGRADED/BAD · 215 автоадаптация таймаутов ·
239 распределение ретраев tg() · 210/211/224 handles/threads/RSS ·
222 metrics.csv раз в 5 мин · 240 slow.log · 241 контроль диска ·
243 детект сна машины · 250 дрейф времени vs Date-заголовок Telegram.
Минутный тик регистрируется в bot_health.MINUTE_TICKS через register().
Только stdlib; bot_tg импортируется лениво (алерты владельцу)."""
import os
import json
import time
import shutil
import hashlib
import logging
import threading
import contextvars
from collections import deque

import bot_state as st
import bot_util
from bot_util import log, log_exc

BASE = st.BASE
RING: deque = deque(maxlen=int(st.cget("obs_ring_size")))   # 207
_TRACE = contextvars.ContextVar("trace", default=None)      # 202
_jl_lock = threading.Lock()
_slow_lock = threading.Lock()
_registered = [False]

# ---------- 202: trace-id ----------
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def b36(n) -> str:
    n = abs(int(n))
    if n == 0:
        return "0"
    s = ""
    while n:
        s = _B36[n % 36] + s
        n //= 36
    return s


def set_trace(update_id) -> None:
    _TRACE.set(b36(update_id) if update_id is not None else None)


def get_trace():
    return _TRACE.get()


# ---------- 201 + 207: NDJSON-лог и ring-buffer ----------
def _rotate(path: str) -> None:
    try:
        if os.path.getsize(path) > int(st.cget("obs_jsonl_max_mb")) * 1024 * 1024:
            os.replace(path, path + ".1")
    except OSError:
        pass


def jlog(event: str, level: str = "INFO", **fields) -> dict:
    """Строка NDJSON {"ts","level","event",...} + копия в ring-buffer."""
    rec = {"ts": round(time.time(), 3), "level": level, "event": event}
    tid = _TRACE.get()
    if tid:
        rec["trace"] = tid
    for k, v in fields.items():
        rec.setdefault(k, v)
    RING.append(rec)
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
    except Exception:
        return rec
    path = st.cget("obs_jsonl_path")
    with _jl_lock:
        try:
            _rotate(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    return rec


def _on_log(level: int, msg: str) -> None:
    """Хук bot_util.log: каждая текстовая запись дублируется в NDJSON (201)."""
    jlog("log", level=logging.getLevelName(level), msg=str(msg)[:800])


def ring_dump(path: str = None, reason: str = "") -> str:
    """207: сброс «чёрного ящика» в events_ring.json (краш, /debug)."""
    path = path or os.path.join(BASE, "events_ring.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"saved_at": round(time.time(), 3), "reason": reason,
                       "events": list(RING)}, f, ensure_ascii=False, default=str)
        return path
    except Exception:
        return ""


# ---------- 240: slow.log ----------
def _slow(text: str) -> None:
    with _slow_lock:
        try:
            with open(st.cget("slow_log_path"), "a", encoding="utf-8") as f:
                tid = _TRACE.get() or "-"
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{tid}] {text}\n")
        except Exception:
            pass


# ---------- 203: канонический event-лог команд ----------
def note_cmd(cmd, arg, dur_s, ok, err=None, tg_retries=0, sent=0) -> None:
    extra = {"err": str(err)[:200]} if err else {}
    jlog("cmd", level=("INFO" if ok else "ERROR"), cmd=str(cmd),
         arg=str(arg)[:120], dur=round(float(dur_s), 2), ok=bool(ok),
         tg_retries=int(tg_retries), sent=int(sent),
         profile=str(st.cget("active_profile")), **extra)
    if dur_s >= float(st.cget("slow_threshold_s")):
        _slow(f"cmd {cmd} {str(arg)[:80]!r} {dur_s:.1f}s ok={ok}"
              + (f" err={err}" if err else ""))


# ---------- 212: аудит-журнал с hash-chain ----------
_audit_lock = threading.Lock()
_audit_prev = [""]


def _load_audit_tail() -> None:
    try:
        with open(st.cget("audit_path"), "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - 8192))
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        if lines:
            _audit_prev[0] = hashlib.sha256(lines[-1]).hexdigest()
    except Exception:
        _audit_prev[0] = ""


def audit(cmd, arg, result) -> None:
    with _audit_lock:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "cmd": str(cmd),
               "arg": str(arg)[:200], "result": str(result)[:200],
               "prev": _audit_prev[0]}
        line = json.dumps(rec, ensure_ascii=False)
        try:
            with open(st.cget("audit_path"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _audit_prev[0] = hashlib.sha256(line.encode("utf-8")).hexdigest()
        except Exception:
            pass


def audit_verify(path: str = None):
    """Проверка hash-chain: (ok, всего строк, номер первой битой или None)."""
    path = path or st.cget("audit_path")
    prev = ""
    try:
        with open(path, "rb") as f:
            raw = [ln for ln in f.read().splitlines() if ln.strip()]
    except Exception:
        return True, 0, None
    for i, ln in enumerate(raw, 1):
        try:
            rec = json.loads(ln.decode("utf-8"))
        except Exception:
            return False, len(raw), i
        if rec.get("prev", "") != prev:
            return False, len(raw), i
        prev = hashlib.sha256(ln).hexdigest()
    return True, len(raw), None


# ---------- 213: латентность getUpdates + перцентили ----------
_polls: deque = deque(maxlen=int(st.cget("chan_window")))  # (overshoot_s, ok)
_poll_lock = threading.Lock()
LAST_POLL_OK = [0.0]     # ts последнего успешного getUpdates
LAST_UPDATE_TS = [0.0]   # ts последнего НЕпустого getUpdates (для sentinel 246)
_nonjson = [0]           # счётчик не-JSON ответов (заглушка DPI)


def pctl(values, q):
    """Перцентиль по методу ближайшего ранга; None на пустом списке."""
    if not values:
        return None
    vs = sorted(values)
    k = max(0, min(len(vs) - 1, int(round(q / 100.0 * (len(vs) - 1)))))
    return vs[k]


def note_poll(dur_s: float, ok: bool, empty: bool = True,
              timeout_s: float = 30.0) -> None:
    """213: перебор сверх long-poll-таймаута — сигнал деградации канала."""
    over = max(0.0, float(dur_s) - float(timeout_s))
    with _poll_lock:
        _polls.append((over, bool(ok)))
    if ok:
        LAST_POLL_OK[0] = time.time()
        if not empty:
            LAST_UPDATE_TS[0] = time.time()


def note_nonjson() -> None:
    _nonjson[0] += 1


def poll_stats() -> dict:
    with _poll_lock:
        rows = list(_polls)
    overs = [o for o, ok in rows if ok]
    fails = sum(1 for _o, ok in rows if not ok)
    n = len(rows)
    return {"n": n, "fail": (fails / n) if n else 0.0,
            "p50": pctl(overs, 50), "p95": pctl(overs, 95),
            "p99": pctl(overs, 99), "nonjson": _nonjson[0]}


# ---------- 214: машина состояний канала ----------
_chan = {"state": "GOOD", "since": time.time()}


def classify(p95_over, fail_share, cfg=None) -> str:
    """Чистая функция для тестов: состояние по p95-перебору и доле неудач."""
    g = cfg or {k: st.cget(k) for k in
                ("chan_over_degraded_s", "chan_over_bad_s",
                 "chan_fail_degraded", "chan_fail_bad")}
    p95 = p95_over or 0.0
    if p95 >= float(g["chan_over_bad_s"]) or fail_share >= float(g["chan_fail_bad"]):
        return "BAD"
    if (p95 >= float(g["chan_over_degraded_s"])
            or fail_share >= float(g["chan_fail_degraded"])):
        return "DEGRADED"
    return "GOOD"


def channel_state() -> str:
    return _chan["state"]


_ICON = {"GOOD": "🟢", "DEGRADED": "🟡", "BAD": "🔴"}


def _eval_channel(alert: bool = True) -> str:
    s = poll_stats()
    if s["n"] < 20:      # мало данных — не дёргаемся
        return _chan["state"]
    new = classify(s["p95"], s["fail"])
    if new != _chan["state"]:
        old, dur = _chan["state"], time.time() - _chan["since"]
        _chan["state"], _chan["since"] = new, time.time()
        log(f"CHAN: канал {old} → {new} (p95 сверх poll {s['p95'] or 0:.1f}s, "
            f"неудач {s['fail'] * 100:.0f}%, был {old} {dur / 60:.0f} мин)",
            logging.WARNING)
        jlog("chan_state", level="WARNING", old=old, new=new,
             p95=s["p95"], fail=round(s["fail"], 2))
        if alert:
            _owner(f"{_ICON.get(new, '⚪')} <b>Канал Telegram: {new}</b> "
                   f"(был {old})\np95 сверх poll: {s['p95'] or 0:.1f}s · "
                   f"неудач: {s['fail'] * 100:.0f}%", silent=(new == "GOOD"),
                   aid="chan_state")
    return _chan["state"]


def _owner(text: str, silent: bool = True, aid: str = None) -> None:
    if aid:
        try:
            import bot_alerts
            if bot_alerts.muted(aid):
                return
        except Exception:
            pass
    try:
        import bot_tg as tgm
        owner = st.cget("owner_chat_id")
        if owner:
            tgm.send(owner, text, silent=silent)
    except Exception:
        log_exc("obs: алерт владельцу не ушёл")


# ---------- 215: автоадаптация таймаутов ----------
def timeout_factor() -> float:
    """Множитель к connect/read-таймаутам tg() по состоянию канала."""
    if not st.cget("obs_adapt_timeouts"):
        return 1.0
    return {"GOOD": 1.0, "DEGRADED": 1.5, "BAD": 2.0}.get(_chan["state"], 1.0)


def poll_timeout() -> int:
    """Long-poll: в DEGRADED/BAD укорачиваем — короткие запросы легче под DPI."""
    base = int(st.cget("poll_timeout_s"))
    if not st.cget("obs_adapt_timeouts"):
        return base
    return {"GOOD": base, "DEGRADED": min(base, 15),
            "BAD": min(base, 10)}.get(_chan["state"], base)


# ---------- 239: распределение ретраев tg() ----------
_tg_attempts: dict = {}          # «прошёл с i-й попытки» -> счётчик
_tg_fail = [0]
_tg_lock = threading.Lock()


def note_tg(attempts: int, ok: bool) -> None:
    with _tg_lock:
        if ok:
            _tg_attempts[int(attempts)] = _tg_attempts.get(int(attempts), 0) + 1
        else:
            _tg_fail[0] += 1


def tg_hist() -> dict:
    with _tg_lock:
        total = sum(_tg_attempts.values())
        first = _tg_attempts.get(1, 0)
        return {"total": total, "fails": _tg_fail[0],
                "first_try_pct": round(100.0 * first / total, 1) if total else 100.0,
                "hist": dict(sorted(_tg_attempts.items()))}


# ---------- 250: дрейф времени по Date-заголовку Telegram ----------
_drift = {"last": 0.0, "warned": 0.0}


def note_server_date(hdr) -> None:
    if not hdr:
        return
    try:
        from email.utils import parsedate_to_datetime
        srv = parsedate_to_datetime(hdr).timestamp()
    except Exception:
        return
    d = time.time() - srv
    _drift["last"] = d
    if abs(d) > 30 and time.time() - _drift["warned"] > 3600:
        _drift["warned"] = time.time()
        log(f"OBS: дрейф системного времени vs Telegram {d:+.0f}s — "
            f"проверь часы машины (после сна?)", logging.WARNING)
        jlog("time_drift", level="WARNING", drift_s=round(d, 1))


def drift_s() -> float:
    return round(_drift["last"], 1)


# ---------- 210/211/224: handles / threads / RSS ----------
def handle_count():
    """210: GetProcessHandleCount — рост = утечка сокетов/файлов."""
    try:
        import ctypes
        import ctypes.wintypes as wt
        cnt = wt.DWORD(0)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.kernel32.GetProcessHandleCount(h, ctypes.byref(cnt)):
            return int(cnt.value)
    except Exception:
        pass
    return None


def proc_metrics() -> dict:
    from bot_util import mem_mb
    return {"rss_mb": mem_mb(), "threads": len(threading.enumerate()),
            "handles": handle_count()}


# ---------- 243: детект сна машины ----------
def sleep_gap(prev_mono, prev_wall, mono, wall, thr_s: float = 120.0):
    """Чистая функция: скачок wall-clock сверх monotonic > порога -> секунды сна."""
    if prev_mono is None or prev_wall is None:
        return None
    gap = (wall - prev_wall) - (mono - prev_mono)
    return gap if gap >= thr_s else None


# ---------- 222 + 241: metrics.csv и диск ----------
_CSV_HDR = ("ts;chan;p50_over;p95_over;fail_pct;polls;nonjson;rss_mb;threads;"
            "handles;tg_first_try_pct;tg_fails;errors_hour;drift_s;"
            "disk_free_gb;profile")


def _write_metrics() -> None:
    s = poll_stats()
    pm = proc_metrics()
    th = tg_hist()
    try:
        free_gb = shutil.disk_usage(BASE).free / (1024 ** 3)
    except Exception:
        free_gb = -1
    errs = st.stats_snapshot().get("errors_hour", 0)
    row = ";".join(str(x) for x in (
        time.strftime("%Y-%m-%d %H:%M:%S"), _chan["state"],
        f"{(s['p50'] or 0):.1f}", f"{(s['p95'] or 0):.1f}",
        f"{s['fail'] * 100:.0f}", s["n"], s["nonjson"],
        f"{(pm['rss_mb'] or 0):.0f}", pm["threads"], pm["handles"] or "",
        th["first_try_pct"], th["fails"], errs, drift_s(),
        f"{free_gb:.2f}", st.cget("active_profile")))
    path = st.cget("metrics_csv_path")
    try:
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write(_CSV_HDR + "\n")
            f.write(row + "\n")
    except Exception:
        log_exc("obs: metrics.csv не записался")
    # пороги 224/211/210 — warning не чаще раза в сутки
    warns = []
    if pm["rss_mb"] and pm["rss_mb"] > float(st.cget("metrics_rss_warn_mb")):
        warns.append(f"RSS {pm['rss_mb']:.0f} МБ > {st.cget('metrics_rss_warn_mb')}")
    if pm["threads"] > int(st.cget("metrics_threads_warn")):
        warns.append(f"потоков {pm['threads']} > {st.cget('metrics_threads_warn')}")
    if pm["handles"] and pm["handles"] > int(st.cget("metrics_handles_warn")):
        warns.append(f"handles {pm['handles']} > {st.cget('metrics_handles_warn')}")
    if warns and time.time() - _tick_state["res_warned"] > 86400:
        _tick_state["res_warned"] = time.time()
        log("OBS: ресурсы процесса: " + "; ".join(warns), logging.WARNING)
        _owner("⚠️ <b>Ресурсы процесса бота</b>: " + "; ".join(warns)
               + "\nВозможна утечка — детали в metrics.csv и /mem.")


def _check_disk() -> None:
    """241: <disk_min_free_gb — алерт (раз в сутки) + чистка старых дампов."""
    try:
        free_gb = shutil.disk_usage(BASE).free / (1024 ** 3)
    except Exception:
        return
    if free_gb >= float(st.cget("disk_min_free_gb")):
        return
    removed = cleanup_dumps(days=14)
    if time.time() - _tick_state["disk_warned"] > 86400:
        _tick_state["disk_warned"] = time.time()
        log(f"OBS: мало места на диске: {free_gb:.2f} ГБ, "
            f"почищено {removed} старых файлов", logging.WARNING)
        _owner(f"💾 <b>Мало места на диске</b>: {free_gb:.2f} ГБ свободно. "
               f"Почистил старых дампов/архивов: {removed}.")


def cleanup_dumps(days: float = 14) -> int:
    """Удаляет state_dump_*.txt / debug_*.zip / crash_*.json старше days."""
    cut = time.time() - days * 86400
    removed = 0
    import glob
    pats = [os.path.join(BASE, "state_dump_*.txt"),
            os.path.join(BASE, "debug_*.zip"),
            os.path.join(st.cget("crash_dir"), "crash_*.json")]
    for pat in pats:
        for fn in glob.glob(pat):
            try:
                if os.path.getmtime(fn) < cut:
                    os.remove(fn)
                    removed += 1
            except OSError:
                pass
    return removed


# ---------- минутный тик ----------
_tick_state = {"last_metrics": 0.0, "prev_mono": None, "prev_wall": None,
               "disk_warned": 0.0, "res_warned": 0.0}


def _tick() -> None:
    now_m, now_w = time.monotonic(), time.time()
    gap = sleep_gap(_tick_state["prev_mono"], _tick_state["prev_wall"],
                    now_m, now_w)
    _tick_state["prev_mono"], _tick_state["prev_wall"] = now_m, now_w
    if gap:
        log(f"OBS: машина спала / часы прыгнули на ~{gap / 60:.0f} мин "
            f"({gap:.0f}s) — возможен протухший offset и дыра в логах",
            logging.WARNING)
        jlog("sleep_detected", level="WARNING", gap_s=round(gap))
    _eval_channel()
    if now_w - _tick_state["last_metrics"] >= float(st.cget("metrics_period_min")) * 60:
        _tick_state["last_metrics"] = now_w
        _write_metrics()
        _check_disk()
        s = poll_stats()
        if s["n"]:
            log(f"CHAN: {_chan['state']} · poll p50/p95 сверх таймаута "
                f"{(s['p50'] or 0):.1f}/{(s['p95'] or 0):.1f}s · неудач "
                f"{s['fail'] * 100:.0f}% · не-JSON {s['nonjson']} · "
                f"tg с 1-й попытки {tg_hist()['first_try_pct']}%")


def register() -> None:
    """Подключить NDJSON/ring к bot_util.log, trace-id и минутный тик."""
    if _registered[0]:
        return
    _registered[0] = True
    bot_util.LOG_HOOKS.append(_on_log)
    bot_util.TRACE_FN[0] = get_trace
    _load_audit_tail()
    try:
        import bot_health as _bh
        if _tick not in _bh.MINUTE_TICKS:
            _bh.MINUTE_TICKS.append(_tick)
    except Exception:
        log_exc("obs: тик не зарегистрирован")
