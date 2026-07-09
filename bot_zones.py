# -*- coding: utf-8 -*-
"""Волна D — геопривязка (151-158, 184):
151 parse_cam_name («AS-7C.01» → корпус 7C, номер 1), 152 /floor,
153 /zone (именованные зоны в zones.json: маски/IP/имена), 154 /zoneshot,
155 /zonediag, 156 /zonestat (рейтинг зон по падениям), 157 /plan (PNG плана
из конфига floor_plans), 158 /switchcams, 184 /route (порядок обхода).
Пароли камер НЕ меняются; всё read-only, состояние — zones.json."""
import re
import time
import fnmatch

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_store as store
from bot_tg import send, send_chunks, send_photo, chat_action
from bot_util import log, esc

_re_cache = {"src": None, "re": None}


def _name_re():
    src = st.cget("name_regex")
    if _re_cache["src"] != src:
        _re_cache["re"] = re.compile(src, re.IGNORECASE)
        _re_cache["src"] = src
    return _re_cache["re"]


def parse_cam_name(name):
    """151: 'AS-7C.01' → {'sys':'AS','bld':'7C','num':1}; None — не по схеме.
    Regex — в конфиге (name_regex), группы sys/bld/num."""
    m = _name_re().match(str(name or "").strip())
    if not m:
        return None
    bld = (m.group("bld") or "").replace(" ", "").upper()
    try:
        num = int(m.group("num"))
    except (TypeError, ValueError):
        num = None
    return {"sys": (m.group("sys") or "").upper(), "bld": bld, "num": num,
            "raw": str(name)}


_FLOOR_RE1 = re.compile(r"(-?\d+)\s*-?\s*эт", re.IGNORECASE)
_FLOOR_RE2 = re.compile(r"эт(?:аж)?\.?\s*(-?\d+)", re.IGNORECASE)


def floor_of(rec) -> object:
    """Этаж из «Расположение» («3 этаж», «этаж -1») или None."""
    loc = str(rec.get("location") or "")
    m = _FLOOR_RE1.search(loc) or _FLOOR_RE2.search(loc)
    return int(m.group(1)) if m else None


def cam_meta(rec) -> dict:
    p = parse_cam_name(rec.get("name")) or {}
    p["floor"] = floor_of(rec)
    return p


def _monitored(rec) -> bool:
    try:
        from bot_lifecycle import is_monitored
        return is_monitored(rec.get("ip") or "", rec.get("name"))
    except Exception:
        return True


# ---------- 153: зоны ----------
def _zpath():
    return st.cget("zones_path")


def zones() -> dict:
    return store.jload(_zpath(), {})


def resolve_zone(arg: str):
    """Имя зоны без учёта регистра → каноническое имя или None."""
    a = (arg or "").strip().lower()
    for z in zones():
        if z.lower() == a:
            return z
    return None


def _item_match(item: str, rec) -> bool:
    it = item.strip()
    if not it:
        return False
    if net.valid_ip(it):
        return rec.get("ip") == it
    name = str(rec.get("name") or "")
    if "*" in it or "?" in it:
        if fnmatch.fnmatch(name.lower(), it.lower()):
            return True
        # нормализованное сравнение: «AS-7C.*» → «as7c*» против nname «as7c01»
        toks = re.split(r"([*?])", it)
        pat = "".join(t if t in "*?" else inv.norm_name(t) for t in toks)
        return fnmatch.fnmatch(rec.get("nname") or inv.norm_name(name), pat)
    if re.fullmatch(r"\d+[A-Za-zА-ЯЁа-яё]", it):  # корпус: «7C»
        p = parse_cam_name(name)
        return bool(p and p["bld"] == it.upper())
    return inv.norm_name(name) == inv.norm_name(it)


