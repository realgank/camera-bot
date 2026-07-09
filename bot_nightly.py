# -*- coding: utf-8 -*-
"""Волна I — ночная автоматика ВНУТРИ бота (минутные тики, Task Scheduler
нужен только внешней пробе 449): 446 сверка «сеть vs инвентарь» в 03:00
(ARP/TCP-скан камерных подсетей, результат — в лист «Ночные задачи» и
Telegram-сводка утром), 447 ночной бэкап xlsx в Drive (bot_gdrive2),
415 помесячная SLA-таблица «SLA-<год>» (лист на месяц, 1-го числа + /sla_gs),
448 еженедельная чистка: старые ротированные логи, *.tmp, локальные JPEG;
бэкапы xlsx сверх 20 — только ОТЧЁТ (авто-удаление выключено дефолтом),
450 git-автокоммит инвентаря (xlsx + _facts_*.json) с диффом в сообщении,
419 журнал всех ночных задач в лист «Ночные задачи» (старт, длительность,
результат, ошибка), 449 статус probe-задачи — в /ga_health и /nightly."""
import os
import glob
import time
import datetime
import subprocess

import bot_state as st
import bot_store as store
from bot_util import log, log_exc, esc

_RESULTS: dict = {}     # имя задачи -> последний результат (для /nightly)


# ---------- расписание (чистая функция — тестируется) ----------
def due_at(now_dt, hour, last_date, weekday=None, monthday=None):
    """True, если задача с суточным часом hour ещё не бегала сегодня."""
    if now_dt.hour < int(hour):
        return False
    if weekday is not None and now_dt.weekday() != int(weekday):
        return False
    if monthday is not None and now_dt.day != int(monthday):
        return False
    return last_date != now_dt.date().isoformat()


def _daily(name, hour, weekday=None, monthday=None):
    s = store.jload(st.cget("ga_state_path"), {})
    last = (s.get("nightly_done") or {}).get(name)
    if not due_at(datetime.datetime.now(), hour, last, weekday, monthday):
        return False

    def upd(d):
        d.setdefault("nightly_done", {})
        d["nightly_done"][name] = datetime.date.today().isoformat()
        return d
    store.jupdate(st.cget("ga_state_path"), {}, upd)
    return True


# ---------- 419: журнал ночных задач ----------
def nightly_log(task, t0, ok, info):
    _RESULTS[task] = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "ok": ok, "info": str(info)[:300]}
    try:
        import bot_gsheets2 as gs
        gs._append_rows(st.cget("nightly_sheet_name"),
                        [[time.strftime("%Y-%m-%d %H:%M:%S"), task,
                          f"{time.time() - t0:.1f}s",
                          "OK" if ok else "ОШИБКА", str(info)[:300]]],
                        header=["Когда", "Задача", "Длительность",
                                "Результат", "Детали"])
    except Exception:
        log_exc(f"nightly: журнал «{task}» не записался в лист")
    log(f"nightly: {task}: {'OK' if ok else 'ОШИБКА'} — {str(info)[:160]}")


def _run(task, fn):
    t0 = time.time()
    try:
        info = fn()
        nightly_log(task, t0, True, info or "готово")
    except Exception as e:
        log_exc(f"nightly: {task}")
        nightly_log(task, t0, False, f"{type(e).__name__}: {e}")


# ---------- 446: ночная сверка сеть-vs-инвентарь ----------
def recon_text(live, inv_cams):
    """Чистая сводка: live {ip: mac}, inv_cams — записи инвентаря с ip/nmac."""
    import bot_inventory as inv
    by_ip = {c["ip"]: c for c in inv_cams if c.get("ip")}
    new = sorted((ip for ip in live if ip not in by_ip),
                 key=lambda x: tuple(int(o) for o in x.split(".")))
    down = [c for c in inv_cams if c.get("ip") and c["ip"] not in live]
    macdiff = [c for c in inv_cams
               if c.get("ip") and c.get("nmac")
               and live.get(c["ip"]) and live[c["ip"]] != "—"
               and inv.norm_mac(live[c["ip"]]) != c["nmac"]]
    lines = [f"🌙 <b>Ночная сверка сеть vs инвентарь</b> "
             f"({time.strftime('%d.%m %H:%M')})",
             f"живых {len(live)} · в инвентаре {len(by_ip)} · "
             f"🆕 {len(new)} · ❌ {len(down)} · ⚠️MAC {len(macdiff)}"]
    for ip in new[:10]:
        lines.append(f"🆕 <code>{ip}</code> {esc(live[ip])}")
    for c in down[:10]:
        lines.append(f"❌ <code>{c['ip']}</code> {esc(c.get('name') or '')}")
    if len(new) > 10 or len(down) > 10:
        lines.append("… подробно: /diff")
    if not (new or down or macdiff):
        lines.append("✅ Расхождений нет.")
    stats = f"🆕{len(new)} ❌{len(down)} ⚠️MAC{len(macdiff)} живых {len(live)}"
    return "\n".join(lines), stats


