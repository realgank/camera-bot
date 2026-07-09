# -*- coding: utf-8 -*-
"""Волна F — журнал и ретроспектива инвентаря:
265 _changelog.jsonl (каждая запись бота в xlsx), 266 /history <ip|mac>,
267 детерминированный CSV-срез в exports\\ + локальный git-коммит ТОЛЬКО этих
CSV с префиксом «inventory:», 268 еженедельный дайджест правок (минутный тик),
296 архив истории статусов _status_history.json (тик после health-прогона),
299 контроль свежести фактов (freshness_note для сверок 270/274/275).
Ротация бэкапов и /diffxlsx — в bot_backups. Прод-xlsx здесь НЕ пишется."""
import os
import io
import csv
import json
import time
import datetime
import threading
import subprocess

import bot_state as st
import bot_inventory as inv
import bot_store as store
from bot_tg import send, send_chunks
from bot_util import log, log_exc, esc


def _state_path():
    return st.cget("reconcile_state_path")


# ---------- 299: свежесть фактов ----------
def facts_age_days(key: str):
    try:
        return (time.time() - os.path.getmtime(st.cget(key))) / 86400
    except OSError:
        return None


def freshness_note() -> str:
    """Строка-предупреждение для сверок 270/274/275 (или '')."""
    maxd = float(st.cget("facts_max_age_days"))
    parts = []
    for key, name in (("facts_cameras", "_facts_cameras"),
                      ("facts_switches", "_facts_switches")):
        age = facts_age_days(key)
        if age is None:
            parts.append(f"{name}: файла нет")
        elif age > maxd:
            parts.append(f"{name}: {age:.0f} дн.")
    if not parts:
        return ""
    return ("⚠️ Факты устарели (" + ", ".join(parts)
            + ") — выводы по сети могут врать, обнови свип.")


