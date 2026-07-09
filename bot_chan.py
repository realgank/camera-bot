# -*- coding: utf-8 -*-
"""Волна E — качество канала и сеть машины (bot_chan):
216 канарейка getMe вторым путём (long-poll молчит, а канарейка жива = залип
именно poll) · 217 проба всех A-записей api.telegram.org (TCP 443 + время) ·
218 детект залипшего DNS (getaddrinfo с дедлайном в отдельном потоке) ·
219 DNS-кэш _dns_cache.json с TTL (хранение/обновление; сама подмена IP в
HTTPS-запросах НЕ делается — сломала бы проверку сертификата) ·
220 детект смены исходящего IP (UDP-connect трюк) · 221 поллинг адресов
адаптеров раз в минуту (облегчённо, без NotifyAddrChange) ·
246 sentinel тихого залипания long-poll -> reset Session.
Тик регистрируется в bot_health.MINUTE_TICKS при импорте."""
import json
import time
import socket
import logging
import threading

import bot_state as st
import bot_obs as obs
from bot_util import log, log_exc

TG_HOST = "api.telegram.org"

_state = {"last_canary": 0.0, "canary_ok_ts": 0.0, "canary_ms": None,
          "last_dns": 0.0, "dns_stuck": False, "dns_ok_ts": 0.0,
          "out_ip": None, "loc_ips": None, "sentinel_ts": 0.0}


# ---------- 216: канарейка ----------
def canary() -> bool:
    """getMe вне очереди getUpdates: жив ли API-путь в принципе."""
    import bot_tg as tgm
    t0 = time.time()
    r = tgm.tg("getMe", {}, retries=1, timeout=(5, 10))
    ok = bool(r and r.get("ok"))
    ms = round((time.time() - t0) * 1000)
    if ok:
        _state["canary_ok_ts"] = time.time()
        _state["canary_ms"] = ms
    obs.jlog("canary", ok=ok, ms=ms)
    return ok


# ---------- 218 + 219: DNS ----------
def _resolve(host: str = TG_HOST, deadline: float = None):
    """getaddrinfo в отдельном потоке с дедлайном. None = резолвер завис."""
    deadline = deadline or float(st.cget("dns_deadline_s"))
    res = {}

    def worker():
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_INET,
                                       socket.SOCK_STREAM)
            res["ips"] = sorted({i[4][0] for i in infos})
        except Exception as e:
            res["err"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=worker, daemon=True, name="dns-probe")
    t.start()
    t.join(deadline)
    if t.is_alive():
        return None
    return res


def cached_ips() -> list:
    try:
        with open(st.cget("dns_cache_path"), encoding="utf-8") as f:
            d = json.load(f)
        return list(d.get("ips") or [])
    except Exception:
        return []


def _save_dns_cache(ips: list) -> None:
    try:
        st._atomic_write(st.cget("dns_cache_path"),
                         {"host": TG_HOST, "ips": ips, "ts": int(time.time())})
    except Exception:
        pass


def dns_check() -> None:
    """218: зависший резолвер (после сна/смены сети Windows) — флаг и кэш."""
    res = _resolve()
    if res is None:
        if not _state["dns_stuck"]:
            log(f"CHAN: DNS-резолвер завис (getaddrinfo {TG_HOST} > "
                f"{st.cget('dns_deadline_s')}s) — кэш: {cached_ips()}",
                logging.WARNING)
            obs.jlog("dns_stuck", level="WARNING", cache=cached_ips())
        _state["dns_stuck"] = True
        return
    _state["dns_stuck"] = False
    if res.get("ips"):
        _state["dns_ok_ts"] = time.time()
        _save_dns_cache(res["ips"])   # 219: фоновое обновление кэша
    elif res.get("err"):
        obs.jlog("dns_fail", level="WARNING", err=res["err"])


