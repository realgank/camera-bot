# -*- coding: utf-8 -*-
r"""Волна H — конфиги свитчей и факты (371-373, 396-398):
371 плановый бэкап в config_backups\<ip>\<дата>: Cross-24 — канонический
JSON-снапшот конфиго-значимых GET (у web-API нет выгрузки сырого cfg,
SSH на парке выключен), Huawei — сырой display current-configuration;
372 конфиг-дрейф: unified diff свежего бэкапа против прошлого → алерт;
373 несохранённый конфиг — ЧАСТИЧНО (running vs startup у Cross-24 web-API
не читается; Huawei не сравнивается — честно помечено);
396 /portdesc — синк описаний портов из инвентаря (dry-run → подтверждение →
set.cgi БЕЗ save); 397 /facts_refresh — фоновая пересборка
_facts_switches.json (прежний срез → _facts_switches.prev.json, его же
использует /reconcile moves); 398 /facts_diff — дифф снапшотов фактов."""
import os
import json
import time
import difflib
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor

import bot_state as st
import bot_inventory as inv
import bot_metrics as mx
import bot_sw_api as sw
from bot_tg import send, send_chunks, send_document, chat_action, answer_cq
from bot_util import log, log_exc, esc

_confirm = sw.Confirm()
_refresh_running = threading.Lock()

# конфиго-значимые GET для JSON-снапшота Cross-24 (без счётчиков/времени)
_C24_SNAP_CMDS = ("sys_sysinfo", "sys_line", "stp_global", "storm_control",
                  "port_port", "poe_poe", "time_time")
_VOLATILE = ("sysUpTime", "sysCurrTime", "sec", "currIpv6", "time", "timeStr",
             "portPower", "portVoltage", "portCurrent", "devPower", "devTemp",
             "operSpeed", "operDuplex", "operStatus", "operFlowCtrl")


def strip_volatile(obj):
    """Убирает из снапшота изменчивые поля (аптайм, ватты, oper-статусы) —
    чтобы дрейф-дифф ловил именно конфиг."""
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items()
                if k not in _VOLATILE}
    if isinstance(obj, list):
        return [strip_volatile(x) for x in obj]
    return obj


def _bdir(ip: str) -> str:
    d = os.path.join(st.cget("config_backups_dir"), ip)
    os.makedirs(d, exist_ok=True)
    return d


def _snapshots(ip: str) -> list:
    d = os.path.join(st.cget("config_backups_dir"), ip)
    try:
        return sorted(os.path.join(d, f) for f in os.listdir(d))
    except OSError:
        return []


