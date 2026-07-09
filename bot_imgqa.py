# -*- coding: utf-8 -*-
"""Волна G — качество изображения без Pillow (301-308):
301 залипший кадр — MD5 двух кадров с паузой; триггер — совпадение MD5 между
проходами ротации, подтверждение — контрольный кадр через imgqa_verify_delay_s;
302 чёрный/белый кадр — размер JPEG < imgqa_black_ratio от медианы камеры;
303/304 (частично) — Pillow НЕ ставим: гистограмм/энтропии нет, работаем по
размеру и байтам JPEG, помечено «частично»;
305 сравнение с baseline (эталон волны D, bot_dossier) по размеру/байтам —
грубая эвристика без декодирования, ограничения описаны в ответе;
306 раздельные профили день/ночь (метрики snap_kb_day / snap_kb_night);
307 (частично) детект ИК — только косвенно: ночная медиана аномально мала;
308 тренд размера кадра за 30 дней. Фон — ротацией imgqa_batch камер раз в
imgqa_period_min (не шторм). Пароли камер НЕ меняются, только чтение."""
import time
import hashlib
import datetime
from concurrent.futures import ThreadPoolExecutor

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_store as store
import bot_metrics as mx
from bot_tg import send, send_chunks, chat_action
from bot_util import log, log_exc, esc


def _spath():
    return st.cget("imgqa_state_path")


def _is_day(ts: float = None) -> bool:
    h = time.localtime(ts or time.time()).tm_hour
    d0, d1 = (st.cget("imgqa_day_hours") or [8, 20])[:2]
    return d0 <= h < d1


def _snap(ip):
    from onvif_snap import get_snapshot
    return get_snapshot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)


def md5_frozen(a: bytes, b: bytes) -> bool:
    """301: два кадра байт-в-байт идентичны = поток заморожен (чистая ф-я)."""
    return bool(a) and bool(b) and \
        hashlib.md5(a).hexdigest() == hashlib.md5(b).hexdigest()


def is_black(size_b: int, med_b: float, ratio: float = None) -> bool:
    """302: кадр аномально мал против медианы камеры (чистая ф-я)."""
    ratio = ratio if ratio is not None else float(st.cget("imgqa_black_ratio"))
    return bool(med_b) and size_b < med_b * ratio


def baseline_cmp(base: bytes, cur: bytes) -> dict:
    """305: грубое сравнение с эталоном по байтам JPEG (без декодирования):
    размер + доля совпадающего префикса (заголовки/таблицы квантования).
    Ограничение: сжатый поток меняется целиком при любом изменении сцены,
    поэтому это индикатор «что-то сильно не так», а не точная мера."""
    ratio = len(cur) / len(base) if base else 0.0
    n = min(len(base), len(cur), 4096)
    same = 0
    for i in range(n):
        if base[i] == cur[i]:
            same += 1
        else:
            break
    verdict = "похоже на эталон по размеру"
    if md5_frozen(base, cur):
        verdict = "кадр байт-в-байт равен эталону (поток отдаёт статичный кадр?)"
    elif ratio < 0.5 or ratio > 2.0:
        verdict = ("размер сильно расходится с эталоном — возможны сдвиг "
                   "обзора/засветка/расфокус (грубая эвристика)")
    return {"ratio": round(ratio, 2), "prefix": same, "verdict": verdict}


def size_trend(vals) -> float:
    """308: изменение медианы размера, % (последняя треть vs первая треть)."""
    vals = [v for v in vals if v]
    if len(vals) < 6:
        return 0.0
    third = max(2, len(vals) // 3)
    import statistics
    a = statistics.median(vals[:third])
    b = statistics.median(vals[-third:])
    return round(100.0 * (b - a) / a, 1) if a else 0.0


# ---------- фоновая ротация ----------
def _check_cam(ip: str) -> None:
    data, _msg = _snap(ip)
    now = time.time()
    if not data:
        # 320 (часть): камера онлайн по TCP, а снимок мёртв — событие
        mx.event_add(ip, "snapdead", cooldown_h=24)
        return
    kb = len(data) / 1024.0
    kind = "snap_kb_day" if _is_day(now) else "snap_kb_night"
    mx.metric_add(ip, kind, kb)
    med = mx.med(ip, kind, days=14, min_n=5)
    if med and is_black(len(data), med * 1024):
        if mx.event_add(ip, "black", f"{kb:.0f}КБ при медиане {med:.0f}КБ"):
            mx.owner_alert(f"🖤 <b>Подозрение на чёрный/белый кадр</b>: "
                           f"{esc(inv.label(ip) or ip)}\n"
                           f"кадр {kb:.0f} КБ при медиане {med:.0f} КБ "
                           f"(объектив закрыт/матрица/засветка?) · /imgqa {ip}")
    st_all = store.jload(_spath(), {})
    prev = st_all.get(ip) or {}
    cur_md5 = hashlib.md5(data).hexdigest()
    if prev.get("md5") == cur_md5:
        # подозрение на заморозку — контрольный кадр через паузу (301)
        time.sleep(float(st.cget("imgqa_verify_delay_s")))
        data2, _m2 = _snap(ip)
        if data2 is not None and md5_frozen(data, data2):
            if mx.event_add(ip, "frozen", "MD5 кадров идентичен"):
                mx.owner_alert(f"🧊 <b>Кадр залип</b>: {esc(inv.label(ip) or ip)}\n"
                               f"MD5 снимков идентичен (энкодер завис при "
                               f"живом ONVIF) · /imgqa {ip}")
    def _fn(d):
        d[ip] = {"md5": cur_md5, "ts": int(now), "kb": round(kb, 1)}
        if len(d) > 1500:  # не распухать
            for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:200]:
                d.pop(k, None)
        return d
    store.jupdate(_spath(), {}, _fn)