# ---------- 217: проба фронтов Telegram ----------
def probe_fronts(timeout: float = 3.0) -> list:
    """TCP-connect к каждой A-записи api.telegram.org с замером времени."""
    res = _resolve(deadline=timeout + 2)
    ips = (res or {}).get("ips") or cached_ips()
    out = []
    for ip in ips:
        t0 = time.time()
        try:
            s = socket.create_connection((ip, 443), timeout=timeout)
            s.close()
            out.append({"ip": ip, "ok": True,
                        "ms": round((time.time() - t0) * 1000)})
        except Exception as e:
            out.append({"ip": ip, "ok": False, "err": type(e).__name__})
    return out


# ---------- 220 + 221: исходящий IP и адреса адаптеров ----------
def out_ip():
    """UDP-connect трюк: локальный адрес маршрута по умолчанию (пакеты не шлются)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def local_ips() -> list:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        return sorted({i[4][0] for i in infos})
    except Exception:
        return []


def _netwatch() -> None:
    """Смена исходящего IP (VPN поднялся/упал) или набора адресов адаптеров
    -> лог + пересоздание requests.Session (мёртвые keep-alive)."""
    oip, lips = out_ip(), local_ips()
    changed = []
    if _state["out_ip"] is not None and oip and oip != _state["out_ip"]:
        changed.append(f"исходящий IP {_state['out_ip']} → {oip}")
    if _state["loc_ips"] is not None and lips and lips != _state["loc_ips"]:
        changed.append(f"адаптеры {_state['loc_ips']} → {lips}")
    if oip:
        _state["out_ip"] = oip
    if lips:
        _state["loc_ips"] = lips
    if changed:
        log("CHAN: сеть машины изменилась: " + "; ".join(changed)
            + " — пересоздаю Session", logging.WARNING)
        obs.jlog("net_change", level="WARNING", detail="; ".join(changed))
        try:
            import bot_tg as tgm
            tgm.reset_session()
        except Exception:
            log_exc("chan: reset_session")


# ---------- 246: sentinel тихого залипания long-poll ----------
def _sentinel() -> None:
    quiet_s = float(st.cget("sentinel_quiet_min")) * 60
    now = time.time()
    last_upd = obs.LAST_UPDATE_TS[0] or st.STATS["started"]
    if now - last_upd < quiet_s:
        return
    if now - _state["canary_ok_ts"] > 600:   # канарейка тоже молчит = сеть
        return
    if now - _state["sentinel_ts"] < quiet_s:
        return
    _state["sentinel_ts"] = now
    log(f"CHAN: getUpdates пуст > {quiet_s / 60:.0f} мин при живой канарейке — "
        f"похоже на залипший long-poll, пересоздаю Session", logging.WARNING)
    obs.jlog("sentinel_reset", level="WARNING",
             quiet_min=round((now - last_upd) / 60))
    try:
        import bot_tg as tgm
        tgm.reset_session()
    except Exception:
        log_exc("chan: sentinel reset_session")


def status() -> dict:
    """Сводка для /env и /debug."""
    return {"canary_ok_ago_s": (round(time.time() - _state["canary_ok_ts"])
                                if _state["canary_ok_ts"] else None),
            "canary_ms": _state["canary_ms"],
            "dns_stuck": _state["dns_stuck"],
            "dns_cache": cached_ips(),
            "out_ip": _state["out_ip"] or out_ip(),
            "local_ips": _state["loc_ips"] or local_ips()}


# ---------- минутный тик ----------
def _tick() -> None:
    now = time.time()
    if now - _state["last_canary"] >= float(st.cget("canary_min")) * 60:
        _state["last_canary"] = now
        canary()
        _sentinel()
    if now - _state["last_dns"] >= float(st.cget("dns_check_min")) * 60:
        _state["last_dns"] = now
        dns_check()
    if st.cget("netwatch_enabled"):
        _netwatch()


try:
    import bot_health as _bh
    if _tick not in _bh.MINUTE_TICKS:
        _bh.MINUTE_TICKS.append(_tick)
except Exception:
    log("bot_chan: тик не зарегистрирован", logging.WARNING)