# ---------- 265: журнал изменений ----------
def record_change(who, sheet, key, field, old, new) -> None:
    """Append-строка в _changelog.jsonl: кто, когда, лист, ключ, поле, было->стало."""
    rec = {"ts": int(time.time()),
           "dt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "who": str(who), "sheet": sheet, "key": str(key or ""),
           "field": field, "old": "" if old is None else str(old),
           "new": "" if new is None else str(new)}
    path = st.cget("changelog_jsonl")
    try:
        with store.lock_for(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        log_exc("reconcile: _changelog.jsonl не записался")


def changelog_entries(days=None) -> list:
    path = st.cget("changelog_jsonl")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    if days:
        cut = time.time() - days * 86400
        out = [e for e in out if e.get("ts", 0) >= cut]
    return out


# ---------- 267: CSV-срез + git ----------
def export_csv_snapshot() -> list:
    """Детерминированные CSV всех листов в exports\\. Возвращает пути."""
    import bot_dq
    os.makedirs(st.cget("exports_dir"), exist_ok=True)
    paths = []
    for sheet, d in bot_dq.read_all().items():
        fn = os.path.join(st.cget("exports_dir"),
                          sheet.replace(" ", "_").replace("/", "_") + ".csv")
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(d["hdr"])
        for row in d["rows"]:
            w.writerow(["" if c is None else str(c) for c in row])
        with open(fn, "w", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())
        paths.append(fn)
    return paths


def _git_commit_exports(msg: str) -> bool:
    """git add/commit ТОЛЬКО exports\\*.csv с префиксом «inventory:»."""
    if not st.cget("exports_git"):
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        rel = os.path.relpath(st.cget("exports_dir"), st.BASE)
        subprocess.run(["git", "-C", st.BASE, "add", "--", rel],
                       capture_output=True, timeout=30, creationflags=flags)
        r = subprocess.run(["git", "-C", st.BASE, "commit",
                            "-m", f"inventory: {msg}", "--", rel],
                           capture_output=True, timeout=30, creationflags=flags)
        ok = r.returncode == 0
        log("reconcile: git-коммит CSV " + ("OK" if ok else
            f"пропущен ({(r.stdout or b'').decode(errors='replace')[:120].strip()})"))
        return ok
    except Exception:
        log_exc("reconcile: git-коммит CSV не удался")
        return False


def after_xlsx_write(msg: str) -> None:
    """Вызывается после каждой записи бота в xlsx: CSV-срез + git (фоном).
    Волна J (I27): плюс пометка dirty для автосинка."""
    try:
        import bot_autosync
        bot_autosync.mark_dirty(msg)
    except Exception:
        log_exc("reconcile: mark_dirty")

    def run():
        try:
            export_csv_snapshot()
            _git_commit_exports(msg)
        except Exception:
            log_exc("reconcile: after_xlsx_write")
    threading.Thread(target=run, daemon=True, name="csv-git").start()


# ---------- 266: /history ----------
def cmd_history(chat, arg="", reply_to=None):
    a = (arg or "").strip()
    if not a:
        send(chat, "История камеры: <code>/history 10.20.50.51</code> или по "
                   "MAC/имени (журнал правок + лист «Изменённые»).",
             reply_to=reply_to)
        return
    recs = inv.search(a)
    rec = recs[0] if len(recs) == 1 else None
    ip = rec["ip"] if rec and rec.get("ip") else (a if "." in a else None)
    nmac = rec["nmac"] if rec else inv.norm_mac(a)
    events = []
    for e in changelog_entries():
        hay = f"{e.get('key', '')} {e.get('old', '')} {e.get('new', '')}"
        if (ip and ip in hay) or (nmac and len(nmac) >= 6
                                  and nmac in inv.norm_mac(hay)):
            events.append((e["ts"], f"{e['dt']} · {e['sheet']} · "
                           f"{e['field']}: «{e['old']}» → «{e['new']}»"))
    try:
        import bot_dq
        d = bot_dq.read_all().get("Изменённые") or {"hdr": [], "rows": []}
        for row in d["rows"]:
            m = bot_dq._cell(row, d["hdr"], "MAC-адрес")
            new_ip = str(bot_dq._cell(row, d["hdr"], "Новый IP") or "")
            if (nmac and inv.norm_mac(m) == nmac) or (ip and new_ip == ip):
                dt = str(bot_dq._cell(row, d["hdr"], "Дата и время") or "?")
                what = str(bot_dq._cell(row, d["hdr"], "Что сделано") or "?")
                old_ip = str(bot_dq._cell(row, d["hdr"], "Старый IP") or "?")
                try:
                    ts = time.mktime(time.strptime(dt[:16], "%Y-%m-%d %H:%M"))
                except ValueError:
                    ts = 0
                events.append((ts, f"{dt} · ПНР: {old_ip} → {new_ip} · {what}"))
    except Exception:
        log_exc("/history: лист «Изменённые»")
    for e in status_events():
        if (ip and e.get("ip") == ip) or (nmac and e.get("uid") == nmac):
            events.append((e["ts"], time.strftime("%Y-%m-%d %H:%M",
                                                  time.localtime(e["ts"]))
                           + f" · статус: {e['st']}"))
    if not events:
        send(chat, f"🕰 По «{esc(a)}» истории не накоплено (журнал пишется "
                   f"с волны F).", reply_to=reply_to)
        return
    events.sort(key=lambda x: x[0])
    head = f"🕰 <b>История {esc(rec.get('name') or a) if rec else esc(a)}</b>"
    lines = [head] + ["• " + esc(t) for _ts, t in events[-40:]]
    if len(events) > 40:
        lines.insert(1, f"(показаны последние 40 из {len(events)})")
    send_chunks(chat, lines)


# ---------- 268: еженедельный дайджест ----------
def digest_text(days=7) -> str:
    ev = changelog_entries(days)
    if not ev:
        return (f"📰 За {days} дн. правок инвентаря ботом не было.")
    by_field = {}
    for e in ev:
        by_field[e["field"]] = by_field.get(e["field"], 0) + 1
    lines = [f"📰 <b>Дайджест правок инвентаря за {days} дн.</b>: {len(ev)} правок",
             "По полям: " + " · ".join(f"{esc(k)}: {v}"
                                       for k, v in sorted(by_field.items()))]
    try:
        import bot_dq
        d = bot_dq.read_all().get("Изменённые") or {"hdr": [], "rows": []}
        cut = datetime.datetime.now() - datetime.timedelta(days=days)
        n_new = n_ip = 0
        for row in d["rows"]:
            dt = str(bot_dq._cell(row, d["hdr"], "Дата и время") or "")
            try:
                when = datetime.datetime.strptime(dt[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if when >= cut:
                if "заводск" in str(bot_dq._cell(row, d["hdr"],
                                                 "Что сделано") or "").lower():
                    n_new += 1
                else:
                    n_ip += 1
        lines.append(f"ПНР за период: заводских введено {n_new}, "
                     f"прочих смен IP {n_ip}")
    except Exception:
        pass
    lines.append("Подробно: /history <ip> · /diffxlsx · /dq")
    return "\n".join(lines)


def cmd_digest(chat, arg="", reply_to=None):
    try:
        days = max(1, min(int(arg), 90))
    except (TypeError, ValueError):
        days = 7
    send(chat, digest_text(days), reply_to=reply_to)


def digest_tick() -> None:
    """268: в заданный день/час недели шлём дайджест владельцу (раз в неделю)."""
    now = datetime.datetime.now()
    if now.weekday() != int(st.cget("digest_weekday")) \
            or now.hour < int(st.cget("digest_hour")):
        return
    key = now.strftime("%G-%V")  # ISO-неделя
    state = store.jload(_state_path(), {})
    if state.get("last_digest") == key:
        return
    state["last_digest"] = key
    store.jsave(_state_path(), state)
    owner = st.cget("owner_chat_id")
    if owner:
        send(owner, digest_text(7), silent=True)


# ---------- 296: архив истории статусов ----------
_hist_seen = [0]


def status_events() -> list:
    return store.jload(st.cget("status_history_path"),
                       {"cur": {}, "events": []}).get("events", [])


def status_tick() -> None:
    """После каждого нового health-прогона пишем ИЗМЕНЕНИЯ статусов по UID."""
    try:
        import bot_health as bh
        snap = bh.snapshot()
    except Exception:
        return
    lr = snap.get("last_run") or 0
    if not lr or lr == _hist_seen[0]:
        return
    _hist_seen[0] = lr

    def _upd(data):
        cur, events, changed = data.get("cur", {}), data.get("events", []), 0
        for ip, e in snap["ips"].items():
            stt = "online" if e.get("ok") else "offline"
            if cur.get(ip) != stt:
                rec = inv.get(ip) or {}
                events.append({"ts": int(lr), "ip": ip,
                               "uid": rec.get("nmac") or "", "st": stt})
                cur[ip] = stt
                changed += 1
        if not changed:
            return None
        cut = time.time() - 90 * 86400
        return {"cur": cur, "events": [ev for ev in events
                                       if ev.get("ts", 0) >= cut]}
    store.jupdate(st.cget("status_history_path"), {"cur": {}, "events": []}, _upd)


# ---------- регистрация ----------
HANDLERS = {"/history": cmd_history, "/digest": cmd_digest}
ALIASES = {"/дайджест": "/digest", "/история_камеры": "/history"}
CALLBACKS = {}

try:  # тики: дайджест (268) + история статусов (296) в существующем цикле
    import bot_health
    bot_health.MINUTE_TICKS.extend([digest_tick, status_tick])
except Exception:
    log_exc("reconcile: не смог зарегистрировать тики")
