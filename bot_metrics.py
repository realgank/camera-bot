# -*- coding: utf-8 -*-
"""Волна G (347): SQLite-хранилище метрик парка — _metrics.db (stdlib sqlite3,
WAL). Таблицы: transitions (переходы up/down из health), runs/subnets
(агрегаты прогонов проб), metrics (числовые ряды per камера: rtt/jit/ttl/
snap_kb/onvif_ms/clock_off/rtsp_ms/rtsp_kbps), events (дискретные события:
frozen/black/clockjump/auth401/port_new/...), kv (курсоры ротаций, маркеры).
Ретенция metrics_keep_days (90 дн., чистка раз в сутки тиком). Старые
JSON-истории (_health_history.json и пр.) остаются на чтение для
совместимости; при первом старте история переходов импортируется однократно.
Плюс общие помощники волны G: rotation_batch (ротация малых порций по
онлайн-камерам), due (самотактирование тиков), owner_alert."""
import os
import time
import atexit
import sqlite3
import threading
import statistics
from typing import List, Optional, Tuple

import bot_state as st
from bot_util import log, log_exc

_lock = threading.RLock()
_conn: List[Optional[sqlite3.Connection]] = [None]
_due: dict = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transitions(
    ts INTEGER NOT NULL, ip TEXT NOT NULL, ev TEXT NOT NULL, dur INTEGER);
CREATE INDEX IF NOT EXISTS i_tr_ip ON transitions(ip, ts);
CREATE INDEX IF NOT EXISTS i_tr_ts ON transitions(ts);
CREATE TABLE IF NOT EXISTS runs(
    ts INTEGER NOT NULL, total INTEGER, online INTEGER);
CREATE TABLE IF NOT EXISTS subnets(
    ts INTEGER NOT NULL, subnet TEXT NOT NULL, total INTEGER, online INTEGER);
CREATE INDEX IF NOT EXISTS i_sub ON subnets(subnet, ts);
CREATE TABLE IF NOT EXISTS metrics(
    ts INTEGER NOT NULL, ip TEXT NOT NULL, kind TEXT NOT NULL, value REAL);
CREATE INDEX IF NOT EXISTS i_m_ip ON metrics(ip, kind, ts);
CREATE INDEX IF NOT EXISTS i_m_ts ON metrics(ts);
CREATE TABLE IF NOT EXISTS events(
    ts INTEGER NOT NULL, ip TEXT NOT NULL, kind TEXT NOT NULL, info TEXT);
CREATE INDEX IF NOT EXISTS i_e_ip ON events(ip, kind, ts);
CREATE INDEX IF NOT EXISTS i_e_ts ON events(ts);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def db() -> sqlite3.Connection:
    """Ленивое соединение (одно на процесс, WAL, автосоздание схемы)."""
    with _lock:
        if _conn[0] is None:
            path = st.cget("metrics_db_path")
            c = sqlite3.connect(path, check_same_thread=False, timeout=15)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.executescript(_SCHEMA)
            c.commit()
            _conn[0] = c
            _import_legacy()
        return _conn[0]


def close_db() -> None:
    """Закрыть соединение (atexit и тесты — смена metrics_db_path)."""
    with _lock:
        if _conn[0] is not None:
            try:
                _conn[0].close()
            except sqlite3.Error:
                pass
            _conn[0] = None


atexit.register(close_db)


def _import_legacy() -> None:
    """Разовый импорт _health_history.json в transitions (JSON не трогаем)."""
    try:
        if kv_get("imported_health_v1"):
            return
        import bot_health
        ev = bot_health.history_events()
        rows = [(int(e.get("ts") or 0), e.get("ip") or "", e.get("ev") or "",
                 e.get("dur")) for e in ev if e.get("ip") and e.get("ev")]
        with _lock:
            _conn[0].executemany(
                "INSERT INTO transitions(ts, ip, ev, dur) VALUES(?,?,?,?)", rows)
            _conn[0].commit()
        kv_set("imported_health_v1", str(int(time.time())))
        log(f"metrics: импортирована история переходов из JSON: {len(rows)} строк")
    except Exception:
        log_exc("metrics: импорт _health_history.json")


def _exec(sql: str, args: tuple = ()) -> None:
    with _lock:
        db().execute(sql, args)
        db().commit()


def _many(sql: str, rows: list) -> None:
    if not rows:
        return
    with _lock:
        db().executemany(sql, rows)
        db().commit()


def _q(sql: str, args: tuple = ()) -> list:
    with _lock:
        return db().execute(sql, args).fetchall()


