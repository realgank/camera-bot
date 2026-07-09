# -*- coding: utf-8 -*-
"""Волна D — алерт-дисциплина: 159 /maint (окно плановых работ, подавление
алертов), 160 автопротокол по /maint end, 162 тихие часы (ночью некритичное
копится в утренний дайджест; массовые пробиваются всегда), 163 эскалация
висящих офлайнов. Фильтры вызываются из bot_health._alert_downs; минутный тик
регистрируется в bot_health.MINUTE_TICKS. Пароли камер НЕ меняются."""
import time
import datetime

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_issues as iss
import bot_store as store
from bot_tg import send, send_chunks
from bot_util import log, log_exc, esc

_DEF = {"seq": 0, "active": [], "log": []}


def _mpath():
    return st.cget("maint_path")


def _owner_send(text, markup=None, silent=False):
    owner = st.cget("owner_chat_id")
    if owner:
        send(owner, text, markup=markup, silent=silent)


# ---------- 159: окна работ ----------
def _resolve_target(a: str) -> list:
    """Зона | корпус | IP/имя свитча | IP камеры → список IP камер."""
    import bot_zones as bz
    _what, recs = bz.cams_by_arg(a)
    if recs:
        return [r["ip"] for r in recs if r.get("ip")]
    if net.valid_ip(a):
        by_sw = [r["ip"] for r in inv.cams()
                 if str(r.get("sw_ip") or "").strip() == a and r.get("ip")]
        return by_sw or ([a] if inv.get(a) else [a])
    nn = inv.norm_name(a)
    return [r["ip"] for r in inv.cams()
            if nn and nn in inv.norm_name(r.get("switch")) and r.get("ip")]


def active_windows() -> list:
    now = time.time()
    return [w for w in store.jload(_mpath(), _DEF)["active"]
            if w.get("until", 0) > now]


def maint_ips() -> set:
    ips = set()
    for w in active_windows():
        ips.update(w.get("ips") or [])
    return ips


def status_line() -> str:
    """Строка для /status: активные окна работ."""
    ws = active_windows()
    if not ws:
        return ""
    return "🚧 работы: " + "; ".join(
        f"{w['target']} до {time.strftime('%H:%M', time.localtime(w['until']))}"
        for w in ws)


