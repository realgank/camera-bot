# -*- coding: utf-8 -*-
"""Волна G — часы камер (309-312) + латентность ONVIF (321):
309 дрейф часов: GetSystemDateAndTime vs часы ПК, |смещение| > clock_drift_warn_s
    -> сводный алерт со списком;
310 /clock_report — сводка по парку: смещение, timezone, DST, NTP/Manual;
311 детект отсутствия NTP: DateTimeType=Manual -> кандидаты на настройку;
312 «часы прыгнули»: скачок смещения > clock_jump_s между опросами = ребут/
    умершая RTC-батарейка; 321 время ответа ONVIF -> метрика onvif_ms.
335 (канарейка кредов): ответ 401/NotAuthorized -> критический алерт.
Фон — ротацией clock_batch камер раз в clock_period_min. Read-only."""
import time
from concurrent.futures import ThreadPoolExecutor

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_store as store
import bot_metrics as mx
import bot_onvifq as q
from bot_tg import send, send_chunks, chat_action
from bot_util import log, esc


def _spath():
    return st.cget("camtime_state_path")


def is_jump(prev_off, cur_off, jump_s: float = None) -> bool:
    """312: скачок смещения между опросами (чистая функция)."""
    if prev_off is None or cur_off is None:
        return False
    jump_s = jump_s if jump_s is not None else float(st.cget("clock_jump_s"))
    return abs(cur_off - prev_off) > jump_s


def _fmt_off(off) -> str:
    if off is None:
        return "?"
    if abs(off) < 60:
        return f"{off:+.0f} c"
    if abs(off) < 3600:
        return f"{off / 60:+.0f} мин"
    return f"{off / 3600:+.1f} ч"


def _poll_one(ip: str) -> dict:
    r = q.get_datetime(ip)
    now = int(time.time())
    if r.get("auth"):
        # 335: канарейка кредов — 401 это НЕ офлайн, это «кто-то сменил пароль»
        if mx.event_add(ip, "auth401", cooldown_h=24):
            mx.owner_alert(f"🚨 <b>КРИТИЧНО: не подошли креды Admin/1234</b> "
                           f"на {esc(inv.label(ip) or ip)} — возможно, "
                           f"кто-то сменил пароль камеры! (335)")
        return {}
    if "offset_s" not in r:
        return {}
    mx.metric_add(ip, "onvif_ms", r["ms"])          # 321
    mx.metric_add(ip, "clock_off", r["offset_s"])   # 309
    prev = (store.jload(_spath(), {}).get(ip) or {})
    if is_jump(prev.get("off"), r["offset_s"]):
        if mx.event_add(ip, "clockjump",
                        f"{_fmt_off(prev.get('off'))} -> "
                        f"{_fmt_off(r['offset_s'])}"):
            mx.owner_alert(f"⏰ <b>Часы прыгнули</b>: {esc(inv.label(ip) or ip)}"
                           f" — было {_fmt_off(prev.get('off'))}, стало "
                           f"{_fmt_off(r['offset_s'])} (ребут камеры / RTC?)")
    rec = {"off": r["offset_s"], "ts": now, "type": r.get("type") or "?",
           "tz": r.get("tz") or "?", "dst": r.get("dst"),
           "year": r.get("year"), "ms": r.get("ms")}
    def _fn(d):
        d[ip] = rec
        return d
    store.jupdate(_spath(), {}, _fn)
    return rec


def _tick() -> None:
    if not st.cget("clock_enabled"):
        return
    if not mx.due("clock", float(st.cget("clock_period_min")),
                  first_delay_s=300):
        return
    batch = mx.rotation_batch("clock", int(st.cget("clock_batch")))
    if not batch:
        return
    t0 = time.time()
    drifted = []
    warn = float(st.cget("clock_drift_warn_s"))
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="clock") as ex:
        for ip, rec in zip(batch, ex.map(_poll_one, batch)):
            if rec and abs(rec.get("off") or 0) > warn:
                if mx.event_add(ip, "drift", _fmt_off(rec["off"]),
                                cooldown_h=7 * 24):
                    drifted.append((ip, rec["off"]))
    if drifted:  # 309: сводный алерт, не по одному
        lines = [f"⏰ <b>Дрейф часов &gt; {warn:.0f}с</b> (таймстампы архива "
                 f"врут):"]
        lines += [f"• {esc(inv.label(i) or i)}: {_fmt_off(o)}"
                  for i, o in drifted[:10]]
        lines.append("Подробно: /clock_report")
        mx.owner_alert("\n".join(lines))
    log(f"clock: ротация {len(batch)} камер за {time.time() - t0:.1f}s")