def recon_run():
    import bot_net as net
    import bot_inventory as inv
    live = {}
    for p in st.cget("diff_subnets") or []:
        live.update(net.scan_subnet(p))
    try:  # Волна J (I36): новые хосты ночного скана -> очередь «Неизвестных»
        import bot_unknownq
        bot_unknownq.note_hosts(live, source="night")
    except Exception:
        log_exc("recon: очередь неизвестных")
    text, stats = recon_text(live, inv.cams())
    store.jupdate(st.cget("ga_state_path"), {},
                  lambda d: {**d, "morning_summary": text})
    try:
        import bot_gsheets2 as gs
        gs.note_event("recon", "", "", stats)
    except Exception:
        pass
    return stats


def _morning_tick():
    """Утренняя отправка результата ночной сверки (тихо, раз в день)."""
    if datetime.datetime.now().hour < int(st.cget("health_daily_hour")):
        return
    s = store.jload(st.cget("ga_state_path"), {})
    text = s.get("morning_summary")
    if not text:
        return
    store.jupdate(st.cget("ga_state_path"), {},
                  lambda d: {**d, "morning_summary": None})
    owner = st.cget("owner_chat_id")
    if owner:
        import bot_tg as tgm
        tgm.send(owner, text, silent=True)


# ---------- 448: еженедельная чистка ----------
def cleanup_run():
    base = st.BASE
    now = time.time()
    removed, freed = 0, 0
    days_log = 30
    for pat, days in (("*.log.[0-9]", days_log), ("*.jsonl.[0-9]", days_log),
                      ("*.tmp", 7)):
        for p in glob.glob(os.path.join(base, pat)):
            try:
                if now - os.path.getmtime(p) > days * 86400:
                    freed += os.path.getsize(p)
                    os.remove(p)
                    removed += 1
            except OSError:
                continue
    snaps_dir = os.path.join(base, "snaps")   # локальные JPEG (если появятся)
    if os.path.isdir(snaps_dir):
        cut = now - float(st.cget("cleanup_snaps_days")) * 86400
        for p in glob.glob(os.path.join(snaps_dir, "*.jpg")):
            try:
                if os.path.getmtime(p) < cut:
                    freed += os.path.getsize(p)
                    os.remove(p)
                    removed += 1
            except OSError:
                continue
    # бэкапы xlsx: сверх cleanup_backups_keep — по умолчанию только отчёт
    import bot_backups
    baks = bot_backups.backups_list()
    keep_n = int(st.cget("cleanup_backups_keep"))
    extra = baks[keep_n:]
    bak_s = ""
    if extra:
        if st.cget("cleanup_backups_auto"):
            for b in extra:
                try:
                    freed += b["size"]
                    os.remove(b["path"])
                    removed += 1
                except OSError:
                    continue
            bak_s = f", бэкапов удалено {len(extra)}"
        else:
            bak_s = (f"; бэкапов сверх {keep_n}: {len(extra)} "
                     f"(авто-удаление выключено — /backups)")
    return f"удалено {removed} файлов, {freed // 1048576} МБ{bak_s}"


# ---------- 450: git-автокоммит инвентаря ----------
INV_GIT_FILES = ["Все_камеры.xlsx", "_facts_cameras.json",
                 "_facts_switches.json"]