def cmd_maint(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    if parts and parts[0].lower() in ("end", "стоп", "конец"):
        wid = None
        if len(parts) > 1 and parts[1].lstrip("#").isdigit():
            wid = int(parts[1].lstrip("#"))
        _end_windows(chat, wid, reason="по команде /maint end")
        return
    if len(parts) >= 2:
        try:
            hours = float(parts[-1].replace(",", "."))
        except ValueError:
            send(chat, "Окно работ: <code>/maint атриум 2</code> "
                       "(зона|свитч|IP и часы)", reply_to=reply_to)
            return
        target = " ".join(parts[:-1])
        ips = _resolve_target(target)
        if not ips:
            send(chat, f"По «{esc(target)}» камер не нашёл (зона, корпус, "
                       f"свитч или IP).", reply_to=reply_to)
            return
        now = time.time()
        w = {}
        def _fn(d):
            d["seq"] = int(d.get("seq") or 0) + 1
            w.update({"id": d["seq"], "target": target, "ips": ips,
                      "start": int(now), "until": int(now + hours * 3600),
                      "downs": []})
            d["active"].append(w)
            return d
        store.jupdate(_mpath(), _DEF, _fn)
        log(f"maint: окно #{w['id']} {target} ({len(ips)} камер, {hours}ч)")
        send(chat, f"🚧 <b>Окно работ #{w['id']}</b>: {esc(target)} — "
                   f"{len(ips)} камер, до "
                   f"{time.strftime('%H:%M', time.localtime(w['until']))}.\n"
                   f"Алерты по ним подавлены. Завершить: /maint end {w['id']}",
             reply_to=reply_to)
        return
    ws = active_windows()
    if not ws:
        send(chat, "🚧 Активных окон работ нет.\n"
                   "<code>/maint атриум 2</code> — окно на 2 часа "
                   "(зона|корпус|свитч|IP)\n<code>/maint end</code> — завершить",
             reply_to=reply_to)
        return
    lines = ["🚧 <b>Активные окна работ</b>:"]
    for w in ws:
        lines.append(f"#{w['id']} {esc(w['target'])} — {len(w['ips'])} камер, "
                     f"до {time.strftime('%d.%m %H:%M', time.localtime(w['until']))}"
                     f" · падало в окно: {len(w.get('downs') or [])}")
    lines.append("Завершить: /maint end [номер]")
    send(chat, "\n".join(lines), reply_to=reply_to)


def _protocol(w) -> str:
    """160: автопротокол по завершении окна."""
    import bot_health as bh
    now = time.time()
    dur = int(now - w["start"])
    ev = [e for e in bh.history_events(max(1.0, (now - w["start"]) / 86400 + 0.1))
          if e.get("ip") in set(w["ips"]) and e.get("ts", 0) >= w["start"]]
    downs = sorted({e["ip"] for e in ev if e.get("ev") == "down"}
                   | set(w.get("downs") or []))
    hm = bh.snapshot()["ips"]
    still = [ip for ip in downs if hm.get(ip, {}).get("ok") is False]
    lines = [f"📋 <b>Протокол работ #{w['id']}</b> — {esc(w['target'])}",
             "Начало: " + time.strftime("%d.%m.%Y %H:%M",
                                        time.localtime(w["start"])),
             "Конец:  " + time.strftime("%d.%m.%Y %H:%M", time.localtime(now)),
             f"Длительность: {dur // 3600}ч {(dur % 3600) // 60}м · "
             f"камер в окне: {len(w['ips'])}"]
    if downs:
        lines.append(f"Падали в окне ({len(downs)}):")
        lines += [f"• {esc(inv.label(ip) or ip)} (<code>{ip}</code>)"
                  for ip in downs[:20]]
    else:
        lines.append("Падений в окне не зафиксировано.")
    lines.append("✅ Все камеры поднялись." if not still else
                 f"⚠️ НЕ поднялись ({len(still)}): "
                 + ", ".join(f"<code>{i}</code>" for i in still[:15]))
    return "\n".join(lines)


def _end_windows(chat, wid=None, reason=""):
    ended = []
    def _fn(d):
        keep = []
        for w in d["active"]:
            if wid is None or w["id"] == wid:
                ended.append(w)
            else:
                keep.append(w)
        d["active"] = keep
        d["log"] = (d.get("log") or [])[-50:] + [
            {"id": w["id"], "target": w["target"], "start": w["start"],
             "end": int(time.time())} for w in ended]
        return d
    store.jupdate(_mpath(), _DEF, _fn)
    if not ended:
        send(chat, "Активных окон работ нет (или номер не найден).")
        return
    for w in ended:
        log(f"maint: окно #{w['id']} завершено {reason}")
        send_chunks(chat, _protocol(w).split("\n"))


# ---------- фильтры для bot_health ----------
def on_downs(downs: list) -> list:
    """Вызывается из bot_health._alert_downs ДО группировки: убирает камеры
    в окнах работ (159), под snooze (161) и «в ремонте» (196); по остальным
    заводит проблемы в журнале (164)."""
    m_ips = maint_ips()
    try:
        from bot_lifecycle import status_of
    except Exception:
        status_of = None
    out = []
    for ip in downs:
        if ip in m_ips:
            def _fn(d, _ip=ip):
                for w in d["active"]:
                    if _ip in (w.get("ips") or []) and _ip not in w["downs"]:
                        w["downs"].append(_ip)
                return d
            store.jupdate(_mpath(), _DEF, _fn)
            log(f"ops: {ip} упала в окне работ — алерт подавлен")
            continue
        if iss.snoozed(ip):
            log(f"ops: {ip} под snooze — алерт подавлен")
            continue
        if status_of and status_of(ip) == "in_repair":
            log(f"ops: {ip} in_repair — алерт подавлен")
            continue
        iss.open_issue(ip)
        out.append(ip)
    return out


def in_quiet(now: float = None) -> bool:
    """162: сейчас тихие часы?"""
    if not st.cget("quiet_enabled"):
        return False
    qh = st.cget("quiet_hours") or [23, 8]
    h = datetime.datetime.fromtimestamp(now or time.time()).hour
    a, b = int(qh[0]), int(qh[1])
    return (a <= h or h < b) if a > b else (a <= h < b)


def quiet_filter(singles: list) -> list:
    """Одиночные падения ночью копим в дайджест; массовые сюда не попадают."""
    if not singles or not in_quiet():
        return singles
    now = int(time.time())
    def _fn(d):
        d["quiet"] = (d.get("quiet") or []) + [
            {"ip": ip, "ts": now} for ip in singles]
        return d
    store.jupdate(st.cget("issues_path"), iss._DEF, _fn)
    log(f"ops: тихие часы — {len(singles)} алертов в утренний дайджест")
    return []


def _flush_quiet():
    if in_quiet():
        return
    box = [None]
    def _fn(d):
        if not d.get("quiet"):
            return None
        box[0] = list(d["quiet"])
        d["quiet"] = []
        return d
    store.jupdate(st.cget("issues_path"), iss._DEF, _fn)
    q = box[0]
    if not q:
        return
    import bot_health as bh
    hm = bh.snapshot()["ips"]
    lines = [f"🌅 <b>Ночной дайджест</b>: за тихие часы падали {len(q)} камер:"]
    for e in q[:30]:
        ip = e["ip"]
        now_ok = hm.get(ip, {}).get("ok")
        mark = "🟢 уже ожила" if now_ok else "🔴 всё ещё офлайн"
        lines.append(f"• <code>{ip}</code> "
                     f"{esc(inv.label(ip) or '')} — "
                     + time.strftime("%H:%M", time.localtime(e["ts"]))
                     + f" · {mark}")
    lines.append("Детали: /offline · /issues")
    _owner_send("\n".join(lines))


def _tick_snooze():
    """161: срок «в ремонте до …» вышел, а камера всё ещё офлайн."""
    import bot_health as bh
    hm = bh.snapshot()["ips"]
    now = time.time()
    for it in iss.open_issues():
        su = it.get("snooze_until")
        if su and su < now and not it.get("snooze_ping"):
            def _fn(d, _id=it["id"]):
                for x in d["issues"]:
                    if x["id"] == _id:
                        x["snooze_ping"] = True
                        return d
                return None
            store.jupdate(st.cget("issues_path"), iss._DEF, _fn)
            if hm.get(it["ip"], {}).get("ok") is False:
                _owner_send(f"⏰ <b>Срок ремонта вышел</b>, а "
                            f"{esc(inv.label(it['ip']) or it['ip'])} всё ещё "
                            f"офлайн (проблема #{it['id']}).",
                            markup=iss.issue_kb(it["ip"]))


def _tick_escalate():
    """163: офлайн дольше N часов без статуса «в работе» — повторный алерт."""
    import bot_health as bh
    thr = float(st.cget("escalate_hours")) * 3600
    now = time.time()
    for ip, e in bh.snapshot()["ips"].items():
        if e.get("ok") is not False or now - e.get("since", now) < thr:
            continue
        it = iss.get_open(ip) or iss.open_issue(ip)
        if it.get("status") == "in_progress" or it.get("escalated"):
            continue
        if ip in maint_ips():
            continue
        iss.mark_escalated(ip)
        hrs = int((now - e.get("since", now)) / 3600)
        _owner_send(f"⚠️ <b>Эскалация</b>: {esc(inv.label(ip) or ip)} офлайн "
                    f"уже <b>{hrs} ч</b>, никто не взял в работу.",
                    markup=iss.issue_kb(ip))


def _tick_maint_expire():
    """Окно истекло само — закрыть с протоколом."""
    now = time.time()
    expired = [w["id"] for w in store.jload(_mpath(), _DEF)["active"]
               if w.get("until", 0) <= now]
    owner = st.cget("owner_chat_id")
    for wid in expired:
        if owner:
            _end_windows(owner, wid, reason="окно истекло")


def ops_tick():
    """Минутный тик из bot_health.run_loop."""
    for fn in (_flush_quiet, _tick_snooze, _tick_escalate, _tick_maint_expire):
        try:
            fn()
        except Exception:
            log_exc(f"ops_tick: {fn.__name__}")


def alert_kb(ip: str) -> dict:
    return iss.issue_kb(ip)


try:  # регистрация минутного тика (159-163)
    import bot_health as _bh
    if ops_tick not in _bh.MINUTE_TICKS:
        _bh.MINUTE_TICKS.append(ops_tick)
except Exception:
    log_exc("bot_ops: не смог зарегистрировать тик")


HANDLERS = {"/maint": cmd_maint}
ALIASES = {"/работы": "/maint"}
CALLBACKS = {}