def _tick() -> None:
    if not st.cget("imgqa_enabled"):
        return
    if not mx.due("imgqa", float(st.cget("imgqa_period_min")),
                  first_delay_s=240):
        return
    batch = mx.rotation_batch("imgqa", int(st.cget("imgqa_batch")))
    if not batch:
        return
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="imgqa") as ex:
        list(ex.map(_check_cam, batch))
    log(f"imgqa: ротация {len(batch)} камер за {time.time() - t0:.1f}s")


# ---------- /imgqa ----------
def _resolve(arg):
    a = (arg or "").strip()
    if net.valid_ip(a):
        return a
    return inv.resolve_ip(a) if a else None


def cmd_imgqa(chat, arg="", reply_to=None):
    ip = _resolve(arg)
    if not ip:
        _summary(chat, reply_to)
        return
    chat_action(chat)
    send(chat, f"🔬 Проверяю кадр <code>{ip}</code> (два снимка с паузой)…",
         reply_to=reply_to, silent=True)
    data1, msg = _snap(ip)
    if not data1:
        send(chat, f"❌ Снимок с <code>{ip}</code> не получился ({esc(msg)}).")
        return
    time.sleep(float(st.cget("imgqa_verify_delay_s")))
    data2, _m = _snap(ip)
    lines = [f"🔬 <b>Качество кадра {esc(inv.label(ip) or ip)}</b>"]
    frozen = data2 is not None and md5_frozen(data1, data2)
    lines.append(("🧊 <b>Кадр ЗАЛИП</b> — MD5 двух снимков идентичен"
                  if frozen else "✅ кадры различаются (поток живой)") +
                 f" · {len(data1) // 1024} КБ")
    for kind, nm in (("snap_kb_day", "день"), ("snap_kb_night", "ночь")):
        m = mx.med(ip, kind, days=14, min_n=3)
        if m:
            lines.append(f"📊 медиана {nm}: {m:.0f} КБ" +
                         (" · сейчас < порога чёрного кадра ⚠️"
                          if is_black(len(data1), m * 1024) and
                          _is_day() == (kind == "snap_kb_day") else ""))
    md, mn = (mx.med(ip, "snap_kb_day", 14, 3),
              mx.med(ip, "snap_kb_night", 14, 3))
    if md and mn and mn < md * 0.25:
        lines.append("🌙 ночная медиана < 25% дневной — возможно, не работает "
                     "ИК-подсветка (307, косвенно: без Pillow только размер)")
    vals = [v for _t, v in mx.series(ip, "snap_kb_day", 30)] or \
           [v for _t, v in mx.series(ip, "snap_kb_night", 30)]
    tr = size_trend(vals)
    if vals:
        lines.append(f"📈 тренд размера за 30 дн.: {tr:+.1f}%"
                     + (" — деградация детализации (грязь/паутина?)"
                        if tr < -30 else ""))
    try:
        import bot_dossier
        if bot_dossier.has_baseline(ip):
            with open(bot_dossier._baseline_path(ip), "rb") as f:
                base = f.read()
            c = baseline_cmp(base, data1)
            lines.append(f"⚖️ vs эталон: размер ×{c['ratio']} — {c['verdict']}")
            lines.append("(305: сравнение по байтам JPEG, без декодирования — "
                         "грубая эвристика; визуально: кнопка «⚖️ С эталоном»)")
        else:
            lines.append("⚖️ эталона нет — сохрани /baseline")
    except Exception:
        log_exc("imgqa: baseline_cmp")
    ev = mx.events(ip=ip, days=7)
    if ev:
        lines.append("🗂 события 7 дн.: " + ", ".join(
            f"{e['kind']} {time.strftime('%d.%m %H:%M', time.localtime(e['ts']))}"
            for e in ev[-6:]))
    send_chunks(chat, lines)


def _summary(chat, reply_to=None):
    cnt = mx.event_counts(7)
    keys = ("frozen", "black", "snapdead")
    lines = ["🔬 <b>Качество кадров парка</b> (события за 7 дн.):",
             " · ".join(f"{k}: {cnt.get(k, 0)}" for k in keys)]
    for k, nm in (("frozen", "🧊 залипшие"), ("black", "🖤 чёрные/белые"),
                  ("snapdead", "📵 онлайн, но снимок мёртв")):
        evs = [e for e in mx.events(kind=k, days=7)]
        if evs:
            ips = sorted({e["ip"] for e in evs})
            lines.append(f"{nm}: " + ", ".join(
                f"<code>{i}</code>" for i in ips[:12])
                + (f" … и ещё {len(ips) - 12}" if len(ips) > 12 else ""))
    lines.append(f"Фоновая ротация: {st.cget('imgqa_batch')} камер / "
                 f"{st.cget('imgqa_period_min')} мин "
                 f"({'вкл' if st.cget('imgqa_enabled') else 'выкл'}). "
                 f"Точечно: /imgqa &lt;ip|имя&gt;")
    lines.append("303/304 (гистограммы/энтропия) — частично: Pillow не "
                 "ставим, работаем по размеру и байтам JPEG.")
    send_chunks(chat, lines)


try:
    import bot_health as _bh
    _bh.MINUTE_TICKS.append(_tick)
except Exception:
    pass

HANDLERS = {"/imgqa": cmd_imgqa}
ALIASES = {"/кадр": "/imgqa"}
CALLBACKS: dict = {}
