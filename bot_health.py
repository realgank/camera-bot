# -*- coding: utf-8 -*-
"""Фоновый health-check парка камер (Волна C): TCP-проба 80/554 всех IP
инвентаря раз в N минут, состояние _health_state.json, история переходов
_health_history.json (кольцо 30 дней), падение подтверждается
health_confirm_probes пробами подряд внутри прогона, алерты владельцу
(падение/восстановление, группировка массовых), проба заводского 192.168.0.250,
ежедневный отчёт, /watch-список.
I14-I18, I21-I24 (расчёты), I40, U30. Первый прогон — через 2 мин после старта
(тихий рестарт); алерты о падениях — только со ВТОРОГО прогона (нужна база).
Пароли камер бот НИКОГДА не меняет; пробы — только TCP-connect (read-only).
"""
import os
import json
import time
import datetime
import threading
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_tg as tgm
from bot_util import log, log_exc, esc

_lock = threading.RLock()
_state: dict = {"ips": {}, "last_run": None, "runs": 0, "factory_ok": False,
                "last_daily": ""}
_loaded = [False]
RUNS_IN_PROC = [0]   # прогонов в ЭТОМ процессе: алерты только начиная со 2-го
RUN_LOCK = threading.Lock()   # single-flight run_once
# Волна D: минутные тики фонового цикла (напоминания 197, снузы 161,
# эскалация 163, тихие часы 162, ППР 189). Модули регистрируются при импорте.
MINUTE_TICKS: list = []
# M1: тики бегут в своих воркер-потоках, чтобы зависший тик (автосинк с
# Google-таймаутами, recon-скан подсетей) не замораживал health-прогоны.
_TICK_THREADS: Dict[str, threading.Thread] = {}   # имя тика -> живой поток


def _tick_name(fn) -> str:
    return (f"{getattr(fn, '__module__', '?')}."
            f"{getattr(fn, '__name__', '?')}")


def _tick_worker(fn, name: str) -> None:
    try:
        fn()
    except Exception:
        log_exc(f"health: тик {name} упал")


def _run_ticks() -> None:
    """Каждый тик — в отдельном daemon-потоке; health-цикл не ждёт их.
    Single-flight: если прошлый запуск тика ещё жив — новый не стартуем."""
    for fn in list(MINUTE_TICKS):
        name = _tick_name(fn)
        prev = _TICK_THREADS.get(name)
        if prev is not None and prev.is_alive():
            log(f"health: тик {name} ещё выполняется — пропускаю запуск")
            continue
        t = threading.Thread(target=_tick_worker, args=(fn, name),
                             name=f"tick:{name}", daemon=True)
        _TICK_THREADS[name] = t
        t.start()


def _spath() -> str:
    return st.cget("health_state_path")


def _hpath() -> str:
    return st.cget("health_history_path")


def _load_state() -> None:
    if _loaded[0]:
        return
    try:
        with open(_spath(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("ips"), dict):
            _state.update(data)
    except Exception:
        pass
    _loaded[0] = True


def _save_state() -> None:
    try:
        tmp = _spath() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False)
        os.replace(tmp, _spath())
    except Exception:
        log_exc("health: не смог сохранить _health_state.json")


def history_events(days: Optional[float] = None) -> List[dict]:
    """I21: события истории [{ts, ip, ev: down|up, dur}], свежие в конце."""
    try:
        with open(_hpath(), encoding="utf-8") as f:
            ev = json.load(f)
    except Exception:
        return []
    if not isinstance(ev, list):
        return []
    if days:
        cut = time.time() - days * 86400
        ev = [e for e in ev if e.get("ts", 0) >= cut]
    return ev


def _append_history(events: List[dict]) -> None:
    if not events:
        return
    ev = history_events()
    ev.extend(events)
    cut = time.time() - st.cget("health_history_days") * 86400
    ev = [e for e in ev if e.get("ts", 0) >= cut]
    try:
        tmp = _hpath() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False)
        os.replace(tmp, _hpath())
    except Exception:
        log_exc("health: не смог сохранить _health_history.json")


