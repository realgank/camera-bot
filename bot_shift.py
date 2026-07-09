# -*- coding: utf-8 -*-
"""Волна D — смены и планирование: 185 /shift start|end (журнал смены),
186 сводка передачи смены («сменщику»), 187 /away (дайджест за N дней),
188 /today (план работ на день), 189 календарь ППР (ppr_schedule.json +
напоминания в тике), 190 /ppr done|report (журнал выполнения в JSON,
без записи в прод-xlsx). Пароли камер НЕ меняются."""
import time
import datetime

import bot_state as st
import bot_inventory as inv
import bot_issues as iss
import bot_store as store
from bot_tg import send, send_chunks
from bot_util import log, esc

_SH_DEF = {"active": None, "log": []}
_PPR_DEF = {"zones": {}, "last_check": ""}


# ---------- 185/186: /shift ----------
def cmd_shift(chat, arg="", reply_to=None):
    import bot_health as bh
    a = (arg or "").strip().lower()
    d = store.jload(st.cget("shift_path"), _SH_DEF)
    if a in ("start", "старт"):
        if d.get("active"):
            send(chat, "Смена уже открыта — /shift end для завершения.",
                 reply_to=reply_to)
            return
        off = bh.offline_ips()
        d["active"] = {"start": int(time.time()), "start_offline": off,
                       "start_open": [it["id"] for it in iss.open_issues()]}
        store.jsave(st.cget("shift_path"), d)
        send(chat, f"🕗 <b>Смена открыта</b> "
                   f"{datetime.datetime.now():%d.%m %H:%M}.\n"
                   f"На старте: офлайн {len(off)}, открытых проблем "
                   f"{len(d['active']['start_open'])}. Удачи!", reply_to=reply_to)
        return
    if a in ("end", "конец", "стоп"):
        act = d.get("active")
        if not act:
            send(chat, "Смена не открыта — /shift start.", reply_to=reply_to)
            return
        now = time.time()
        dur = int(now - act["start"])
        win_days = max(0.02, (now - act["start"]) / 86400)
        ev = [e for e in bh.history_events(win_days + 0.1)
              if e.get("ts", 0) >= act["start"]]
        downs = [e for e in ev if e.get("ev") == "down"]
        ups = [e for e in ev if e.get("ev") == "up"]
        off_now = bh.offline_ips()
        fixed = [it for it in iss.data()["issues"]
                 if it["status"] == "fixed" and (it.get("fixed") or 0) >= act["start"]]
        lines = [f"🕗 <b>Итог смены</b> ({dur // 3600}ч {(dur % 3600) // 60}м):",
                 f"• падений: {len(downs)} · восстановлений: {len(ups)}",
                 f"• починено за смену: {len(fixed)}",
                 f"• офлайн на конец: {len(off_now)} "
                 f"(на старте было {len(act.get('start_offline') or [])})"]
        send(chat, "\n".join(lines))
        # 186: блок «сменщику»
        hand = ["📨 <b>Сменщику</b> (копипаст в чат смены):"]
        opens = iss.open_issues()
        if not opens and not off_now:
            hand.append("Всё спокойно: открытых проблем нет, парк онлайн.")
        for it in opens[:15]:
            age = int(now - it["opened"])
            su = it.get("snooze_until")
            hand.append(f"• {inv.label(it['ip']) or it['ip']} — "
                        f"{'в работе' if it['status'] == 'in_progress' else 'открыта'}"
                        f", висит {age // 3600}ч"
                        + (f", в ремонте до "
                           + time.strftime("%d.%m %H:%M", time.localtime(su))
                           if su and su > now else "")
                        + (f" ({esc(it['note'][:60])})" if it.get("note") else ""))
        rest = [ip for ip in off_now
                if not any(it["ip"] == ip for it in opens)]
        if rest:
            hand.append("Офлайн без заведённой проблемы: "
                        + ", ".join(f"<code>{i}</code>" for i in rest[:10]))
        send_chunks(chat, hand)
        d["log"] = (d.get("log") or [])[-60:] + [{
            "start": act["start"], "end": int(now), "downs": len(downs),
            "fixed": len(fixed), "off_end": len(off_now)}]
        d["active"] = None
        store.jsave(st.cget("shift_path"), d)
        return
    act = d.get("active")
    if act:
        dur = int(time.time() - act["start"])
        send(chat, f"🕗 Смена идёт {dur // 3600}ч {(dur % 3600) // 60}м "
                   f"(с {time.strftime('%H:%M', time.localtime(act['start']))}).\n"
                   f"Завершить: /shift end", reply_to=reply_to)
    else:
        send(chat, "🕗 Смена не открыта.\n<code>/shift start</code> — открыть "
                   "(зафиксирую срез парка) · <code>/shift end</code> — итог + "
                   "сводка сменщику.", reply_to=reply_to)