# ---------- 310: /clock_report ----------
def cmd_clock_report(chat, arg="", reply_to=None):
    d = store.jload(_spath(), {})
    if not d:
        send(chat, "⏰ Часы ещё не опрашивались — ротация "
                   f"{st.cget('clock_batch')} камер раз в "
                   f"{st.cget('clock_period_min')} мин запустится в фоне.\n"
                   "Точечно: <code>/clock 10.20.50.51</code>",
             reply_to=reply_to)
        return
    warn = float(st.cget("clock_drift_warn_s"))
    bad = sorted(((ip, r) for ip, r in d.items()
                  if abs(r.get("off") or 0) > warn),
                 key=lambda kv: -abs(kv[1].get("off") or 0))
    manual = [ip for ip, r in d.items()
              if (r.get("type") or "").lower() != "ntp"]
    import collections
    tzs = collections.Counter((r.get("tz") or "?") for r in d.values())
    jumps = mx.events(kind="clockjump", days=7)
    lines = [f"⏰ <b>Часы парка</b>: опрошено {len(d)} камер",
             f"дрейф &gt; {warn:.0f}с: <b>{len(bad)}</b> · не-NTP (Manual): "
             f"<b>{len(manual)}</b> · «прыжков» за 7 дн.: {len(jumps)}"]
    if bad:
        lines.append("\n🔺 <b>Худшие смещения</b>:")
        lines += [f"• {esc(inv.label(ip) or ip)}: {_fmt_off(r.get('off'))} "
                  f"({esc(r.get('type') or '?')}, tz {esc(r.get('tz') or '?')})"
                  for ip, r in bad[:15]]
        if len(bad) > 15:
            lines.append(f"… и ещё {len(bad) - 15}")
    if manual:
        lines.append(f"\n🕰 <b>Без NTP</b> (311, уплывут обязательно): "
                     + ", ".join(f"<code>{i}</code>" for i in manual[:12])
                     + (f" … и ещё {len(manual) - 12}"
                        if len(manual) > 12 else ""))
    lines.append("\n🌍 timezone: " + " · ".join(
        f"{esc(tz)}: {n}" for tz, n in tzs.most_common(5)))
    old_year = [ip for ip, r in d.items() if (r.get("year") or 9999) < 2020]
    if old_year:
        lines.append("📟 год < 2020 (сброс/RTC): " + ", ".join(
            f"<code>{i}</code>" for i in old_year[:10]))
    send_chunks(chat, lines)


def cmd_clock(chat, arg="", reply_to=None):
    """Точечный живой опрос часов одной камеры."""
    a = (arg or "").strip()
    ip = a if net.valid_ip(a) else (inv.resolve_ip(a) if a else None)
    if not ip:
        send(chat, "Часы камеры: <code>/clock 10.20.50.51</code>",
             reply_to=reply_to)
        return
    chat_action(chat)
    r = q.get_datetime(ip)
    if r.get("auth"):
        send(chat, f"🚨 <code>{ip}</code>: креды не подошли (401) — канарейка!")
        return
    if "offset_s" not in r:
        send(chat, f"❌ <code>{ip}</code>: {esc(r.get('error') or '?')}",
             reply_to=reply_to)
        return
    _poll_one(ip)  # заодно в состояние/метрики
    send(chat, f"⏰ <b>{esc(inv.label(ip) or ip)}</b>\n"
               f"смещение: <b>{_fmt_off(r['offset_s'])}</b> · "
               f"источник: {esc(r.get('type') or '?')} · "
               f"tz {esc(r.get('tz') or '?')} · DST {esc(r.get('dst') or '?')}\n"
               f"ONVIF ответил за {r['ms']:.0f} мс", reply_to=reply_to)


try:
    import bot_health as _bh
    _bh.MINUTE_TICKS.append(_tick)
except Exception:
    pass

HANDLERS = {"/clock_report": cmd_clock_report, "/clock": cmd_clock}
ALIASES = {"/часы": "/clock_report"}
CALLBACKS: dict = {}