def git_commit_run():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(["git", "-C", st.BASE, "status", "--porcelain", "--"]
                       + INV_GIT_FILES, capture_output=True, timeout=30,
                       creationflags=flags)
    changed = [ln[3:].strip() for ln in
               r.stdout.decode("utf-8", errors="replace").splitlines() if ln]
    if not changed:
        return "инвентарь не менялся"
    msg = "изменены: " + ", ".join(changed)
    try:  # дифф строк против свежайшего бэкапа — в сообщение коммита
        import bot_backups
        import bot_inventory as inv
        baks = bot_backups.backups_list()
        if baks and any("xlsx" in c for c in changed):
            d = bot_backups.diff_xlsx(baks[0]["path"], inv.inv_path())
            msg += (f"; строк {d['a_n']}→{d['b_n']} "
                    f"(+{len(d['added'])}/-{len(d['removed'])}"
                    f"/✏️{len(d['changed'])})")
    except Exception:
        log_exc("nightly: дифф для сообщения коммита")
    subprocess.run(["git", "-C", st.BASE, "add", "--"] + INV_GIT_FILES,
                   capture_output=True, timeout=30, creationflags=flags)
    r = subprocess.run(["git", "-C", st.BASE, "commit",
                        "-m", f"inventory-nightly: {msg}", "--"]
                       + INV_GIT_FILES, capture_output=True, timeout=30,
                       creationflags=flags)
    if r.returncode != 0:
        return ("коммит пропущен: "
                + r.stdout.decode(errors="replace")[:120].strip())
    return f"коммит: {msg}"


# ---------- 415: помесячная SLA-таблица ----------
def sla_rows(month):
    """Строки SLA за месяц ГГГГ-ММ из _metrics.db (чисто, без Google)."""
    import bot_metrics as mx
    import bot_health as bh
    y, m = int(month[:4]), int(month[5:7])
    t0 = datetime.datetime(y, m, 1).timestamp()
    t1 = (datetime.datetime(y + (m == 12), m % 12 + 1, 1)).timestamp()
    span = min(t1, time.time()) - t0
    if span <= 0:
        return []
    ips = sorted(bh.snapshot()["ips"],
                 key=lambda x: tuple(int(o) for o in x.split(".")))
    per_ip, by_sub = [], {}
    for ip in ips:
        down = mx.downtime_s(ip, t0, min(t1, time.time()))
        pct = max(0.0, 100.0 * (1 - down / span))
        per_ip.append((ip, pct, down))
        sub = ip.rsplit(".", 1)[0]
        t, s = by_sub.get(sub, (0, 0.0))
        by_sub[sub] = (t + 1, s + pct)
    rows = [[f"SLA {month}", "камер", "аптайм %"],
            ["ПАРК", len(per_ip),
             round(sum(p for _i, p, _d in per_ip) / max(1, len(per_ip)), 2)]]
    for sub, (t, s) in sorted(by_sub.items()):
        rows.append([f"{sub}.x", t, round(s / t, 2)])
    rows.append([])
    rows.append(["Худшие камеры", "аптайм %", "простой, ч"])
    for ip, pct, down in sorted(per_ip, key=lambda x: x[1])[:15]:
        rows.append([ip, round(pct, 2), round(down / 3600, 1)])
    return rows


def sla_push(month=None):
    """415: лист «ГГГГ-ММ» в таблице «SLA-<год>» (find-or-create в Drive)."""
    from urllib.parse import quote
    import google_api as g
    import bot_gdrive2 as gd
    month = month or (datetime.date.today().replace(day=1)
                      - datetime.timedelta(days=1)).strftime("%Y-%m")
    rows = sla_rows(month)
    if not rows:
        return f"нет данных за {month}"
    title = f"SLA-{month[:4]}"
    sa = st.cget("sa_path")
    s = store.jload(st.cget("ga_state_path"), {})
    sid = (s.get("sla_sheets") or {}).get(title)
    if not sid:
        got = gd.files_list(
            f"name='{title}' and trashed=false and "
            f"mimeType='application/vnd.google-apps.spreadsheet'",
            fields="files(id,name)")
        if got:
            sid = got[0]["id"]
        else:
            j = g.gjson("POST", "https://sheets.googleapis.com/v4/spreadsheets",
                        sa_path=sa, json={"properties": {"title": title}},
                        timeout=60)
            sid = j["spreadsheetId"]
            try:  # в папку снимков, чтобы была видна владельцу
                g.gjson("PATCH", f"https://www.googleapis.com/drive/v3/files/"
                        f"{sid}?addParents={st.cget('drive_folder_id')}",
                        sa_path=sa, scope=g.SCOPE_DRIVE, timeout=30, json={})
            except Exception:
                log_exc("sla: перенос в папку (не критично)")

        def upd(d):
            d.setdefault("sla_sheets", {})
            d["sla_sheets"][title] = sid
            return d
        store.jupdate(st.cget("ga_state_path"), {}, upd)
    meta = g.gjson("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}",
                   sa_path=sa, fields="sheets.properties.title", timeout=30)
    titles = {x["properties"]["title"] for x in meta.get("sheets") or []}
    if month not in titles:
        g.gjson("POST", f"https://sheets.googleapis.com/v4/spreadsheets/"
                f"{sid}:batchUpdate", sa_path=sa,
                json={"requests": [{"addSheet": {"properties":
                                                 {"title": month}}}]})
    g.request("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/"
              f"values/{quote(month)}:clear", sa_path=sa).raise_for_status()
    g.gjson("PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/"
            f"values/{quote(month + '!A1')}?valueInputOption=RAW",
            sa_path=sa, json={"values": rows}, timeout=60)
    return (f"{title}/{month}: {len(rows)} строк · "
            f"https://docs.google.com/spreadsheets/d/{sid}")