# ---------- 187: /away ----------
def cmd_away(chat, arg="", reply_to=None):
    import bot_health as bh
    import bot_zones as bz
    try:
        days = max(1, min(int(arg), 30))
    except (TypeError, ValueError):
        days = 2
    ev = bh.history_events(days)
    if not ev:
        send(chat, f"🏖 За {days} дн. событий нет — можно было не уезжать.",
             reply_to=reply_to)
        return
    downs = [e for e in ev if e.get("ev") == "down"]
    ups = [e for e in ev if e.get("ev") == "up"]
    by_zone = {}
    zs = list(bz.zones())
    zips = {z: set(bz.zone_ips(z)) for z in zs}
    for e in downs:
        zone = next((z for z in zs if e["ip"] in zips[z]), None)
        key = zone or (e["ip"].rsplit(".", 1)[0] + ".x")
        by_zone.setdefault(key, []).append(e)
    lines = [f"🏖 <b>Пока тебя не было ({days} дн.)</b>: падений {len(downs)}, "
             f"восстановлений {len(ups)}"]
    for zone in sorted(by_zone, key=lambda z: -len(by_zone[z])):
        evs = by_zone[zone]
        lines.append(f"\n<b>{esc(zone)}</b> — {len(evs)}:")
        seen = set()
        for e in evs[:10]:
            if e["ip"] in seen:
                continue
            seen.add(e["ip"])
            lines.append(f"• {esc(inv.label(e['ip']) or e['ip'])} "
                         + time.strftime("%d.%m %H:%M", time.localtime(e["ts"])))
    still = bh.offline_ips()
    lines.append(f"\nСейчас офлайн: {len(still)} (/offline) · "
                 f"открытых проблем: {len(iss.open_issues())} (/issues)")
    send_chunks(chat, lines)


# ---------- 189/190: ППР ----------
def _ppr():
    return store.jload(st.cget("ppr_path"), _PPR_DEF)


def ppr_due(now=None) -> list:
    """[(зона, дата срока, просрочка дней)] — отсортировано по просрочке."""
    today = datetime.date.fromtimestamp(now or time.time())
    out = []
    for z, e in _ppr()["zones"].items():
        period = int(e.get("period_days") or 90)
        try:
            last = datetime.date.fromisoformat(str(e.get("last")))
        except (TypeError, ValueError):
            last = today - datetime.timedelta(days=period + 1)
        due = last + datetime.timedelta(days=period)
        out.append((z, due, (today - due).days))
    return sorted(out, key=lambda x: -x[2])