def backup_one(ip: str, kind: str) -> str:
    """Снимает бэкап, пишет файл, возвращает путь. 371."""
    today = datetime.date.today().isoformat()
    if kind == "huawei":
        out = sw.huawei_cli(ip, ["display current-configuration"], timeout=90)
        text = sw.hw_section(out, "display current-configuration") or out
        path = os.path.join(_bdir(ip), f"{today}.cfg")
    else:
        snap = {}
        for cmd in _C24_SNAP_CMDS:
            try:
                snap[cmd] = strip_volatile(sw.cross24_get(ip, cmd))
            except Exception as e:
                snap[cmd] = {"_err": str(e)[:100]}
        text = json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True)
        path = os.path.join(_bdir(ip), f"{today}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    # ротация
    snaps = _snapshots(ip)
    for old in snaps[:-int(st.cget("sw_backup_keep"))]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


def drift_check(ip: str) -> str:
    """372: unified diff двух последних бэкапов ('' — нет изменений)."""
    snaps = _snapshots(ip)
    if len(snaps) < 2:
        return ""
    try:
        with open(snaps[-2], encoding="utf-8") as f:
            a = f.read().splitlines()
        with open(snaps[-1], encoding="utf-8") as f:
            b = f.read().splitlines()
    except OSError:
        return ""
    if a == b:
        return ""
    return "\n".join(difflib.unified_diff(
        a, b, fromfile=os.path.basename(snaps[-2]),
        tofile=os.path.basename(snaps[-1]), lineterm=""))[:60000]


def _tick_backup() -> None:
    """Суточный бэкап малыми порциями начиная с sw_backup_hour."""
    if not st.cget("sw_backup_enabled"):
        return
    if datetime.datetime.now().hour < int(st.cget("sw_backup_hour")):
        return
    if not mx.due("sw_backup", 30, first_delay_s=600):
        return
    today = datetime.date.today().isoformat()
    targets = [(e["ip"], "cross24") for e in sw.facts() if e["ok"]] + \
              [(str(ip), "huawei") for ip in st.cget("hw_switches") or []]
    done = 0
    for ip, kind in targets:
        if done >= int(st.cget("sw_backup_batch")):
            break
        snaps = _snapshots(ip)
        if snaps and os.path.basename(snaps[-1]).startswith(today):
            continue
        try:
            backup_one(ip, kind)
            done += 1
            d = drift_check(ip)
            if d:
                host = (sw.by_ip(ip) or {}).get("host") or ip
                if mx.event_add(ip, "cfg_drift", d[:200]):
                    mx.owner_alert(f"📝 <b>Конфиг изменился</b>: {esc(host)} "
                                   f"<code>{ip}</code> (372) — дифф: "
                                   f"/swbackup diff {ip}")
        except Exception as e:
            log(f"sw_cfg: бэкап {ip} не снялся: {e}")
    if done:
        log(f"sw_cfg: бэкап-тик — снято {done} конфигов")


# ---------- /swbackup ----------
def cmd_swbackup(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    if parts and parts[0] == "diff" and len(parts) > 1:
        r = sw.find_switch(parts[1])
        if not r:
            send(chat, "Не нашёл свитч.", reply_to=reply_to)
            return
        d = drift_check(r["ip"])
        if not d:
            send(chat, f"Дрейфа нет (или меньше двух бэкапов) — "
                       f"<code>{r['ip']}</code>", reply_to=reply_to)
            return
        send_document(chat, d.encode("utf-8"),
                      f"drift_{r['ip']}_{datetime.date.today()}.diff",
                      caption=f"📝 Конфиг-дрейф {r['ip']} (372)")
        return
    r = sw.find_switch(parts[0]) if parts else None
    if not r:
        n = sum(len(_snapshots(e["ip"])) for e in sw.facts())
        send(chat, f"💾 <b>Бэкапы конфигов</b> (371): {n} снапшотов в "
                   f"<code>config_backups\\</code>\n"
                   f"/swbackup <code>ip</code> — снять сейчас · "
                   f"/swbackup diff <code>ip</code> — дрейф (372)\n"
                   f"Плановый: ежесуточно с {st.cget('sw_backup_hour')}:00, "
                   f"порциями по {st.cget('sw_backup_batch')}.\n"
                   f"373 (running≠startup): у Cross-24 web-API не читается, "
                   f"честно не реализовано.", reply_to=reply_to)
        return
    chat_action(chat)
    try:
        path = backup_one(r["ip"], r.get("kind") or "cross24")
        with open(path, "rb") as f:
            data = f.read()
        send_document(chat, data, os.path.basename(f"{r['ip']}_{os.path.basename(path)}"),
                      caption=f"💾 Бэкап {r.get('host') or r['ip']} "
                              f"({len(data) // 1024} КБ)")
        d = drift_check(r["ip"])
        if d:
            send(chat, f"📝 Есть дрейф против прошлого бэкапа — "
                       f"/swbackup diff {r['ip']}")
    except Exception as e:
        send(chat, f"❌ Бэкап {esc(r['ip'])} не снялся: {esc(str(e)[:150])}")


# ---------- 397: /facts_refresh ----------
def _collect_one(ip: str) -> dict:
    e = {"ip": ip, "ok": False, "err": None, "sys": None, "lldp": [],
         "mac_table": [], "port_density": {}, "vlan_density": {}}
    try:
        e["sys"] = sw.cross24_get(ip, "sys_sysinfo")
        try:
            e["lldp"] = (sw.cross24_get(ip, "lldp_neighbor")
                         .get("neighbors") or [])
        except Exception:
            pass
        mt = sw.cross24_get(ip, "mac_dynamic").get("entries") or []
        e["mac_table"] = [{"vlan": m.get("vlan"), "mac": m.get("macAddr"),
                           "port": m.get("port")} for m in mt]
        for m in e["mac_table"]:
            p = m.get("port")
            e["port_density"][p] = e["port_density"].get(p, 0) + 1
            k = f"{p}|{m.get('vlan')}"
            e["vlan_density"][k] = e["vlan_density"].get(k, 0) + 1
        e["ok"] = True
    except Exception as ex:
        e["err"] = str(ex)[:300]
    return e


def _do_refresh(chat) -> None:
    try:
        ips = sorted({e["ip"] for e in sw.facts()}
                     | {r["ip"] for r in sw.sheet_switches()},
                     key=lambda x: tuple(int(o) for o in x.split(".")))
        t0 = time.time()
        res = []
        with ThreadPoolExecutor(max_workers=int(st.cget("facts_refresh_workers")),
                                thread_name_prefix="factsrf") as ex:
            for i, e in enumerate(ex.map(_collect_one, ips), 1):
                res.append(e)
                if i % 15 == 0:
                    send(chat, f"⏳ /facts_refresh: {i}/{len(ips)}…", silent=True)
        path = st.cget("facts_switches")
        prev = st.cget("facts_prev_path")
        try:
            if os.path.exists(path):
                import shutil
                shutil.copy2(path, prev)
        except Exception:
            log_exc("sw_cfg: не смог сохранить prev-срез фактов")
        for i, e in enumerate(res):
            e["row"] = i + 3    # совместимость со старой схемой
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        ok = sum(1 for e in res if e["ok"])
        send(chat, f"✅ <b>/facts_refresh</b>: {ok}/{len(res)} свитчей за "
                   f"{time.time() - t0:.0f}с. Прежний срез → "
                   f"<code>_facts_switches.prev.json</code>.\n"
                   f"Дифф: /facts_diff · переезды камер: /reconcile moves")
        log(f"sw_cfg: факты пересобраны {ok}/{len(res)}")
    except Exception as e:
        log_exc("sw_cfg: facts_refresh")
        send(chat, f"❌ /facts_refresh упал: {esc(str(e)[:150])}")
    finally:
        _refresh_running.release()


def cmd_facts_refresh(chat, arg="", reply_to=None):
    if (arg or "").strip().lower() != "go":
        send(chat, f"🔄 <b>Пересборка фактов свитчей</b> (397): опрос "
                   f"~{len(sw.registry())} Cross-24 (web-API, "
                   f"{st.cget('facts_refresh_workers')} потоков, ~2-4 мин).\n"
                   f"Сейчас: {esc(sw.age_note())}\n"
                   f"Запуск: <code>/facts_refresh go</code>", reply_to=reply_to)
        return
    if not _refresh_running.acquire(blocking=False):
        send(chat, "⏳ Пересборка уже идёт.", reply_to=reply_to)
        return
    send(chat, "🔄 Стартовал фоновую пересборку фактов…", reply_to=reply_to)
    threading.Thread(target=_do_refresh, args=(chat,), daemon=True,
                     name="factsrefresh").start()


# ---------- 398: /facts_diff ----------
def facts_diff() -> dict:
    """Сравнение текущих фактов с prev-срезом."""
    try:
        with open(st.cget("facts_prev_path"), encoding="utf-8") as f:
            prev_raw = json.load(f)
    except Exception:
        return {}
    cur = {e["ip"]: e for e in sw.facts()}
    prev = {e["ip"]: sw.norm_switch(e) for e in prev_raw}
    out = {"new_sw": sorted(set(cur) - set(prev)),
           "gone_sw": sorted(set(prev) - set(cur)),
           "state": [], "macs_new": [], "macs_gone": [], "lldp": []}
    for ip in sorted(set(cur) & set(prev)):
        c, p = cur[ip], prev[ip]
        if c["ok"] != p["ok"]:
            out["state"].append((ip, p["ok"], c["ok"]))
        cm = {inv.norm_mac(m["mac"]): m["port"] for m in c["mac_table"]}
        pm = {inv.norm_mac(m["mac"]): m["port"] for m in p["mac_table"]}
        for m in set(cm) - set(pm):
            out["macs_new"].append((ip, m, cm[m]))
        for m in set(pm) - set(cm):
            out["macs_gone"].append((ip, m, pm[m]))
        cl = {(n["port"], n["id"]) for n in c["lldp"]}
        pl = {(n["port"], n["id"]) for n in p["lldp"]}
        for port, mid in cl ^ pl:
            out["lldp"].append((ip, port, mid, "+" if (port, mid) in cl else "-"))
    return out


def cmd_facts_diff(chat, arg="", reply_to=None):
    chat_action(chat)
    d = facts_diff()
    if not d:
        send(chat, "Prev-среза нет — сначала /facts_refresh go (398).",
             reply_to=reply_to)
        return
    lines = [f"🧾 <b>Дифф снапшотов фактов</b> (398) · {esc(sw.age_note())}",
             f"свитчи: +{len(d['new_sw'])} / -{len(d['gone_sw'])} · смена "
             f"доступности: {len(d['state'])}",
             f"MAC: появилось {len(d['macs_new'])}, пропало "
             f"{len(d['macs_gone'])} · LLDP-изменений: {len(d['lldp'])}"]
    for ip, was, now in d["state"][:10]:
        lines.append(f"• <code>{ip}</code>: {'🟢' if now else '🔴'} "
                     f"(было {'🟢' if was else '🔴'})")
    for ip, m, p in d["macs_new"][:12]:
        lines.append(f"➕ {ip} п.{esc(p)}: <code>{esc(m)}</code>")
    for ip, m, p in d["macs_gone"][:12]:
        lines.append(f"➖ {ip} п.{esc(p)}: <code>{esc(m)}</code>")
    for ip, port, mid, sign in d["lldp"][:10]:
        lines.append(f"{sign} LLDP {ip} {esc(port)}: <code>{esc(mid)}</code>")
    send_chunks(chat, lines)


# ---------- 396: /portdesc (запись с подтверждением) ----------
_LANG_MAP = {"Auto": "auto", "Disabled": "disable"}


def _adm(v: str) -> str:
    return _LANG_MAP.get(sw.lang_label(v), sw.lang_label(v)).lower()


def portdesc_plan(ip: str) -> list:
    """[(port, старое descp, новое имя камеры)] по фактам и инвентарю."""
    e = sw.by_ip(ip)
    if not e or not e["ok"]:
        return []
    import bot_topo
    ups = set(bot_topo.uplink_ports(ip))
    by_mac = {c["nmac"]: c for c in inv.cams() if c.get("nmac")}
    ports = sw.cross24_get(ip, "port_port").get("ports") or []
    plan = []
    per_port: dict = {}
    for m in e["mac_table"]:
        per_port.setdefault(m.get("port"), []).append(inv.norm_mac(m.get("mac")))
    for port, macs in per_port.items():
        idx = sw.port_index(port)
        if port in ups or idx is None or idx >= len(ports):
            continue
        cams = [by_mac[m] for m in macs if m in by_mac]
        if len(cams) != 1:
            continue
        name = str(cams[0].get("name") or "").strip()
        cur = ports[idx]
        if not name or not cur.get("adminStatus"):
            continue
        old = str(cur.get("descp") or "")
        if old != name[:32]:
            plan.append((port, old, name[:32], _adm(cur.get("adminSpeed")),
                         _adm(cur.get("adminDuplex")),
                         _adm(cur.get("adminFlowCtrl"))))
    return sorted(plan, key=lambda x: sw.port_index(x[0]) or 0)


def cmd_portdesc(chat, arg="", reply_to=None):
    r = sw.find_switch(arg)
    if not r or r.get("kind") != "cross24":
        send(chat, "Синк описаний портов из инвентаря (396): "
                   "<code>/portdesc 10.10.60.52</code> — dry-run превью, "
                   "запись только после подтверждения.", reply_to=reply_to)
        return
    chat_action(chat)
    try:
        plan = portdesc_plan(r["ip"])
    except Exception as e:
        send(chat, f"❌ Не смог прочитать порты: {esc(str(e)[:150])}")
        return
    if not plan:
        send(chat, f"Описания портов {esc(r.get('host') or r['ip'])} уже "
                   f"совпадают с инвентарём (или однозначных камер нет).",
             reply_to=reply_to)
        return
    _confirm.put(r["ip"], plan)
    lines = [f"📝 <b>Dry-run синка описаний</b> {esc(r.get('host') or r['ip'])} "
             f"(396): {len(plan)} портов"]
    lines += [f"• {esc(p)}: «{esc(old) or '—'}» → «{esc(new)}»"
              for p, old, new, *_x in plan[:15]]
    if len(plan) > 15:
        lines.append(f"… и ещё {len(plan) - 15}")
    lines.append("⚠️ SET port_portEdit сохраняет adminStatus=on и текущие "
                 "speed/duplex; save НЕ выполняется. Подтверди:")
    send(chat, "\n".join(lines), markup={"inline_keyboard": [[
        {"text": "✅ Записать описания", "callback_data": f"pdesc:{r['ip']}"},
        {"text": "✖️ Отмена", "callback_data": "cancel"}]]})


def cb_portdesc(chat, cq, payload):
    plan = _confirm.take(payload)
    if not plan:
        answer_cq(cq.get("id"), "⌛ Устарело — повтори /portdesc")
        return
    answer_cq(cq.get("id"), "📝 Записываю…")
    ok, fail = 0, []
    for port, _old, new, spd, dup, fc in plan:
        try:
            sw.cross24_set(payload, "port_portEdit",
                           {"portList": port, "descp": new,
                            "adminStatus": "on", "adminSpeed": spd or "auto",
                            "adminDuplex": dup or "auto",
                            "adminFlowCtrl": fc or "disable"})
            ok += 1
        except Exception as e:
            fail.append(f"{port}: {e}")
    log(f"sw_cfg: portdesc {payload}: ok={ok} fail={len(fail)} (без save)")
    send(chat, f"📝 <b>396</b> {esc(payload)}: записано {ok}, ошибок {len(fail)}\n"
               + "\n".join(f"❌ {esc(f)[:80]}" for f in fail[:6])
               + "\n⚠️ save не выполнялся (запрещён в этой волне).")


try:
    import bot_health as _bh
    _bh.MINUTE_TICKS.append(_tick_backup)
except Exception:
    pass

HANDLERS = {
    "/swbackup": cmd_swbackup, "/facts_refresh": cmd_facts_refresh,
    "/facts_diff": cmd_facts_diff, "/portdesc": cmd_portdesc,
}
ALIASES = {"/бэкапсвитч": "/swbackup", "/факты": "/facts_refresh"}
CALLBACKS = {"pdesc": cb_portdesc}