def target_ips() -> List[str]:
    """IP для проб: инвентарь (опц. фильтр health_subnets) + /watch-список."""
    subs = [s.rstrip(".") for s in st.cget("health_subnets") or []]
    ips = []
    try:  # Волна D (196): демонтированные/плановые — вне health-check
        from bot_lifecycle import is_monitored
    except Exception:
        is_monitored = None
    for c in inv.cams():
        ip = c.get("ip")
        if not ip or not net.valid_ip(ip):
            continue
        if subs and not any(ip.startswith(s + ".") for s in subs):
            continue
        if is_monitored and not is_monitored(ip):
            continue
        ips.append(ip)
    for ip in st.get_ips("watch_ips"):
        if net.valid_ip(ip) and ip not in ips:
            ips.append(ip)
    return ips


def probe(ip: str) -> bool:
    return net.tcp_alive(ip, ports=tuple(st.cget("health_ports")),
                         t=float(st.cget("health_tcp_timeout_s")))


def _alert(text: str, markup: Optional[dict] = None, silent: bool = False,
           aid: str = None) -> None:
    if aid:  # /alerts: выключенные фоновые алерты не шлём
        try:
            import bot_alerts
            if bot_alerts.muted(aid):
                return
        except Exception:
            pass
    owner = st.cget("owner_chat_id")
    if owner:
        tgm.send(owner, text, markup=markup, silent=silent)


def _ip_kb(ip: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "🩺 Диаг", "callback_data": f"diag:{ip}"},
        {"text": "📸 Снимок", "callback_data": f"shot:{ip}"}]]}


def _label(ip: str) -> str:
    lbl = inv.label(ip)
    return f"{ip} — {lbl}" if lbl else ip


def _fmt_dur(sec: float) -> str:
    sec = int(sec)
    if sec < 3600:
        return f"{sec // 60} мин"
    if sec < 86400:
        return f"{sec // 3600} ч {(sec % 3600) // 60} мин"
    return f"{sec // 86400} д {(sec % 86400) // 3600} ч"


def _apply_probe(ip: str, ok: bool, now: float) -> Optional[dict]:
    """Обновляет запись состояния; возвращает событие перехода или None.
    Дебаунс I17: офлайн после health_fail_threshold провалов подряд
    (для /watch-камер — с первого, U30)."""
    watched = ip in st.get_ips("watch_ips")
    thr = 1 if watched else int(st.cget("health_fail_threshold"))
    cur = _state["ips"].get(ip) or {"ok": None, "fails": 0, "since": now}
    ev = None
    if ok:
        if cur.get("ok") is False:
            ev = {"ts": int(now), "ip": ip, "ev": "up",
                  "dur": int(now - cur.get("since", now))}
            cur["since"] = now
        elif cur.get("ok") is None:
            cur["since"] = now
        cur["ok"] = True
        cur["fails"] = 0
    else:
        cur["fails"] = cur.get("fails", 0) + 1
        if cur.get("ok") is not False and cur["fails"] >= thr:
            # None -> False (первичная база) — событие только если знали, что жила
            if cur.get("ok") is True:
                ev = {"ts": int(now), "ip": ip, "ev": "down"}
            cur["ok"] = False
            cur["since"] = now
    cur["checked"] = int(now)
    _state["ips"][ip] = cur
    return ev


def _confirm_downs(results: List[Tuple[str, bool]]) -> List[Tuple[str, bool]]:
    """Кандидаты в «упала» (были живы, проба провалилась) перепроверяются
    ещё health_confirm_probes-1 раз с паузой — отсекаем разовые TCP-блипы;
    алерт уходит только после серии подтверждённых провалов."""
    probes = int(st.cget("health_confirm_probes"))
    if probes <= 1:
        return results
    with _lock:
        was_ok = {ip for ip, e in _state["ips"].items() if e.get("ok") is True}
    res = dict(results)
    cand = [ip for ip, ok in results if not ok and ip in was_ok]
    for _ in range(probes - 1):
        if not cand:
            break
        time.sleep(float(st.cget("health_confirm_delay_s")))
        with ThreadPoolExecutor(max_workers=int(st.cget("health_workers")),
                                thread_name_prefix="health") as ex:
            recheck = list(zip(cand, ex.map(probe, cand)))
        for ip, ok in recheck:
            if ok:
                res[ip] = True
        cand = [ip for ip, ok in recheck if not ok]
    return [(ip, res[ip]) for ip, _ in results]


