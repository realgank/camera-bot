# -*- coding: utf-8 -*-
"""Волна I — Google Calendar тем же сервис-аккаунтом (scope calendar):
417 календарь «ППР камеры» (создание + расшаривание владельцу + события
чистки/юстировки по камерам: /gcal ppr <ip|имя> <дата> [текст]),
418 инфраструктурные напоминания (/gcal remind <дата> <текст>; /gcal setup
сам ставит напоминание о ротации ключа SA за 14 дней до порога 180 дн.).
Calendar API может быть НЕ включён в консоли проекта SA — тогда честная
подсказка, ничего не падает. Id календаря — в _gapi_state.json."""
import os
import time
import datetime

import bot_state as st
import bot_store as store
import google_api as g
from bot_util import log, log_exc, esc, human_err

CAL = "https://www.googleapis.com/calendar/v3"
HINT_403 = ("⛔ Calendar API отвечает 403 — скорее всего он не включён для "
            "проекта сервис-аккаунта. Включи в консоли Google Cloud: "
            "APIs &amp; Services → Enable → Google Calendar API, и повтори.")


def _sa():
    return st.cget("sa_path")


def _spath():
    return st.cget("ga_state_path")


def _is_403(e):
    return "403" in str(e)


def ensure_calendar():
    """417: календарь «ППР камеры» (создать один раз + расшарить владельцу)."""
    s = store.jload(_spath(), {})
    if s.get("gcal_id"):
        return s["gcal_id"]
    j = g.gjson("POST", f"{CAL}/calendars", sa_path=_sa(),
                scope=g.SCOPE_CALENDAR,
                json={"summary": st.cget("gcal_name"),
                      "timeZone": "Europe/Moscow"}, timeout=30)
    cal_id = j["id"]
    email = st.cget("gcal_share_email")
    if email:
        try:
            g.gjson("POST", f"{CAL}/calendars/{cal_id}/acl", sa_path=_sa(),
                    scope=g.SCOPE_CALENDAR, timeout=30,
                    json={"role": "owner",
                          "scope": {"type": "user", "value": email}})
        except Exception:
            log_exc("gcal: не смог расшарить календарь (не критично)")
    store.jupdate(_spath(), {}, lambda d: {**d, "gcal_id": cal_id})
    log(f"gcal: создан календарь «{st.cget('gcal_name')}» -> {cal_id}")
    return cal_id


def add_event(date_s, summary, description=""):
    """Событие на весь день date_s (ГГГГ-ММ-ДД). Возвращает htmlLink."""
    cal_id = ensure_calendar()
    end = (datetime.date.fromisoformat(date_s)
           + datetime.timedelta(days=1)).isoformat()
    j = g.gjson("POST", f"{CAL}/calendars/{cal_id}/events", sa_path=_sa(),
                scope=g.SCOPE_CALENDAR, timeout=30,
                json={"summary": summary, "description": description,
                      "start": {"date": date_s}, "end": {"date": end},
                      "reminders": {"useDefault": False, "overrides": [
                          {"method": "popup", "minutes": 9 * 60}]}})
    return j.get("htmlLink") or ""


def _parse_date(tok):
    try:
        return datetime.date.fromisoformat(tok).isoformat()
    except ValueError:
        return None


def setup_reminders():
    """418: стандартные инфраструктурные дедлайны (ключ SA)."""
    out = []
    try:
        key_day = (datetime.date.fromtimestamp(os.path.getmtime(_sa()))
                   + datetime.timedelta(
                       days=float(st.cget("sa_key_max_age_days")) - 14))
        if key_day > datetime.date.today():
            add_event(key_day.isoformat(),
                      "🔑 Ротация ключа сервис-аккаунта Google",
                      "Ключ SA приближается к порогу 180 дн. — создай новый "
                      "ключ, подмени json, проверь /ga_health, удали старый "
                      "(идея 441).")
            out.append(f"ключ SA — {key_day.isoformat()}")
    except OSError:
        pass
    return out


def cmd_gcal(chat, arg="", reply_to=None):
    """/gcal — статус; setup — создать+расшарить+напоминания;
    ppr <ip|имя> <дата> [текст]; remind <дата> <текст>."""
    from bot_tg import send, chat_action
    parts = (arg or "").split()
    sub = parts[0].lower() if parts else ""
    chat_action(chat)
    try:
        if sub == "setup":
            cal_id = ensure_calendar()
            rem = setup_reminders()
            send(chat, f"📅 Календарь «{esc(st.cget('gcal_name'))}» готов "
                       f"(id <code>{esc(cal_id[:40])}…</code>), расшарен на "
                       f"{esc(st.cget('gcal_share_email'))}."
                       + (f"\n🔔 Напоминания: {esc(', '.join(rem))}" if rem
                          else ""), reply_to=reply_to)
            return
        if sub == "ppr" and len(parts) >= 3:  # /gcal ppr AS-7C.01 2026-08-01 …
            import bot_net as net
            import bot_inventory as inv
            cam, date_s = parts[1], _parse_date(parts[2])
            note = " ".join(parts[3:]) or "чистка/юстировка"
            if not date_s:
                send(chat, "Дата в виде ГГГГ-ММ-ДД.", reply_to=reply_to)
                return
            ip = cam if net.valid_ip(cam) else inv.resolve_ip(cam)
            rec = inv.get(ip) if ip else {}
            label = (rec or {}).get("name") or cam
            loc = (rec or {}).get("location") or ""
            link = add_event(date_s, f"ППР {label}: {note}",
                             f"Камера {label} · {ip or '?'} · {loc}")
            send(chat, f"📅 Событие ППР создано на {date_s}: {esc(label)} — "
                       f"{esc(note)}" + (f'\n<a href="{link}">открыть</a>'
                                         if link else ""), reply_to=reply_to)
            return
        if sub == "remind" and len(parts) >= 3:  # 418
            date_s = _parse_date(parts[1])
            if not date_s:
                send(chat, "Дата в виде ГГГГ-ММ-ДД.", reply_to=reply_to)
                return
            text = " ".join(parts[2:])
            link = add_event(date_s, f"🔔 {text}", "Напоминание из camera_bot")
            send(chat, f"🔔 Напоминание создано на {date_s}: {esc(text)}",
                 reply_to=reply_to)
            return
        s = store.jload(_spath(), {})
        send(chat,
             "📅 <b>Google Calendar (ППР)</b>\n"
             + (f"Календарь: <code>{esc(str(s.get('gcal_id'))[:40])}…</code>"
                if s.get("gcal_id") else "Календарь ещё не создан")
             + "\n/gcal setup — создать и расшарить\n"
               "/gcal ppr <code>&lt;ip|имя&gt; &lt;ГГГГ-ММ-ДД&gt; [текст]</code>\n"
               "/gcal remind <code>&lt;ГГГГ-ММ-ДД&gt; &lt;текст&gt;</code>\n"
               "Локальный план ППР без Google — /ppr.", reply_to=reply_to)
    except Exception as e:
        log_exc("/gcal")
        send(chat, HINT_403 if _is_403(e)
             else human_err("Calendar не ответил", e), reply_to=reply_to)


HANDLERS = {"/gcal": cmd_gcal}
ALIASES = {"/календарь": "/gcal"}
CALLBACKS = {}