# ---------- команды и тик ----------
def cmd_nightly(chat, arg="", reply_to=None):
    from bot_tg import send
    import bot_gdrive2 as gd
    a = (arg or "").strip().lower()
    if a in ("recon", "backup", "cleanup", "git"):
        send(chat, f"▶️ Запускаю «{a}» …", silent=True, reply_to=reply_to)
        fn = {"recon": recon_run, "backup": _backup, "cleanup": cleanup_run,
              "git": git_commit_run}[a]
        _run(a, fn)
        r = _RESULTS.get(a) or {}
        send(chat, f"{'✅' if r.get('ok') else '❌'} {a}: {esc(r.get('info', '?'))}")
        return
    lines = ["🌙 <b>Ночные задачи</b> (в боте, лист «Ночные задачи»):",
             f"• 03:00+ сверка сеть-vs-инвентарь ({'вкл' if st.cget('nightly_recon_enabled') else 'выкл'})",
             f"• 0{st.cget('drive_backup_hour')}:00 бэкап xlsx в Drive "
             f"({'вкл' if st.cget('drive_backup_enabled') else 'выкл'})",
             f"• 03:30+ git-автокоммит инвентаря "
             f"({'вкл' if st.cget('git_autocommit_enabled') else 'выкл'})",
             f"• вс чистка логов/temp ({'вкл' if st.cget('cleanup_enabled') else 'выкл'})",
             f"• 1-го числа SLA-таблица ({'вкл' if st.cget('sla_gsheet_enabled') else 'выкл'})",
             f"⏰ {esc(gd.probe_task_status())}"]
    for name, r in sorted(_RESULTS.items()):
        lines.append(f"{'✅' if r['ok'] else '❌'} {esc(name)} {r['ts']}: "
                     f"{esc(r['info'][:80])}")
    lines.append("Вручную: /nightly recon|backup|cleanup|git · /sla_gs [ГГГГ-ММ]")
    send(chat, "\n".join(lines), reply_to=reply_to)


def cmd_sla_gs(chat, arg="", reply_to=None):
    from bot_tg import send, chat_action
    chat_action(chat)
    month = (arg or "").strip() or None
    try:
        send(chat, "📊 " + esc(sla_push(month)), reply_to=reply_to)
    except Exception as e:
        from bot_util import human_err
        log_exc("/sla_gs")
        send(chat, human_err("SLA-таблица не собралась", e), reply_to=reply_to)


def _backup():
    import bot_gdrive2 as gd
    return gd.backup_xlsx()


def _tick():
    """Минутный тик: раскладка ночных задач по часам."""
    h = int(st.cget("nightly_hour"))
    if st.cget("nightly_recon_enabled") and _daily("recon", h):
        _run("recon", recon_run)
    if st.cget("git_autocommit_enabled") and _daily("git", h):
        _run("git", git_commit_run)
    if st.cget("drive_backup_enabled") \
            and _daily("backup", st.cget("drive_backup_hour")):
        _run("backup", _backup)
    if st.cget("cleanup_enabled") and _daily(
            "cleanup", h + 1, weekday=st.cget("cleanup_weekday")):
        _run("cleanup", cleanup_run)
    if st.cget("sla_gsheet_enabled") and _daily("sla", h + 1, monthday=1):
        _run("sla", sla_push)
    _morning_tick()


HANDLERS = {"/nightly": cmd_nightly, "/sla_gs": cmd_sla_gs}
ALIASES = {"/ночные": "/nightly"}
CALLBACKS = {}

try:
    import bot_health as _bh
    _bh.MINUTE_TICKS.append(_tick)
except Exception:
    log_exc("nightly: не смог зарегистрировать тик")