def _alert_downs(downs: List[str]) -> None:
    """I15 + I18: одиночные алерты либо группировка по подсети/свитчу.
    Волна D: перед алертами — фильтр bot_ops (159 maint / 161 snooze /
    196 lifecycle), одиночные в тихие часы копятся в дайджест (162)."""
    kb_fn = _ip_kb
    try:
        import bot_ops
        downs = bot_ops.on_downs(downs)      # maint/snooze/in_repair + журнал 164
        kb_fn = bot_ops.alert_kb             # кнопки «Взял/Починил/Ремонт до…»
    except Exception:
        log_exc("health: bot_ops.on_downs")
    mass_thr = int(st.cget("health_mass_threshold"))
    by_sub: Dict[str, List[str]] = {}
    for ip in downs:
        by_sub.setdefault(ip.rsplit(".", 1)[0], []).append(ip)
    singles: List[str] = []
    for sub, ips in sorted(by_sub.items()):
        if len(ips) >= mass_thr:
            sws = {str((inv.get(i) or {}).get("switch") or "?") for i in ips}
            sw_s = (f" · свитч {esc(sws.pop())}" if len(sws) == 1
                    else f" · свитчи: {esc(', '.join(sorted(sws)[:4]))}")
            lines = [f"🔴 <b>Массовое падение в {esc(sub)}.x</b>: "
                     f"{len(ips)} камер{sw_s}"]
            lines += [f"• {esc(_label(i))}" for i in ips[:12]]
            if len(ips) > 12:
                lines.append(f"… и ещё {len(ips) - 12}")
            lines.append("Похоже на свитч/линк сегмента, а не камеры.")
            try:  # Волна H (388): root-cause — жив ли свитч/аплинк/PoE
                import bot_sw_mon
                lines += bot_sw_mon.mass_context(ips)
            except Exception:
                log_exc("health: bot_sw_mon.mass_context")
            _alert("\n".join(lines), aid="cam_mass")
        else:
            singles.extend(ips)
    try:  # 162: тихие часы — некритичные одиночные копим до утра
        import bot_ops
        singles = bot_ops.quiet_filter(singles)
    except Exception:
        log_exc("health: bot_ops.quiet_filter")
    for ip in singles[:int(st.cget("health_alerts_max"))]:
        rec = inv.get(ip) or {}
        loc = " · ".join(str(rec[k]) for k in ("location", "obj") if rec.get(k))
        sw = (f"\n🔌 {esc(rec.get('switch') or '?')} п.{esc(rec.get('port') or '?')}"
              if rec.get("switch") or rec.get("port") else "")
        _alert(f"🔴 <b>Камера упала</b>: {esc(_label(ip))}"
               + (f"\n📍 {esc(loc)}" if loc else "") + sw,
               markup=kb_fn(ip), aid="cam_down")
    if len(singles) > int(st.cget("health_alerts_max")):
        _alert(f"🔴 … и ещё {len(singles) - int(st.cget('health_alerts_max'))} "
               f"падений — смотри /offline", aid="cam_down")


def _alert_ups(ups: List[dict]) -> None:
    """I16: восстановление («лежала 47 мин») + автоснимок (первые 2)."""
    for i, ev in enumerate(ups[:int(st.cget("health_alerts_max"))]):
        ip = ev["ip"]
        _alert(f"🟢 <b>Камера ожила</b>: {esc(_label(ip))}\n"
               f"⏳ лежала {_fmt_dur(ev.get('dur', 0))}", markup=_ip_kb(ip),
               aid="cam_up")
        if i < 2:  # автоснимок best-effort, не задерживаем прогон надолго
            try:
                from onvif_snap import get_snapshot
                data, _m = get_snapshot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
                if data:
                    tgm.send_photo(st.cget("owner_chat_id"), data,
                                   caption=f"🟢 {ip} — снимок после восстановления")
            except Exception:
                log_exc(f"health: автоснимок {ip}")


