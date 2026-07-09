# -*- coding: utf-8 -*-
"""Волна E — цикл релизов и рестартов (bot_release):
225 /version (git describe) · 226 /changelog (docs/CHANGELOG.md) ·
227 /upgrade (git pull --ff-only -> py_compile -> exit(0); грязный репо или
неудачный pull = отказ; git-команды, меняющие историю, НЕ выполняются) ·
229/230 маркер выхода + отчёт «почему был рестарт» · 234 детект flapping по
restarts.csv (пишет run_bot.cmd, 235/236) · 244 /slo из канонического NDJSON ·
223 /metrics_push в Google Sheets · 228 /profile — профили таймаутов."""
import os
import json
import time
import atexit
import logging
import threading
import subprocess

import bot_state as st
import bot_obs as obs
from bot_tg import send, send_chunks
from bot_util import log, log_exc, esc, BOT_VERSION

BASE = st.BASE
CREATE_NO_WINDOW = 0x08000000
_MARK = ["unknown"]     # причина выхода этой сессии ("unknown" = ещё не задана)


# ---------- git ----------
def _git(*args, timeout=30):
    try:
        r = subprocess.run(["git"] + list(args), cwd=BASE, capture_output=True,
                           timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return (r.returncode, (r.stdout or b"").decode("utf-8", "replace").strip(),
                (r.stderr or b"").decode("utf-8", "replace").strip())
    except Exception as e:
        return -1, "", f"{type(e).__name__}: {e}"


def git_version():
    rc, out, _err = _git("describe", "--always", "--dirty", "--tags")
    return out if rc == 0 and out else None


# ---------- 230: маркер выхода ----------
def mark_exit(reason: str, code=None) -> None:
    """Пишет exit_marker.json СРАЗУ (os._exit не выполняет atexit)."""
    _MARK[0] = reason
    try:
        st._atomic_write(st.cget("exit_marker_path"),
                         {"reason": reason, "code": code, "ts": int(time.time()),
                          "pid": os.getpid(), "version": BOT_VERSION,
                          "uptime_s": int(time.time() - st.STATS["started"])})
    except Exception:
        pass


def _atexit_marker() -> None:
    if _MARK[0] == "unknown":   # никто не задал причину — обычный выход
        mark_exit("atexit", None)


def install_exit_marker() -> None:
    atexit.register(_atexit_marker)


# ---------- 236: парсер restarts.csv (пишет run_bot.cmd) ----------
def parse_restarts(text: str) -> list:
    """'ts;exit_code;life_s;pause_s' -> [{'ts','code','life','pause'}]."""
    rows = []
    for ln in (text or "").splitlines():
        parts = ln.strip().split(";")
        if len(parts) < 3 or parts[0].startswith("ts"):
            continue
        try:
            ts = time.mktime(time.strptime(parts[0], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        try:
            code = int(parts[1])
        except ValueError:
            code = None
        try:
            life = int(parts[2])
        except ValueError:
            life = None
        pause = None
        if len(parts) > 3:
            try:
                pause = int(parts[3])
            except ValueError:
                pause = None
        rows.append({"ts": ts, "code": code, "life": life, "pause": pause})
    return rows


def flap_count(rows: list, now: float = None, window_s: float = 3600) -> int:
    """234: сколько рестартов за последний час (будущие ts не считаем)."""
    now = now or time.time()
    return sum(1 for r in rows if 0 <= now - r["ts"] <= window_s)


def _read_restarts() -> list:
    try:
        with open(st.cget("restarts_csv"), encoding="utf-8", errors="replace") as f:
            return parse_restarts(f.read())
    except OSError:
        return []


def _stderr_last_error() -> str:
    """229: последняя строка-ошибка из хвоста bot_stderr.log."""
    try:
        with open(os.path.join(BASE, "bot_stderr.log"), "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - 30 * 1024))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for ln in reversed(tail):
        low = ln.lower()
        if ("error" in low or "critical" in low or "traceback" in low
                or "exception" in low):
            return ln.strip()[:160]
    return ""


def _fmt_ago(ts) -> str:
    try:
        return time.strftime("%d.%m %H:%M", time.localtime(ts))
    except Exception:
        return "?"


def restart_report() -> str:
    """229 + 234: короткий текст к «♻️ бот перезапущен» — почему и не flapping ли.
    Маркер после разбора удаляется (его отсутствие = жёсткая смерть процесса)."""
    path = st.cget("exit_marker_path")
    marker = None
    try:
        with open(path, encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        pass
    lines = []
    if marker:
        up = int(marker.get("uptime_s") or 0)
        lines.append(f"Прошлый выход: {marker.get('reason')} "
                     f"(код {marker.get('code')}, аптайм {up // 3600}ч "
                     f"{(up % 3600) // 60}м, {_fmt_ago(marker.get('ts'))})")
        try:
            os.remove(path)
        except OSError:
            pass
    else:
        rows = _read_restarts()
        code = rows[-1].get("code") if rows else None
        err = _stderr_last_error()
        lines.append("Маркера выхода нет — прошлый процесс был убит жёстко "
                     f"(kill/OOM/watchdog{f', код {code}' if code is not None else ''})"
                     + (f"\nПоследняя ошибка: {err}" if err else ""))
    n = flap_count(_read_restarts())
    if n > int(st.cget("flap_per_hour")):
        err = _stderr_last_error()
        lines.append(f"⚠️ <b>Бот в цикле падений</b>: {n} рестартов за час!"
                     + (f"\nПоследняя ошибка: {err}" if err else ""))
        obs.jlog("flapping", level="CRITICAL", restarts_hour=n)
    return "\n".join(lines)


def startup_diagnostics() -> None:
    """Стартовая сводка: 225 git-версия, 242 окружение, 248 sha кода."""
    ver = git_version()
    log(f"VERSION: bot v{BOT_VERSION} · git {ver or 'недоступен'}")
    obs.jlog("start", version=BOT_VERSION, git=ver, pid=os.getpid())
    try:
        import bot_debug
        bot_debug.log_env_snapshot()      # 242
        bot_debug.check_code_hashes()     # 248
    except Exception:
        log_exc("release: стартовая диагностика")


# ---------- 225: /version ----------
def cmd_version(chat, arg="", reply_to=None):
    import sys
    ver = git_version()
    rc, branch, _e = _git("rev-parse", "--abbrev-ref", "HEAD")
    rc2, last, _e2 = _git("log", "-1", "--format=%h %ad %s", "--date=format:%d.%m %H:%M")
    send(chat,
         f"🏷 <b>Версия бота</b>\n"
         f"Код: v{esc(BOT_VERSION)}\n"
         f"Git: <code>{esc(ver or 'недоступен')}</code>"
         + (f" · ветка <code>{esc(branch)}</code>" if rc == 0 and branch else "")
         + (f"\nКоммит: {esc(last)}" if rc2 == 0 and last else "")
         + f"\nPython {esc(sys.version.split()[0])} · PID {os.getpid()}\n"
           f"Профиль таймаутов: {esc(st.cget('active_profile'))} · "
           f"канал {esc(obs.channel_state())}",
         reply_to=reply_to)


# ---------- 226: /changelog ----------
def changelog_entries(text: str) -> list:
    """Секции '## ...' из CHANGELOG.md, свежие первыми (как в файле)."""
    out, cur = [], None
    for ln in (text or "").splitlines():
        if ln.startswith("## "):
            cur = [ln[3:].strip()]
            out.append(cur)
        elif cur is not None and ln.strip():
            cur.append(ln.rstrip())
    return ["\n".join(e) for e in out]


def cmd_changelog(chat, arg="", reply_to=None):
    path = st.cget("changelog_path")
    try:
        with open(path, encoding="utf-8") as f:
            entries = changelog_entries(f.read())
    except OSError:
        send(chat, f"CHANGELOG не найден: <code>{esc(path)}</code>", reply_to=reply_to)
        return
    if not entries:
        send(chat, "CHANGELOG пуст.", reply_to=reply_to)
        return
    lines = ["📜 <b>Последние изменения</b> (docs/CHANGELOG.md):"]
    for e in entries[:5]:
        head, _, body = e.partition("\n")
        lines.append(f"\n<b>{esc(head)}</b>\n{esc(body[:400])}"
                     + ("…" if len(body) > 400 else ""))
    send_chunks(chat, lines)


# ---------- 227: /upgrade ----------
def _compile_all() -> list:
    """py_compile всех модулей бота; список ошибок (пусто = ок)."""
    import py_compile
    import bot_debug
    errs = []
    for fn in bot_debug._code_files():
        try:
            py_compile.compile(fn, doraise=True)
        except Exception as e:
            errs.append(f"{os.path.basename(fn)}: {e}")
    return errs


def cmd_upgrade(chat, arg="", reply_to=None):
    """git pull --ff-only -> py_compile -> exit(0) (обёртка поднимет новую
    версию). Правки в отслеживаемых .py = отказ. Историю git НЕ трогаем."""
    rc, out, err = _git("status", "--porcelain")
    if rc != 0:
        send(chat, f"⛔ git недоступен: <code>{esc(err or out)}</code>",
             reply_to=reply_to)
        return
    dirty_py = [ln for ln in out.splitlines()
                if not ln.startswith("??") and ln.strip().endswith(".py")]
    if dirty_py:
        send(chat, "⛔ <b>Отказ</b>: в репозитории незакоммиченные правки "
                   "кода:\n<pre>" + esc("\n".join(dirty_py[:10])) + "</pre>\n"
                   "Закоммить или убери их, потом /upgrade.", reply_to=reply_to)
        return
    was = git_version()
    send(chat, f"⬇️ Обновляюсь: git pull (сейчас <code>{esc(was or '?')}</code>)…",
         silent=True, reply_to=reply_to)
    rc, out, err = _git("pull", "--ff-only", timeout=120)
    if rc != 0:
        send(chat, f"⛔ <b>git pull не удался</b> — остаюсь на "
                   f"<code>{esc(was or '?')}</code>:\n"
                   f"<pre>{esc((err or out)[:600])}</pre>", reply_to=reply_to)
        return
    if "Already up to date" in out or "Уже обновлено" in out:
        send(chat, f"✅ Уже последняя версия (<code>{esc(was or '?')}</code>) — "
                   f"перезапуск не нужен.", reply_to=reply_to)
        return
    errs = _compile_all()
    if errs:
        send(chat, "⛔ <b>py_compile: ошибки в новом коде</b> — перезапуск "
                   "ОТМЕНЁН, процесс живёт на старом коде из памяти. "
                   "Почини и повтори /upgrade:\n<pre>"
                   + esc("\n".join(errs[:5])) + "</pre>", reply_to=reply_to)
        obs.jlog("upgrade_compile_fail", level="ERROR", errors=errs[:5])
        return
    now = git_version()
    obs.audit("/upgrade", f"{was} -> {now}", "OK")
    send(chat, f"✅ Обновлено: <code>{esc(was or '?')}</code> → "
               f"<code>{esc(now or '?')}</code>, компиляция чистая.\n"
               f"♻️ Перезапускаюсь (run_bot.cmd поднимет новую версию)…",
         reply_to=reply_to)
    log(f"UPGRADE {was} -> {now}, рестарт", logging.WARNING)
    mark_exit("upgrade", 0)
    threading.Timer(1.5, lambda: os._exit(0)).start()


# ---------- 244: /slo ----------
def _read_cmd_events(hours: float = 24) -> list:
    cut = time.time() - hours * 3600
    out = []
    path = st.cget("obs_jsonl_path")
    for p in (path + ".1", path):
        try:
            with open(p, encoding="utf-8") as f:
                for ln in f:
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        continue
                    if rec.get("event") == "cmd" and rec.get("ts", 0) >= cut:
                        out.append(rec)
        except OSError:
            pass
    return out


def slo_table(events: list) -> list:
    """[(cmd, count, p50, p95, err_pct)] по убыванию count."""
    by = {}
    for e in events:
        by.setdefault(e.get("cmd", "?"), []).append(e)
    rows = []
    for cmd, evs in by.items():
        durs = [float(e.get("dur") or 0) for e in evs]
        errs = sum(1 for e in evs if not e.get("ok"))
        rows.append((cmd, len(evs), obs.pctl(durs, 50) or 0,
                     obs.pctl(durs, 95) or 0, 100.0 * errs / len(evs)))
    return sorted(rows, key=lambda r: -r[1])


def cmd_slo(chat, arg="", reply_to=None):
    try:
        hours = max(1, min(int(arg), 168))
    except (TypeError, ValueError):
        hours = 24
    events = _read_cmd_events(hours)
    if not events:
        send(chat, f"📈 За {hours}ч канонических событий нет — они копятся в "
                   f"<code>camera_bot.jsonl</code> с этого запуска.",
             reply_to=reply_to)
        return
    rows = slo_table(events)
    total = sum(r[1] for r in rows)
    errs = sum(int(r[1] * r[4] / 100 + 0.5) for r in rows)
    tab = [f"{'команда':<14} {'n':>4} {'p50':>6} {'p95':>6} {'err%':>5}"]
    for cmd, n, p50, p95, ep in rows[:20]:
        tab.append(f"{cmd[:14]:<14} {n:>4} {p50:>5.1f}s {p95:>5.1f}s {ep:>4.0f}%")
    s = obs.poll_stats()
    send(chat,
         f"📈 <b>SLO за {hours}ч</b>: команд {total}, ошибок {errs}\n"
         f"<pre>{esc(chr(10).join(tab))}</pre>"
         f"Канал: {esc(obs.channel_state())} · poll p95 сверх таймаута "
         f"{(s['p95'] or 0):.1f}s · tg с 1-й попытки "
         f"{obs.tg_hist()['first_try_pct']}%",
         reply_to=reply_to)


# ---------- 223: /metrics_push ----------
def cmd_metrics_push(chat, arg="", reply_to=None):
    path = st.cget("metrics_csv_path")
    try:
        with open(path, encoding="utf-8") as f:
            rows = [ln.strip().split(";") for ln in f if ln.strip()]
    except OSError:
        send(chat, "metrics.csv ещё нет — метрики копятся раз в "
                   f"{st.cget('metrics_period_min')} мин.", reply_to=reply_to)
        return
    send(chat, f"📤 Пушу {len(rows) - 1} строк метрик в Google-таблицу…",
         silent=True, reply_to=reply_to)
    try:
        import google_api as g
        import bot_sheets as bsh
        from urllib.parse import quote
        title = st.cget("metrics_sheet_name")
        sid, sa = st.cget("sheet_id"), st.cget("sa_path")
        meta = g.gjson("GET", f"{bsh.SHEETS}/{sid}", sa_path=sa, timeout=30)
        existing = {s_["properties"]["title"] for s_ in meta["sheets"]}
        if title not in existing:
            g.gjson("POST", f"{bsh.SHEETS}/{sid}:batchUpdate", sa_path=sa,
                    json={"requests": [{"addSheet": {"properties":
                                                     {"title": title}}}]})
        g.request("POST", f"{bsh.SHEETS}/{sid}/values/{quote(title)}:clear",
                  sa_path=sa).raise_for_status()
        r = g.gjson("PUT", f"{bsh.SHEETS}/{sid}/values/{quote(title + '!A1')}"
                           f"?valueInputOption=RAW",
                    sa_path=sa, json={"values": rows}, timeout=120)
        send(chat, f"✅ Метрики в таблице: лист «{esc(title)}», "
                   f"{r.get('updatedCells', 0)} ячеек.\n"
                   f'<a href="{bsh.sheet_url()}">Открыть таблицу</a>')
    except Exception as e:
        log_exc("metrics_push")
        send(chat, f"⛔ Пуш метрик не удался: <code>{esc(str(e)[:200])}</code>")


# ---------- 228: /profile ----------
def apply_profile(name: str) -> dict:
    profiles = st.cget("timeout_profiles") or {}
    p = profiles.get(name)
    if not isinstance(p, dict):
        return {}
    with st._cfg_lock:
        st.CFG["active_profile"] = name
        for k, v in p.items():
            st.CFG[k] = v
        st.save_cfg()
    log(f"PROFILE: применён «{name}»: {p}")
    obs.jlog("profile", name=name, values=p)
    return p


def cmd_profile(chat, arg="", reply_to=None):
    profiles = st.cget("timeout_profiles") or {}
    a = (arg or "").strip().lower()
    if not a:
        cur = st.cget("active_profile")
        lines = [f"⚙️ <b>Профили таймаутов</b> (активен: <b>{esc(cur)}</b>):"]
        for name, p in profiles.items():
            mark = "• " if name == cur else "  "
            lines.append(f"{mark}<code>{esc(name)}</code>: "
                         + ", ".join(f"{k}={v}" for k, v in p.items()))
        lines.append("Переключить: <code>/profile harsh</code> — метрики "
                     "помечаются именем профиля (честное A/B под DPI).")
        send(chat, "\n".join(lines), reply_to=reply_to)
        return
    if a not in profiles:
        send(chat, f"Нет профиля «{esc(a)}». Есть: "
                   + ", ".join(f"<code>{esc(n)}</code>" for n in profiles),
             reply_to=reply_to)
        return
    p = apply_profile(a)
    obs.audit("/profile", a, "OK")
    send(chat, f"✅ Профиль <b>{esc(a)}</b> применён: "
               + ", ".join(f"{esc(k)}={esc(v)}" for k, v in p.items())
               + "\nДействует сразу (ключи в конфиге).", reply_to=reply_to)


HANDLERS = {
    "/version": cmd_version, "/changelog": cmd_changelog,
    "/upgrade": cmd_upgrade, "/slo": cmd_slo,
    "/metrics_push": cmd_metrics_push, "/profile": cmd_profile,
}
ALIASES = {
    "/версия": "/version", "/обновись": "/upgrade", "/профиль": "/profile",
}
CALLBACKS = {}
