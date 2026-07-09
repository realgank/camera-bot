# -*- coding: utf-8 -*-
"""Волна F — бэкапы и ретроспективные диффы инвентаря:
269 /backups — политика ротации Все_камеры.backup.* (дневные N дн. + недельные
M нед.), отчёт по месту, удаление ТОЛЬКО через двухшаговое подтверждение;
297 /diffxlsx — построчный дифф двух бэкапов (или бэкап vs текущий) по UID
(канонический MAC, фолбэк IP): добавлено/удалено/изменено."""
import os
import io
import csv
import time
import glob
import threading

import bot_state as st
import bot_inventory as inv
from bot_tg import send, send_chunks, send_document, edit_message, answer_cq
from bot_util import log, esc

_BAK_DEL = {}   # token -> (ts, [файлы]) — двухшаговое удаление бэкапов
_bak_lock = threading.Lock()


# ---------- 269: /backups — ротация с подтверждением ----------
def backups_list() -> list:
    pat = os.path.join(st.BASE, "Все_камеры.backup.*.xlsx")
    out = []
    for p in glob.glob(pat):
        try:
            out.append({"path": p, "name": os.path.basename(p),
                        "mtime": os.path.getmtime(p),
                        "size": os.path.getsize(p)})
        except OSError:
            continue
    return sorted(out, key=lambda x: -x["mtime"])


def rotation_plan(baks=None) -> tuple:
    """(оставить, удалить): дневные keep_daily дн., далее по одному на ISO-неделю
    keep_weekly недель, остальное — кандидаты на удаление."""
    baks = baks if baks is not None else backups_list()
    keep_d = float(st.cget("backup_keep_daily"))
    keep_w = float(st.cget("backup_keep_weekly"))
    now = time.time()
    keep, drop, weekly = [], [], {}
    for b in baks:
        age_d = (now - b["mtime"]) / 86400
        if age_d <= keep_d:
            keep.append(b)
        elif age_d <= keep_d + keep_w * 7:
            wk = time.strftime("%G-%V", time.localtime(b["mtime"]))
            if wk not in weekly:
                weekly[wk] = b
                keep.append(b)
            else:
                drop.append(b)
        else:
            drop.append(b)
    return keep, drop


def cmd_backups(chat, arg="", reply_to=None):
    baks = backups_list()
    if not baks:
        send(chat, "💾 Бэкапов Все_камеры.backup.* нет.", reply_to=reply_to)
        return
    keep, drop = rotation_plan(baks)
    total_mb = sum(b["size"] for b in baks) / 1048576
    drop_mb = sum(b["size"] for b in drop) / 1048576
    lines = [f"💾 <b>Бэкапы инвентаря</b>: {len(baks)} файлов · "
             f"{total_mb:.1f} МБ",
             f"Политика: дневные {st.cget('backup_keep_daily')} дн. + "
             f"недельные {st.cget('backup_keep_weekly')} нед.",
             f"Оставить: {len(keep)} · К удалению: <b>{len(drop)}</b> "
             f"({drop_mb:.1f} МБ)"]
    for i, b in enumerate(baks[:15], 1):
        mark = "🗑" if b in drop else "✅"
        lines.append(f"{mark} {i}. <code>{esc(b['name'])}</code> · "
                     f"{b['size'] // 1024} КБ")
    if len(baks) > 15:
        lines.append(f"… и ещё {len(baks) - 15}")
    lines.append("Дифф двух бэкапов: /diffxlsx 1 2 (номера из списка)")
    kb = None
    if drop:
        tok = str(int(time.time()))
        with _bak_lock:
            _BAK_DEL.clear()
            _BAK_DEL[tok] = (time.time(), [b["path"] for b in drop])
        kb = {"inline_keyboard": [[
            {"text": f"🗑 Удалить {len(drop)} старых…",
             "callback_data": f"bkdel:{tok}"},
            {"text": "✖️ Отмена", "callback_data": "cancel"}]]}
    send(chat, "\n".join(lines), markup=kb, reply_to=reply_to)


def cb_bkdel(chat, cq, tok):
    with _bak_lock:
        e = _BAK_DEL.get(tok)
    if not e or time.time() - e[0] > 300:
        answer_cq(cq.get("id"), "⌛ Список устарел — повтори /backups")
        return
    answer_cq(cq.get("id"), "Нужно финальное подтверждение")
    mid = (cq.get("message") or {}).get("message_id")
    txt = (f"⚠️ <b>Финальное подтверждение</b>: безвозвратно удалить "
           f"{len(e[1])} файлов бэкапов? Текущий xlsx не трогается.")
    kb = {"inline_keyboard": [[
        {"text": "✅ Да, удалить", "callback_data": f"bkdely:{tok}"},
        {"text": "✖️ Отмена", "callback_data": "cancel"}]]}
    if mid:
        edit_message(chat, mid, txt, markup=kb)
    else:
        send(chat, txt, markup=kb)