def _probe_factory(alerts: bool) -> None:
    """I40: заводская камера 192.168.0.250 появилась в сети — алерт с кнопкой."""
    if not st.cget("health_factory_probe"):
        return
    fip = st.cget("health_factory_ip")
    ok = net.tcp_alive(fip, ports=(80,), t=float(st.cget("health_tcp_timeout_s")))
    was = bool(_state.get("factory_ok"))
    _state["factory_ok"] = ok
    if ok and not was and alerts:
        _alert(f"🏭 <b>Появилась заводская камера</b> <code>{fip}</code> — "
               f"похоже, подключили новую (все Apix с завода на этом IP).",
               markup=_ip_kb(fip), aid="factory_appeared")


def run_once(alerts: bool = True) -> dict:
    """Один прогон проб. Возвращает {'total','online','offline','downs','ups'}."""
    if not RUN_LOCK.acquire(blocking=False):
        return {}
    try:
        with _lock:
            _load_state()
        ips = target_ips()
        t0 = time.time()
        results: List[Tuple[str, bool]] = []
        with ThreadPoolExecutor(max_workers=int(st.cget("health_workers")),
                                thread_name_prefix="health") as ex:
            for ip, ok in zip(ips, ex.map(probe, ips)):
                results.append((ip, ok))
        results = _confirm_downs(results)
        now = time.time()
        downs, ups = [], []
        with _lock:
            for ip, ok in results:
                ev = _apply_probe(ip, ok, now)
                if ev and ev["ev"] == "down":
                    downs.append(ip)
                elif ev:
                    ups.append(ev)
            RUNS_IN_PROC[0] += 1
            _state["runs"] = int(_state.get("runs") or 0) + 1
            _state["last_run"] = int(now)
            do_alerts = alerts and RUNS_IN_PROC[0] >= 2
            _probe_factory(alerts=do_alerts)
            _append_history([{"ts": e["ts"], "ip": e["ip"], "ev": e["ev"],
                              **({"dur": e["dur"]} if "dur" in e else {})}
                             for e in
                             ([{"ts": int(now), "ip": i, "ev": "down"} for i in downs]
                              + ups)])
            _save_state()
        try:  # Волна G (347): пробы/переходы -> SQLite _metrics.db
            import bot_metrics
            bot_metrics.on_run(results, downs, ups, now)
        except Exception:
            log_exc("health: bot_metrics.on_run")
        online = sum(1 for _ip, ok in results if ok)
        log(f"health: прогон #{RUNS_IN_PROC[0]}: {online}/{len(results)} онлайн, "
            f"падений {len(downs)}, восстановлений {len(ups)}, "
            f"{time.time() - t0:.1f}s")
        if do_alerts:
            _alert_downs(downs)
            _alert_ups(ups)
        return {"total": len(results), "online": online,
                "offline": len(results) - online, "downs": downs, "ups": ups}
    finally:
        RUN_LOCK.release()


def snapshot() -> dict:
    """Копия состояния для хендлеров и /sync (I28)."""
    with _lock:
        _load_state()
        return {"ips": {k: dict(v) for k, v in _state["ips"].items()},
                "last_run": _state.get("last_run"),
                "runs": _state.get("runs"), "factory_ok": _state.get("factory_ok")}


def offline_ips() -> List[str]:
    snap = snapshot()
    return sorted((ip for ip, e in snap["ips"].items() if e.get("ok") is False),
                  key=lambda x: tuple(int(o) for o in x.split(".")))