# ---------- kv ----------
def kv_get(k: str) -> Optional[str]:
    with _lock:
        r = db().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def kv_set(k: str, v: str) -> None:
    _exec("INSERT INTO kv(k, v) VALUES(?,?) "
          "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ---------- приём данных из health (вызывается из bot_health.run_once) ----------
def on_run(results: List[Tuple[str, bool]], downs: List[str],
           ups: List[dict], now: float) -> None:
    """Агрегаты прогона + переходы -> SQLite (347)."""
    ts = int(now)
    _exec("INSERT INTO runs(ts, total, online) VALUES(?,?,?)",
          (ts, len(results), sum(1 for _i, ok in results if ok)))
    agg: dict = {}
    for ip, ok in results:
        sub = ip.rsplit(".", 1)[0]
        t, on = agg.get(sub, (0, 0))
        agg[sub] = (t + 1, on + (1 if ok else 0))
    _many("INSERT INTO subnets(ts, subnet, total, online) VALUES(?,?,?,?)",
          [(ts, s, t, on) for s, (t, on) in agg.items()])
    rows = [(ts, ip, "down", None) for ip in downs]
    rows += [(int(e["ts"]), e["ip"], "up", int(e.get("dur") or 0)) for e in ups]
    _many("INSERT INTO transitions(ts, ip, ev, dur) VALUES(?,?,?,?)", rows)


# ---------- переходы / аптайм ----------
def transitions(ip: Optional[str] = None, days: float = 7) -> List[dict]:
    cut = int(time.time() - days * 86400)
    if ip:
        rows = _q("SELECT ts, ip, ev, dur FROM transitions "
                  "WHERE ip=? AND ts>=? ORDER BY ts", (ip, cut))
    else:
        rows = _q("SELECT ts, ip, ev, dur FROM transitions "
                  "WHERE ts>=? ORDER BY ts", (cut,))
    return [{"ts": r[0], "ip": r[1], "ev": r[2], "dur": r[3]} for r in rows]


def downs_count(days: float = 7, ip: Optional[str] = None) -> dict:
    cut = int(time.time() - days * 86400)
    if ip:
        rows = _q("SELECT ip, COUNT(*) FROM transitions "
                  "WHERE ev='down' AND ts>=? AND ip=? GROUP BY ip", (cut, ip))
    else:
        rows = _q("SELECT ip, COUNT(*) FROM transitions "
                  "WHERE ev='down' AND ts>=? GROUP BY ip", (cut,))
    return dict(rows)


def micro_reboots(days: float = 7, max_dur: int = 180) -> dict:
    """341: восстановления с простоем < max_dur c = микро-ребуты, per ip."""
    cut = int(time.time() - days * 86400)
    rows = _q("SELECT ip, COUNT(*) FROM transitions WHERE ev='up' AND ts>=? "
              "AND dur IS NOT NULL AND dur>0 AND dur<? GROUP BY ip",
              (cut, max_dur))
    return dict(rows)


def _down_windows(ip: str, t0: float, t1: float) -> List[Tuple[float, float]]:
    """Окна простоя камеры, зажатые в [t0, t1] (из ups.dur + текущего down)."""
    wins = []
    rows = _q("SELECT ts, dur FROM transitions WHERE ip=? AND ev='up' "
              "AND ts>=? AND dur IS NOT NULL", (ip, int(t0)))
    for ts, dur in rows:
        a, b = max(t0, ts - (dur or 0)), min(t1, ts)
        if b > a:
            wins.append((a, b))
    try:
        import bot_health
        cur = bot_health.snapshot()["ips"].get(ip)
        if cur and cur.get("ok") is False:
            a = max(t0, cur.get("since", t1))
            if t1 > a:
                wins.append((a, t1))
    except Exception:
        pass
    return wins


def downtime_s(ip: str, t0: float, t1: float,
               exclude: Optional[List[Tuple[float, float]]] = None) -> float:
    """Суммарный простой в [t0,t1]; exclude — окна работ (344, /maint)."""
    total = 0.0
    for a, b in _down_windows(ip, t0, t1):
        d = b - a
        for xa, xb in exclude or []:
            d -= max(0.0, min(b, xb) - max(a, xa))
        total += max(0.0, d)
    return total


def uptime_pct(ip: str, days: float = 7) -> float:
    now = time.time()
    win = days * 86400
    return max(0.0, 100.0 * (1 - downtime_s(ip, now - win, now) / win))


def daily_uptime(ip: str, days: int = 14) -> List[float]:
    """Аптайм по суткам (для спарклайна 348), старые -> свежие."""
    now = time.time()
    out = []
    for d in range(days, 0, -1):
        t1 = now - (d - 1) * 86400
        t0 = t1 - 86400
        out.append(max(0.0, 100.0 * (1 - downtime_s(ip, t0, t1) / 86400)))
    return out


# ---------- метрики ----------
def metric_add(ip: str, kind: str, value: float, ts: Optional[int] = None) -> None:
    _exec("INSERT INTO metrics(ts, ip, kind, value) VALUES(?,?,?,?)",
          (int(ts or time.time()), ip, kind, float(value)))


def series(ip: str, kind: str, days: float = 7) -> List[Tuple[int, float]]:
    cut = int(time.time() - days * 86400)
    return _q("SELECT ts, value FROM metrics WHERE ip=? AND kind=? AND ts>=? "
              "ORDER BY ts", (ip, kind, cut))


def med(ip: str, kind: str, days: float = 7,
        min_n: int = 3) -> Optional[float]:
    vals = [v for _t, v in series(ip, kind, days)]
    if len(vals) < min_n:
        return None
    return statistics.median(vals)


def last_value(ip: str, kind: str) -> Optional[float]:
    r = _q("SELECT value FROM metrics WHERE ip=? AND kind=? "
           "ORDER BY ts DESC LIMIT 1", (ip, kind))
    return r[0][0] if r else None


# ---------- события ----------
def event_add(ip: str, kind: str, info: str = "",
              cooldown_h: float = 24) -> bool:
    """Пишет событие; True = «свежее» (такого kind по ip не было cooldown_h) —
    сигнал модулю, что можно алертить, не спамя."""
    cut = int(time.time() - cooldown_h * 3600)
    fresh = not _q("SELECT 1 FROM events WHERE ip=? AND kind=? AND ts>=? "
                   "LIMIT 1", (ip, kind, cut))
    _exec("INSERT INTO events(ts, ip, kind, info) VALUES(?,?,?,?)",
          (int(time.time()), ip, kind, info[:300]))
    return fresh


def events(ip: Optional[str] = None, kind: Optional[str] = None,
           days: float = 7) -> List[dict]:
    cut = int(time.time() - days * 86400)
    sql, args = "SELECT ts, ip, kind, info FROM events WHERE ts>=?", [cut]
    if ip:
        sql += " AND ip=?"
        args.append(ip)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    rows = _q(sql + " ORDER BY ts", tuple(args))
    return [{"ts": r[0], "ip": r[1], "kind": r[2], "info": r[3]} for r in rows]


def event_counts(days: float = 7) -> dict:
    cut = int(time.time() - days * 86400)
    return dict(_q("SELECT kind, COUNT(*) FROM events WHERE ts>=? "
                   "GROUP BY kind", (cut,)))


# ---------- общие помощники волны G ----------
def due(name: str, period_min: float, first_delay_s: float = 180) -> bool:
    """Самотактирование тиков: True раз в period_min (первый — с задержкой,
    чтобы тяжёлые ротации не совпали и не мешали старту)."""
    now = time.time()
    if name not in _due:
        _due[name] = now + first_delay_s
        return False
    if now >= _due[name]:
        _due[name] = now + period_min * 60
        return True
    return False


def rotation_batch(name: str, n: int) -> List[str]:
    """316: следующая порция из онлайн-камер health-надзора (по кругу)."""
    try:
        import bot_health
        snap = bot_health.snapshot()["ips"]
    except Exception:
        return []
    ips = sorted((ip for ip, e in snap.items() if e.get("ok")),
                 key=lambda x: tuple(int(o) for o in x.split(".")))
    if not ips or n <= 0:
        return []
    cur = int(kv_get(f"cursor:{name}") or 0) % len(ips)
    batch = [ips[(cur + i) % len(ips)] for i in range(min(n, len(ips)))]
    kv_set(f"cursor:{name}", str((cur + len(batch)) % len(ips)))
    return batch


ALERT_HOOKS: list = []   # Волна I (402): подписчики алертов (журнал в Sheets)


def owner_alert(text: str, silent: bool = False, aid: str = None) -> None:
    if aid:  # /alerts: выключенные фоновые алерты не шлём
        try:
            import bot_alerts
            if bot_alerts.muted(aid):
                return
        except Exception:
            pass
    for h in list(ALERT_HOOKS):  # 402: лист «Журнал событий» и пр.
        try:
            h(text)
        except Exception:
            log_exc("metrics: alert-hook")
    try:
        owner = st.cget("owner_chat_id")
        if owner:
            import bot_tg as tgm
            tgm.send(owner, text, silent=silent)
    except Exception:
        log_exc("metrics: owner_alert")


def db_stats() -> dict:
    out = {}
    for t in ("transitions", "runs", "subnets", "metrics", "events"):
        out[t] = _q(f"SELECT COUNT(*) FROM {t}")[0][0]
    try:
        out["size_mb"] = round(os.path.getsize(st.cget("metrics_db_path"))
                               / 1048576, 1)
    except OSError:
        out["size_mb"] = 0
    return out


# ---------- ретенция (тик раз в сутки) ----------
def cleanup() -> int:
    cut = int(time.time() - float(st.cget("metrics_keep_days")) * 86400)
    n = 0
    with _lock:
        for t in ("transitions", "runs", "subnets", "metrics", "events"):
            n += db().execute(f"DELETE FROM {t} WHERE ts<?", (cut,)).rowcount
        db().commit()
        try:
            db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
    if n:
        log(f"metrics: ретенция — удалено {n} строк старше "
            f"{st.cget('metrics_keep_days')} дн.")
    return n


def _tick() -> None:
    if due("metrics_cleanup", 24 * 60, first_delay_s=1800):
        cleanup()


try:
    import bot_health as _bh
    _bh.MINUTE_TICKS.append(_tick)
except Exception:
    pass

HANDLERS: dict = {}
ALIASES: dict = {}
CALLBACKS: dict = {}