def cb_bkdely(chat, cq, tok):
    with _bak_lock:
        e = _BAK_DEL.pop(tok, None)
    if not e or time.time() - e[0] > 300:
        answer_cq(cq.get("id"), "⌛ Подтверждение устарело — повтори /backups")
        return
    answer_cq(cq.get("id"), "🗑 Удаляю…")
    ok = err = 0
    freed = 0
    for p in e[1]:
        try:
            freed += os.path.getsize(p)
            os.remove(p)
            ok += 1
        except OSError:
            err += 1
    log(f"backups: удалено {ok}, ошибок {err}, освобождено {freed // 1048576} МБ")
    send(chat, f"🗑 Удалено {ok} бэкапов ({freed // 1048576} МБ)"
               + (f", ошибок {err}" if err else "") + ". /backups — проверить.")


# ---------- 297: /diffxlsx ----------
def _load_main(path) -> dict:
    """Главный лист файла -> {uid(mac-канон или ip): {поле: значение}}."""
    import bot_dq
    d = bot_dq.read_all(path).get(inv.SHEET_MAIN)
    if not d:
        return {}
    out = {}
    for row in d["rows"]:
        vals = {h: ("" if (i >= len(row) or row[i] is None) else str(row[i]))
                for i, h in enumerate(d["hdr"]) if h}
        uid = bot_dq.mac_canon(vals.get("MAC-адрес")) \
            or inv.norm_mac(vals.get("MAC-адрес")) or vals.get("IP-адрес")
        if uid:
            out.setdefault(uid, vals)
    return out


def diff_xlsx(path_a, path_b) -> dict:
    """297: {'added': [...], 'removed': [...], 'changed': [(uid, поле, a, b)]}."""
    a, b = _load_main(path_a), _load_main(path_b)
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = []
    for uid in sorted(set(a) & set(b)):
        for f in sorted(set(a[uid]) | set(b[uid])):
            va, vb = a[uid].get(f, ""), b[uid].get(f, "")
            if va != vb:
                changed.append((uid, f, va, vb))
    return {"added": added, "removed": removed, "changed": changed,
            "a_n": len(a), "b_n": len(b)}


def _pick_bak(token, baks):
    if token in ("current", "тек", "cur", "0"):
        return inv.inv_path(), "текущий"
    try:
        i = int(token)
        if 1 <= i <= len(baks):
            return baks[i - 1]["path"], baks[i - 1]["name"]
    except ValueError:
        pass
    for b in baks:
        if token in b["name"]:
            return b["path"], b["name"]
    return None, None


def cmd_diffxlsx(chat, arg="", reply_to=None):
    baks = backups_list()
    parts = (arg or "").split()
    if not parts:
        if not baks:
            send(chat, "Бэкапов нет — сравнивать нечего.", reply_to=reply_to)
            return
        parts = ["1", "0"]  # свежайший бэкап vs текущий
    if len(parts) == 1:
        parts.append("0")
    pa, na = _pick_bak(parts[0], baks)
    pb, nb = _pick_bak(parts[1], baks)
    if not pa or not pb:
        send(chat, "Дифф: <code>/diffxlsx 2 1</code> (номера из /backups), "
                   "<code>/diffxlsx 20260708 0</code> (подстрока имени; 0 = "
                   "текущий файл). Без аргументов — последний бэкап vs текущий.",
             reply_to=reply_to)
        return
    d = diff_xlsx(pa, pb)
    lines = [f"🆚 <b>Дифф</b>: {esc(na)} → {esc(nb)}",
             f"строк {d['a_n']} → {d['b_n']} · ➕{len(d['added'])} · "
             f"➖{len(d['removed'])} · ✏️{len(d['changed'])} изменений"]
    for uid in d["added"][:10]:
        lines.append(f"➕ <code>{esc(uid)}</code>")
    for uid in d["removed"][:10]:
        lines.append(f"➖ <code>{esc(uid)}</code>")
    shown = 0
    for uid, f, va, vb in d["changed"]:
        if f.startswith(("Статус", "Снимок", "Online", "Проверка")):
            continue  # шумные колонки — в файле-отчёте
        lines.append(f"✏️ <code>{esc(uid[-8:])}</code> {esc(f)}: "
                     f"«{esc(va[:30])}» → «{esc(vb[:30])}»")
        shown += 1
        if shown >= 20:
            lines.append("… остальное — в CSV-отчёте ниже")
            break
    if not (d["added"] or d["removed"] or d["changed"]):
        lines.append("✅ Различий нет.")
    send_chunks(chat, lines)
    if len(d["changed"]) + len(d["added"]) + len(d["removed"]) > 20:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["uid", "тип", "поле", "было", "стало"])
        for uid in d["added"]:
            w.writerow([uid, "добавлена", "", "", ""])
        for uid in d["removed"]:
            w.writerow([uid, "удалена", "", "", ""])
        for uid, f, va, vb in d["changed"]:
            w.writerow([uid, "изменение", f, va, vb])
        send_document(chat, ("﻿" + buf.getvalue()).encode("utf-8"),
                      f"diff_{time.strftime('%Y%m%d_%H%M')}.csv",
                      caption="🆚 Полный дифф")

HANDLERS = {"/backups": cmd_backups, "/diffxlsx": cmd_diffxlsx}
ALIASES = {"/бэкапы": "/backups"}
CALLBACKS = {"bkdel": cb_bkdel, "bkdely": cb_bkdely}