def report_text() -> str:
    """I20/I24: сводка парка по подсетям из последнего состояния."""
    snap = snapshot()
    if not snap["ips"]:
        return ("💓 Health-check ещё не выполнялся — первый прогон через "
                f"{st.cget('health_first_delay_s') // 60} мин после старта бота.")
    by_sub: Dict[str, List[bool]] = {}
    for ip, e in snap["ips"].items():
        by_sub.setdefault(ip.rsplit(".", 1)[0], []).append(bool(e.get("ok")))
    total = sum(len(v) for v in by_sub.values())
    online = sum(sum(v) for v in by_sub.values())
    lines = [f"💓 <b>Сводка парка</b>: онлайн <b>{online}/{total}</b>"]
    for sub, oks in sorted(by_sub.items()):
        n_on, n = sum(oks), len(oks)
        mark = "✅" if n_on == n else ("⚠️" if n_on >= n * 0.9 else "🔴")
        lines.append(f"{mark} <code>{sub}.x</code>: {n_on}/{n}"
                     + (f" (офлайн {n - n_on})" if n_on < n else ""))
    if snap.get("factory_ok"):
        lines.append(f"🏭 заводская <code>{st.cget('health_factory_ip')}</code> "
                     f"СЕЙЧАС в сети!")
    if snap.get("last_run"):
        lines.append("🕒 прогон: "
                     + time.strftime("%d.%m %H:%M", time.localtime(snap["last_run"]))
                     + f" · интервал {st.cget('health_interval_min')} мин")
    down7 = len([e for e in history_events(7) if e.get("ev") == "down"])
    lines.append(f"📉 падений за 7 дней: {down7} · /offline /top_flaky /uptime")
    return "\n".join(lines)


def uptime(ip: str, days: float = 7) -> Tuple[float, int, int]:
    """I22: (процент доступности, падений, суммарный даунтайм сек) за days."""
    win = days * 86400
    now = time.time()
    downs, down_s = 0, 0
    for e in history_events(days):
        if e.get("ip") != ip:
            continue
        if e.get("ev") == "down":
            downs += 1
        elif e.get("ev") == "up":
            down_s += min(int(e.get("dur") or 0), int(win))
    cur = snapshot()["ips"].get(ip)
    if cur and cur.get("ok") is False:
        down_s += int(min(now - cur.get("since", now), win))
    pct = max(0.0, 100.0 * (1 - down_s / win))
    return pct, downs, down_s


def top_flaky(days: float = 7, n: int = 10) -> List[Tuple[str, int]]:
    """I23: топ нестабильных — по числу падений за days."""
    cnt: Dict[str, int] = {}
    for e in history_events(days):
        if e.get("ev") == "down":
            cnt[e["ip"]] = cnt.get(e["ip"], 0) + 1
    return sorted(cnt.items(), key=lambda kv: -kv[1])[:n]


def _maybe_daily() -> None:
    """I24: ежедневный отчёт в заданный час (раз в день)."""
    today = datetime.date.today().isoformat()
    if (datetime.datetime.now().hour >= int(st.cget("health_daily_hour"))
            and _state.get("last_daily") != today):
        with _lock:
            _state["last_daily"] = today
            _save_state()
        _alert("🗓 <b>Ежедневный отчёт</b>\n" + report_text(), silent=True,
               aid="daily_report")


def run_loop(stop_event: threading.Event) -> None:
    """Фоновый поток: первый прогон через health_first_delay_s, далее раз в
    health_interval_min. Волна D: между прогонами — минутные тики
    (напоминания, снузы, эскалация, тихие часы, ППР). Останов по stop_event."""
    if stop_event.wait(float(st.cget("health_first_delay_s"))):
        return
    log("health: фоновый цикл запущен")
    next_run = 0.0
    while not stop_event.is_set():
        if time.time() >= next_run:
            try:
                run_once(alerts=True)
                _maybe_daily()
            except Exception:
                log_exc("health: прогон упал")
            next_run = time.time() + float(st.cget("health_interval_min")) * 60
        _run_ticks()
        if stop_event.wait(60):
            return