def cmd_ppr(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    sub = parts[0].lower() if parts else ""
    if sub == "set" and len(parts) >= 3 and parts[2].isdigit():
        zone, period = parts[1], int(parts[2])
        def _fn(d):
            e = d["zones"].setdefault(zone, {})
            e["period_days"] = period
            e.setdefault("last", datetime.date.today().isoformat())
            e.setdefault("log", [])
            return d
        store.jupdate(st.cget("ppr_path"), _PPR_DEF, _fn)
        send(chat, f"🛠 ППР зоны «{esc(zone)}»: раз в {period} дн.",
             reply_to=reply_to)
        return
    if sub == "done" and len(parts) >= 2:
        zone = parts[1]
        note = " ".join(parts[2:])
        if zone not in _ppr()["zones"]:
            send(chat, f"Зоны «{esc(zone)}» нет в ППР — сначала "
                       f"<code>/ppr set {esc(zone)} 90</code>", reply_to=reply_to)
            return
        today = datetime.date.today().isoformat()
        def _fn(d):
            e = d["zones"][zone]
            e["last"] = today
            e.setdefault("log", [])
            e["log"] = e["log"][-40:] + [{"date": today, "note": note,
                                          "by": "владелец"}]
            return d
        store.jupdate(st.cget("ppr_path"), _PPR_DEF, _fn)
        log(f"ppr: {zone} выполнен {today}")
        send(chat, f"✅ ППР «{esc(zone)}» отмечен ({today}). Следующий срок "
                   f"сдвинут. Журнал — в ppr_schedule.json.", reply_to=reply_to)
        return
    if sub == "report":
        d = _ppr()
        q_start = datetime.date.today() - datetime.timedelta(days=92)
        lines = ["🛠 <b>ППР за квартал</b>:"]
        for z, e in sorted(d["zones"].items()):
            done = [x for x in (e.get("log") or [])
                    if str(x.get("date", "")) >= q_start.isoformat()]
            due = next((x for x in ppr_due() if x[0] == z), None)
            over = due and due[2] > 0
            lines.append(f"• <b>{esc(z)}</b>: выполнено {len(done)} раз"
                         + (f" · ⚠️ просрочен на {due[2]} дн." if over else " · ✅"))
        if len(lines) == 1:
            lines.append("Зон в ППР нет — /ppr set <зона> <дней>.")
        send_chunks(chat, lines)
        return
    due = ppr_due()
    if not due:
        send(chat, "🛠 Календарь ППР пуст.\n"
                   "<code>/ppr set атриум 90</code> — ППР раз в 90 дней\n"
                   "<code>/ppr done атриум [замечания]</code> — отметить\n"
                   "<code>/ppr report</code> — сводка за квартал", reply_to=reply_to)
        return
    lines = ["🛠 <b>План ППР</b> (на месяц вперёд):"]
    horizon = datetime.date.today() + datetime.timedelta(days=31)
    for z, dt, over in due:
        if over > 0:
            lines.append(f"🔴 <b>{esc(z)}</b> — просрочен на {over} дн. "
                         f"(срок был {dt:%d.%m})")
        elif dt <= horizon:
            lines.append(f"🟡 {esc(z)} — до {dt:%d.%m.%Y}")
        else:
            lines.append(f"🟢 {esc(z)} — следующий {dt:%d.%m.%Y}")
    lines.append("Отметить: /ppr done <зона> · период: /ppr set <зона> <дней>")
    send(chat, "\n".join(lines), reply_to=reply_to)


def _tick_ppr():
    """189: раз в день в ppr_check_hour — напоминание о просроченных ППР."""
    now = datetime.datetime.now()
    if now.hour < int(st.cget("ppr_check_hour")):
        return
    today = now.date().isoformat()
    d = _ppr()
    if d.get("last_check") == today:
        return
    def _fn(x):
        x["last_check"] = today
        return x
    store.jupdate(st.cget("ppr_path"), _PPR_DEF, _fn)
    overdue = [(z, dt, ov) for z, dt, ov in ppr_due() if ov >= 0]
    if not overdue:
        return
    owner = st.cget("owner_chat_id")
    if owner:
        lines = ["🛠 <b>ППР на этой неделе</b>:"]
        for z, dt, ov in overdue[:10]:
            lines.append(f"• {esc(z)}"
                         + (f" — просрочено {ov} дн." if ov > 0 else " — срок сегодня"))
        lines.append("План: /ppr · отметить: /ppr done <зона>")
        send(owner, "\n".join(lines))


# ---------- 188: /today ----------
def cmd_today(chat, arg="", reply_to=None):
    import bot_health as bh
    now = time.time()
    lines = [f"📋 <b>План на {datetime.date.today():%d.%m.%Y}</b>:"]
    n = 0
    for it in iss.open_issues():
        su = it.get("snooze_until")
        if su and su > now:
            continue
        n += 1
        age = int(now - it["opened"])
        lines.append(f"🔧 {esc(inv.label(it['ip']) or it['ip'])} — "
                     f"{'в работе' if it['status'] == 'in_progress' else 'открыта'}"
                     f" {age // 3600}ч"
                     + (" · ⏰ срок ремонта вышел" if su and su <= now else ""))
    try:
        from bot_lifecycle import reminders
        end_day = datetime.datetime.now().replace(hour=23, minute=59).timestamp()
        for r in reminders():
            if r["ts"] <= end_day:
                n += 1
                lines.append(f"⏰ {time.strftime('%H:%M', time.localtime(r['ts']))} "
                             f"<code>{r['ip']}</code> {esc(r['text'])}")
    except Exception:
        pass
    for z, dt, ov in ppr_due():
        if ov >= 0:
            n += 1
            lines.append(f"🛠 ППР «{esc(z)}»"
                         + (f" — просрочен {ov} дн." if ov > 0 else " — сегодня"))
    off = bh.offline_ips()
    orphan = [ip for ip in off
              if not iss.get_open(ip)][:5]
    for ip in orphan:
        n += 1
        lines.append(f"🔴 {esc(inv.label(ip) or ip)} — офлайн без заведённой "
                     f"проблемы")
    if not n:
        lines.append("✅ Пусто: ни дефектов, ни напоминаний, ни ППР. Кофе ☕")
    send_chunks(chat, lines)


try:  # 189: ежедневная проверка ППР в минутном тике
    import bot_health as _bh
    if _tick_ppr not in _bh.MINUTE_TICKS:
        _bh.MINUTE_TICKS.append(_tick_ppr)
except Exception:
    log("bot_shift: тик ППР не зарегистрирован")


HANDLERS = {
    "/shift": cmd_shift, "/away": cmd_away, "/today": cmd_today,
    "/ppr": cmd_ppr,
}
ALIASES = {
    "/смена": "/shift", "/отпуск": "/away", "/сегодня": "/today",
    "/ппр": "/ppr",
}
CALLBACKS = {}