def zone_cams(zname: str) -> list:
    """Записи инвентаря, входящие в зону (демонтированные исключены, 196)."""
    z = zones().get(zname) or {}
    items = z.get("items") or []
    res, seen = [], set()
    for rec in inv.cams():
        key = rec.get("ip") or rec.get("nname")
        if not key or key in seen:
            continue
        if any(_item_match(it, rec) for it in items):
            if _monitored(rec):
                res.append(rec)
                seen.add(key)
    return res


def zone_ips(zname: str) -> list:
    return [r["ip"] for r in zone_cams(zname) if r.get("ip")]


def cmd_zone(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    sub = parts[0].lower() if parts else ""
    if sub in ("create", "создать") and len(parts) >= 2:
        name = parts[1]
        def _mk(z):
            if name in z:
                return None
            z[name] = {"items": [], "created": time.strftime("%Y-%m-%d %H:%M")}
            return z
        before = zones()
        store.jupdate(_zpath(), {}, _mk)
        send(chat, (f"Зона «{esc(name)}» уже есть — /zone add {esc(name)} …"
                    if name in before else
                    f"✅ Зона «{esc(name)}» создана. Добавь камеры:\n"
                    f"<code>/zone add {esc(name)} AS-7C.*</code>"), reply_to=reply_to)
        return
    if sub in ("add", "добавить") and len(parts) >= 3:
        name = resolve_zone(parts[1]) or parts[1]
        items = [t.strip() for t in " ".join(parts[2:]).replace(",", " ").split()
                 if t.strip()]
        def _add(z):
            e = z.setdefault(name, {"items": [],
                                    "created": time.strftime("%Y-%m-%d %H:%M")})
            for it in items:
                if it not in e["items"]:
                    e["items"].append(it)
            return z
        store.jupdate(_zpath(), {}, _add)
        n = len(zone_cams(name))
        send(chat, f"✅ В зоне «{esc(name)}» теперь {len(zones()[name]['items'])} "
                   f"правил → <b>{n}</b> камер. Состав: /zone show {esc(name)}",
             reply_to=reply_to)
        return
    if sub in ("del", "rm", "удалить") and len(parts) >= 2:
        name = resolve_zone(parts[1])
        if not name:
            send(chat, f"Зоны «{esc(parts[1])}» нет.", reply_to=reply_to)
            return
        item = " ".join(parts[2:]).strip()
        def _del(z):
            if item:
                z[name]["items"] = [i for i in z[name]["items"] if i != item]
            else:
                z.pop(name, None)
            return z
        store.jupdate(_zpath(), {}, _del)
        send(chat, (f"🗑 Из зоны «{esc(name)}» убрано «{esc(item)}»." if item
                    else f"🗑 Зона «{esc(name)}» удалена."), reply_to=reply_to)
        return
    if sub in ("show", "состав") and len(parts) >= 2:
        name = resolve_zone(parts[1])
        if not name:
            send(chat, f"Зоны «{esc(parts[1])}» нет. Список: /zone", reply_to=reply_to)
            return
        recs = zone_cams(name)
        z = zones()[name]
        lines = [f"🧭 <b>Зона «{esc(name)}»</b> — правила: "
                 + ", ".join(f"<code>{esc(i)}</code>" for i in z.get("items", []))]
        lines += [f"• <code>{esc(r.get('ip') or '—')}</code> {esc(r.get('name') or '')}"
                  for r in recs[:60]]
        if len(recs) > 60:
            lines.append(f"… и ещё {len(recs) - 60}")
        lines.append(f"Итого камер: <b>{len(recs)}</b>")
        send_chunks(chat, lines)
        return
    # без аргументов / list — обзор
    zs = zones()
    if not zs:
        send(chat, "🧭 Зон пока нет.\n"
                   "<code>/zone create атриум</code> — создать\n"
                   "<code>/zone add атриум AS-7C.*</code> — добавить маску/IP/имя\n"
                   "<code>/zone show атриум</code> · <code>/zone del атриум</code>",
             reply_to=reply_to)
        return
    lines = ["🧭 <b>Зоны</b> (единица массовых операций):"]
    for z in sorted(zs):
        lines.append(f"• <b>{esc(z)}</b> — {len(zone_cams(z))} камер "
                     f"({len(zs[z].get('items') or [])} правил)")
    lines.append("Команды: /zoneshot /zonediag /zonestat /route /patrol <зона>")
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- вспомогательное: набор камер по аргументу (зона | корпус) ----------
def cams_by_arg(arg: str):
    """(описание, [recs]) по аргументу: имя зоны или корпус вида 7C."""
    a = (arg or "").strip()
    z = resolve_zone(a)
    if z:
        return f"зона «{z}»", zone_cams(z)
    m = re.fullmatch(r"(\d+\s?[A-Za-zА-ЯЁа-яё])", a)
    if m:
        bld = m.group(1).replace(" ", "").upper()
        recs = [r for r in inv.cams()
                if (parse_cam_name(r.get("name")) or {}).get("bld") == bld
                and _monitored(r)]
        return f"корпус {bld}", recs
    return None, []


def _health_map() -> dict:
    try:
        import bot_health as bh
        return bh.snapshot()["ips"]
    except Exception:
        return {}


def _state_icon(hm, ip):
    e = hm.get(ip)
    if not e:
        return "❔"
    return "🟢" if e.get("ok") else "🔴"


# ---------- 152: /floor ----------
def cmd_floor(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    if not parts:
        blds = sorted({(parse_cam_name(r.get("name")) or {}).get("bld")
                       for r in inv.cams()
                       if parse_cam_name(r.get("name"))} - {None, ""})
        send(chat, "Камеры корпуса/этажа: <code>/floor 7C</code> или "
                   "<code>/floor 7C 3</code>\nКорпуса в инвентаре: "
                   + ", ".join(f"<code>{esc(b)}</code>" for b in blds[:40]),
             reply_to=reply_to)
        return
    what, recs = cams_by_arg(parts[0])
    if not recs:
        send(chat, f"По «{esc(parts[0])}» камер не нашёл (корпус вида 7C или зона).",
             reply_to=reply_to)
        return
    want_floor = None
    if len(parts) > 1:
        try:
            want_floor = int(parts[1])
        except ValueError:
            pass
    if want_floor is not None:
        recs = [r for r in recs if floor_of(r) == want_floor]
    hm = _health_map()
    recs.sort(key=lambda r: ((cam_meta(r).get("num") or 0), str(r.get("name"))))
    lines = [f"🏢 <b>{esc(what)}</b>"
             + (f" · этаж {want_floor}" if want_floor is not None else "")
             + f" — {len(recs)} камер:"]
    rows = []
    for r in recs[:60]:
        ip = r.get("ip") or "—"
        fl = floor_of(r)
        lines.append(f"{_state_icon(hm, ip)} <code>{esc(ip)}</code> "
                     f"{esc(r.get('name') or '')}"
                     + (f" · эт.{fl}" if fl is not None else "")
                     + (f" · {esc(r.get('location'))}" if r.get("location") else ""))
        if len(rows) < 6 and r.get("ip"):
            rows.append([{"text": f"📸 {r['ip']}", "callback_data": f"shot:{r['ip']}"},
                         {"text": "🩺", "callback_data": f"diag:{r['ip']}"}])
    if len(recs) > 60:
        lines.append(f"… и ещё {len(recs) - 60}")
    send_chunks(chat, lines)
    if rows:
        send(chat, "Действия по первым:", silent=True,
             markup={"inline_keyboard": rows})


# ---------- 184: /route ----------
def cmd_route(chat, arg="", reply_to=None):
    what, recs = cams_by_arg(arg)
    if not recs:
        send(chat, "Порядок обхода: <code>/route атриум</code> или "
                   "<code>/route 7C</code>", reply_to=reply_to)
        return
    def key(r):
        m = cam_meta(r)
        fl = m.get("floor")
        return (m.get("bld") or "яя", fl if fl is not None else 999,
                m.get("num") or 0, str(r.get("name")))
    recs = sorted(recs, key=key)
    hm = _health_map()
    lines = [f"🚶 <b>Маршрут обхода: {esc(what)}</b> — {len(recs)} камер"]
    cur = object()
    for r in recs:
        m = cam_meta(r)
        grp = (m.get("bld") or "?",
               m.get("floor") if m.get("floor") is not None else "?")
        if grp != cur:
            cur = grp
            lines.append(f"\n<b>Корпус {esc(grp[0])} · этаж {esc(grp[1])}</b>")
        ip = r.get("ip") or "—"
        lines.append(f"{_state_icon(hm, ip)} {esc(r.get('name') or '?')} "
                     f"<code>{esc(ip)}</code>"
                     + (f" · {esc(r.get('location'))}" if r.get("location") else ""))
    lines.append("\nФормализованный обход с отметками: /patrol <зона>")
    send_chunks(chat, lines)


# ---------- 158: /switchcams ----------
def cmd_switchcams(chat, arg="", reply_to=None):
    a = (arg or "").strip()
    if not a:
        send(chat, "Камеры на коммутаторе: <code>/switchcams 10.10.60.5</code> "
                   "или по имени свитча", reply_to=reply_to)
        return
    nn = inv.norm_name(a)
    recs = [r for r in inv.cams()
            if (net.valid_ip(a) and str(r.get("sw_ip") or "").strip() == a)
            or (not net.valid_ip(a) and nn and nn in inv.norm_name(r.get("switch")))]
    if not recs:
        send(chat, f"На «{esc(a)}» камер в инвентаре не найдено.", reply_to=reply_to)
        return
    hm = _health_map()
    def pkey(r):
        p = str(r.get("port") or "")
        d = re.sub(r"\D", "", p)
        return int(d) if d else 999
    recs.sort(key=pkey)
    off = [r for r in recs if hm.get(r.get("ip"), {}).get("ok") is False]
    lines = [f"🔌 <b>Камеры на {esc(a)}</b> — {len(recs)} "
             f"(перед работами предупреди, что лягут):"]
    for r in recs[:60]:
        ip = r.get("ip") or "—"
        lines.append(f"{_state_icon(hm, ip)} п.{esc(r.get('port') or '?')} — "
                     f"{esc(r.get('name') or '?')} <code>{esc(ip)}</code>")
    if off:
        lines.append(f"⚠️ уже офлайн: {len(off)}")
    lines.append("Окно работ без алертов: /maint <свитч|зона> <часы>")
    send_chunks(chat, lines)


# ---------- 157: /plan ----------
def cmd_plan(chat, arg="", reply_to=None):
    plans = dict(st.cget("floor_plans") or {})
    key = (arg or "").strip().upper().replace(" ", "")
    if not key or key not in {k.upper() for k in plans}:
        if not plans:
            send(chat, "🗺 Планы этажей не настроены: добавь в конфиг "
                       "<code>floor_plans</code> = {\"7C-1\": \"путь к PNG\"}.",
                 reply_to=reply_to)
        else:
            send(chat, "Есть планы: " + ", ".join(
                f"<code>{esc(k)}</code>" for k in sorted(plans)), reply_to=reply_to)
        return
    path = next(v for k, v in plans.items() if k.upper() == key)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        send(chat, f"❌ Файл плана {esc(key)} не читается: {esc(e)}", reply_to=reply_to)
        return
    chat_action(chat, "upload_photo")
    send_photo(chat, data, caption=f"🗺 План {key}")


# ---------- 154: /zoneshot ----------
def cmd_zoneshot(chat, arg="", reply_to=None):
    what, recs = cams_by_arg(arg)
    ips = [r["ip"] for r in recs if r.get("ip")][:int(st.cget("zone_shot_max"))]
    if not ips:
        send(chat, "Снимки зоны: <code>/zoneshot атриум</code> (или корпус 7C)",
             reply_to=reply_to)
        return
    import bot_handlers_media as hm
    send(chat, f"📸 Снимаю {len(ips)} камер ({esc(what)}) — альбомами по 10…",
         silent=True, reply_to=reply_to)
    for i in range(0, len(ips), 10):
        hm._send_album(chat, ips[i:i + 10])


# ---------- 155: /zonediag ----------
def cmd_zonediag(chat, arg="", reply_to=None):
    what, recs = cams_by_arg(arg)
    recs = [r for r in recs if r.get("ip")]
    if not recs:
        send(chat, "Сводная диагностика: <code>/zonediag атриум</code>",
             reply_to=reply_to)
        return
    chat_action(chat)
    send(chat, f"🩺 Проверяю {len(recs)} камер ({esc(what)})…", silent=True,
         reply_to=reply_to)
    ips = [r["ip"] for r in recs]
    pmap = net.probe_many(ips, ports=(80, 554))
    ok_n, rows = 0, []
    for r in recs:
        ip = r["ip"]
        p = pmap.get(ip) or []
        ok = bool(p)
        ok_n += 1 if ok else 0
        rows.append(f"{'OK ' if ok else '!! '}{(r.get('name') or '?')[:14]:<14} "
                    f"{ip:<15} п:{','.join(map(str, p)) or '—'}")
    head = (f"🩺 <b>{esc(what)}</b>: ОК <b>{ok_n}</b> / проблем "
            f"<b>{len(recs) - ok_n}</b>")
    buf = []
    for row in rows:
        buf.append(row)
        if len("\n".join(buf)) > 3200:
            send(chat, head + f"\n<pre>{esc(chr(10).join(buf))}</pre>")
            buf, head = [], "…"
    if buf:
        send(chat, head + f"\n<pre>{esc(chr(10).join(buf))}</pre>")


# ---------- 156: /zonestat ----------
def cmd_zonestat(chat, arg="", reply_to=None):
    try:
        days = max(1, min(int(arg), 30))
    except (TypeError, ValueError):
        days = 7
    zs = zones()
    if not zs:
        send(chat, "Зон нет — создай: /zone create <имя>", reply_to=reply_to)
        return
    import bot_health as bh
    ev = bh.history_events(days)
    hm = _health_map()
    stat = []
    for z in zs:
        ips = set(zone_ips(z))
        if not ips:
            continue
        downs = sum(1 for e in ev if e.get("ev") == "down" and e.get("ip") in ips)
        dt = sum(int(e.get("dur") or 0) for e in ev
                 if e.get("ev") == "up" and e.get("ip") in ips)
        off_now = sum(1 for ip in ips if hm.get(ip, {}).get("ok") is False)
        stat.append((z, len(ips), downs, dt, off_now))
    stat.sort(key=lambda x: (-x[2], -x[3]))
    lines = [f"🏆 <b>Рейтинг зон по проблемности за {days} дн.</b> "
             f"(падения · даунтайм · офлайн сейчас):"]
    for i, (z, n, downs, dt, off) in enumerate(stat, 1):
        mark = "🔴" if downs else "🟢"
        lines.append(f"{i}. {mark} <b>{esc(z)}</b> ({n} кам): падений {downs} · "
                     f"даунтайм {dt // 3600}ч {(dt % 3600) // 60}м · офлайн {off}")
    if not stat:
        lines.append("Во всех зонах пусто (нет камер с IP).")
    send_chunks(chat, lines)


HANDLERS = {
    "/zone": cmd_zone, "/floor": cmd_floor, "/route": cmd_route,
    "/switchcams": cmd_switchcams, "/plan": cmd_plan,
    "/zoneshot": cmd_zoneshot, "/zonediag": cmd_zonediag,
    "/zonestat": cmd_zonestat,
}
ALIASES = {
    "/зона": "/zone", "/этаж": "/floor", "/маршрут": "/route",
    "/свитчкам": "/switchcams", "/план": "/plan",
}
CALLBACKS = {}
