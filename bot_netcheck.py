# -*- coding: utf-8 -*-
"""Волна H — self-check рабочей станции (390-392):
390 /netcheck — камерный адаптер (роли плавают — ищем по source-IP в
камерных /24), DadTransmits=0, Duplicate-адреса (ловушка proxy-ARP
FortiGate), on-link маршруты, метрика интерфейса, кто держит маршрут до
камер (не утёк ли в VPN/шлюз);
391 кнопка «🔧 Починить» — netsh-рецепт из памяти проекта (DadTransmits=0,
ifMetric=1, /25-маршруты) — ТОЛЬКО после подтверждения (запись в ОС);
392 /trmatrix — traceroute-матрица до камерных подсетей (напрямую/через
шлюз = утёк в туннель); 393 /ipplan и 394 /dupip — IP-план и дубли IP.
Диагностика — subprocess netsh/route/tracert, read-only кроме 391."""
import re
import subprocess

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_metrics as mx
import bot_sw_api as sw
from bot_tg import send, send_chunks, chat_action, answer_cq
from bot_util import log, log_exc, esc

_confirm = sw.Confirm()
_SUB = dict(capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _run(args, timeout=20) -> str:
    """netsh на этой машине пишет UTF-8, классика — cp866: пробуем оба."""
    try:
        raw = subprocess.run(args, timeout=timeout, **_SUB).stdout or b""
    except Exception as e:
        return f"__err__ {e}"
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp866", errors="replace")


def cam_prefixes() -> list:
    return list(st.cget("cam_subnets") or []) + list(st.cget("sw_subnets") or [])


# ---------- парсеры (чистые, тестируются) ----------
def parse_ipcfg_addrs(text: str) -> dict:
    """`netsh interface ipv4 show addresses` -> {ifname: [ip, ...]}."""
    out: dict = {}
    cur = None
    for ln in (text or "").splitlines():
        # заголовок секции: Настройка/Конфигурация интерфейса "Ethernet 4"
        m = re.match(r'.*"([^"]+)"\s*$', ln)
        if m and not ln.strip().startswith('"'):
            cur = m.group(1)
            out.setdefault(cur, [])
            continue
        # строка адреса заканчивается голым IP («Префикс подсети» — маской)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s*$", ln)
        if m and cur:
            out[cur].append(m.group(1))
    return out


def parse_dad(text: str):
    """`netsh interface ipv4 show interface <if>` -> DadTransmits или None.
    RU-локаль: «Передачи в рамках обнаружения повторяющихся адресов»."""
    m = re.search(r"(?:DAD Transmits|DadTransmits|"
                  r"повторяющихся адресов)\s*:?\s*(\d+)", text or "", re.I)
    return int(m.group(1)) if m else None


def parse_metric(text: str):
    """Метрика интерфейса (строка «Метрика : 1» / «Metric : 1»)."""
    m = re.search(r"^\s*(?:Метрика|Metric|InterfaceMetric)\s*:\s*(\d+)",
                  text or "", re.I | re.M)
    return int(m.group(1)) if m else None


def parse_duplicates(text: str) -> list:
    """PowerShell Get-NetIPAddress table -> IP в состоянии Duplicate/Tentative
    (link-local 169.254.x не считаем — это норма APIPA)."""
    out = []
    for ln in (text or "").splitlines():
        m = re.match(r"\s*(\d+\.\d+\.\d+\.\d+).*(Duplicate|Tentative)", ln)
        if m and not m.group(1).startswith("169.254."):
            out.append((m.group(1), m.group(2)))
    return out


def parse_tracert_first_hop(text: str):
    """Первый отвечший hop tracert или None."""
    for ln in (text or "").splitlines():
        m = re.match(r"\s*1\s+.*?(\d+\.\d+\.\d+\.\d+)\s*$", ln)
        if m:
            return m.group(1)
    return None


# ---------- 390: /netcheck ----------
def find_cam_iface():
    """(ifname, [камерные IP на нём]) — адаптер с source-IP в камерных /24."""
    addrs = parse_ipcfg_addrs(_run(["netsh", "interface", "ipv4",
                                    "show", "addresses"]))
    best = (None, [])
    for ifname, ips in addrs.items():
        cams = [i for i in ips
                if any(i.startswith(p + ".") for p in cam_prefixes())]
        if len(cams) > len(best[1]):
            best = (ifname, cams)
    return best


def netcheck_report() -> tuple:
    """(строки отчёта, список проблем) — 390."""
    lines, problems = [], []
    ifname, cam_ips = find_cam_iface()
    if not ifname:
        return (["🔴 Камерный адаптер НЕ найден (нет source-IP в камерных "
                 "/24) — проверь кабель/адаптер (роли плавают!)"],
                ["no_iface"])
    lines.append(f"🖧 Камерный адаптер: <b>{esc(ifname)}</b> · source-IP: "
                 f"{len(cam_ips)}")
    missing = [p for p in cam_prefixes()
               if not any(i.startswith(p + ".") for i in cam_ips)]
    if missing:
        problems.append("missing_ips")
        lines.append(f"⚠️ Нет source-IP в: {esc(', '.join(missing))} — эти /24 "
                     f"будут «невидимы» (камеры без шлюза, нужен IP в каждой)")
    ifc = _run(["netsh", "interface", "ipv4", "show", "interface", ifname])
    dad, metric = parse_dad(ifc), parse_metric(ifc)
    if dad != 0:
        problems.append("dad")
        lines.append(f"🔴 DadTransmits={dad} (нужно 0!) — proxy-ARP FortiGate "
                     f"ломает DAD → адреса Duplicate → нет on-link маршрутов")
    else:
        lines.append("✅ DadTransmits=0 (защита от proxy-ARP)")
    if metric is not None and metric > 5:
        problems.append("metric")
        lines.append(f"⚠️ Метрика интерфейса {metric} (нужно 1, у Amnezia 5 — "
                     f"иначе туннель перехватит камерные маршруты)")
    else:
        lines.append(f"✅ Метрика интерфейса: {metric}")
    dup = parse_duplicates(_run(
        ["powershell", "-NoProfile", "-Command",
         "Get-NetIPAddress -AddressFamily IPv4 | "
         "Format-Table IPAddress,AddressState -HideTableHeaders"], 25))
    if dup:
        problems.append("duplicate")
        lines.append("🔴 Адреса в Duplicate/Tentative (ложный DAD): "
                     + ", ".join(f"<code>{i}</code>({s})" for i, s in dup[:6]))
    else:
        lines.append("✅ Duplicate-адресов нет")
    # кто держит маршрут до камер
    leak = []
    for p in (st.cget("cam_subnets") or [])[:4]:
        out = _run(["powershell", "-NoProfile", "-Command",
                    f"(Find-NetRoute -RemoteIPAddress {p}.50)[0]."
                    f"InterfaceAlias"], 20)
        alias = (out or "").strip().splitlines()[-1].strip() if out.strip() else "?"
        mark = "✅" if alias == ifname else "🔴"
        if alias != ifname:
            leak.append(p)
            problems.append("route")
        lines.append(f"{mark} маршрут до {p}.x → {esc(alias)}")
    return lines, sorted(set(problems))


def cmd_netcheck(chat, arg="", reply_to=None):
    chat_action(chat)
    lines, problems = netcheck_report()
    head = ("✅ <b>Сеть рабочей станции в норме</b> (390)" if not problems
            else f"⚠️ <b>Netcheck: проблем {len(problems)}</b> (390)")
    kb = None
    if problems and problems != ["no_iface"]:
        _confirm.put("fix", problems)
        kb = {"inline_keyboard": [[
            {"text": "🔧 Починить (netsh, с подтверждением)",
             "callback_data": "netfix:fix"}]]}
    send(chat, head + "\n" + "\n".join(lines), markup=kb, reply_to=reply_to)


# ---------- 391: починка ----------
def fix_commands(ifname: str) -> list:
    """Команды рецепта из памяти проекта: DadTransmits=0, метрика 1,
    /25-маршруты на камерный адаптер."""
    cmds = [["netsh", "interface", "ipv4", "set", "interface", ifname,
             "dadtransmits=0"],
            ["netsh", "interface", "ipv4", "set", "interface", ifname,
             "metric=1"]]
    for p in cam_prefixes():
        for half in ("0", "128"):
            cmds.append(["netsh", "interface", "ipv4", "add", "route",
                         f"{p}.{half}/25", ifname, "store=persistent"])
    return cmds


def cb_netfix(chat, cq, payload):
    problems = _confirm.take(payload)
    if not problems:
        answer_cq(cq.get("id"), "⌛ Устарело — повтори /netcheck")
        return
    answer_cq(cq.get("id"), "🔧 Чиню…")
    ifname, _ips = find_cam_iface()
    if not ifname:
        send(chat, "❌ Камерный адаптер не найден — чинить нечего.")
        return
    log(f"netcheck: починка по подтверждению владельца, adapter={ifname}")
    ok, fail = 0, 0
    for cmd in fix_commands(ifname):
        out = _run(cmd, 20)
        if out.startswith("__err__") or "требует повышения" in out.lower() \
                or "elevation" in out.lower():
            fail += 1
        else:
            ok += 1
    lines2, probs2 = netcheck_report()
    send(chat, f"🔧 <b>Починка (391)</b>: команд ок {ok}, ошибок {fail}"
               + ("\n⚠️ Часть netsh требует прав администратора — запусти "
                  "бот/консоль elevated" if fail else "")
               + "\n\nПовторная проверка:\n" + "\n".join(lines2))


# ---------- 392: /trmatrix ----------
def cmd_trmatrix(chat, arg="", reply_to=None):
    chat_action(chat)
    send(chat, "🧭 Трассирую до камерных подсетей (по 1 IP, до 4 хопов)…",
         silent=True, reply_to=reply_to)
    ifname, cam_ips = find_cam_iface()
    lines = ["🧭 <b>Traceroute-матрица</b> (392):"]
    for p in cam_prefixes():
        out = _run(["tracert", "-d", "-h", "4", "-w", "800", f"{p}.50"], 40)
        hop = parse_tracert_first_hop(out)
        if hop is None:
            verdict = "❔ нет ответа на первом хопе"
        elif hop.startswith(p + "."):
            verdict = f"✅ напрямую (L2, {hop})"
        else:
            verdict = f"🔴 через {hop} — трафик УШЁЛ в шлюз/туннель!"
        lines.append(f"• {p}.x: {verdict}")
    lines.append("Если «через шлюз» — /netcheck и кнопка «Починить» (391)")
    send_chunks(chat, lines)


# ---------- 393: /ipplan ----------
def cmd_ipplan(chat, arg="", reply_to=None):
    chat_action(chat)
    used: dict = {}
    for c in inv.cams():
        ip = c.get("ip")
        if ip and net.valid_ip(ip):
            used.setdefault(ip.rsplit(".", 1)[0], set()).add(int(ip.rsplit(".", 1)[1]))
    try:
        import bot_health as bh
        alive = {ip for ip, e in bh.snapshot()["ips"].items() if e.get("ok")}
    except Exception:
        alive = set()
    lines = ["📋 <b>IP-план камерных подсетей</b> (393):"]
    for sub in st.cget("cam_subnets") or []:
        u = used.get(sub, set())
        free = 254 - len(u)
        on = sum(1 for i in u if f"{sub}.{i}" in alive)
        # фрагментация: самый длинный свободный диапазон
        octets = sorted(u)
        gap, prev, best = 0, 0, (0, 0)
        for o in octets + [255]:
            if o - prev - 1 > best[0]:
                best = (o - prev - 1, prev + 1)
            prev = o
        lines.append(f"• <code>{sub}.0/24</code>: занято {len(u)} "
                     f"(онлайн {on}) · свободно {free} · макс. свободный "
                     f"блок {best[0]} с .{best[1]}")
    lines.append("Свободные адреса точечно: /free_ip · дубли: /dupip")
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- 394: /dupip ----------
def cmd_dupip(chat, arg="", reply_to=None):
    chat_action(chat)
    arp = net.arp_table()
    lines = ["👯 <b>Кандидаты в дубли IP</b> (394):"]
    n = 0
    for c in inv.cams():
        ip, nm = c.get("ip"), c.get("nmac")
        if not ip or not nm:
            continue
        live = arp.get(ip)
        if live and inv.norm_mac(live) != nm:
            n += 1
            if n <= 20:
                lines.append(f"• <code>{ip}</code> {esc(c.get('name') or '')}: "
                             f"в сети <code>{esc(live)}</code> "
                             f"({esc(net.vendor(live))}), в инвентаре "
                             f"<code>{esc(c.get('mac'))}</code>")
    ev = mx.events(kind="arp_flap", days=7)
    lines.append(f"Итого MAC≠инвентарь по ARP: {n} · ARP-флаппинг за 7 дн. "
                 f"(330): {len(ev)}")
    if not n and not ev:
        lines.append("дублей не видно ✅ (ARP-кэш покрывает не всю сеть — "
                     "прогони /find по подсети для полноты)")
    send_chunks(chat, lines)


HANDLERS = {"/netcheck": cmd_netcheck, "/trmatrix": cmd_trmatrix,
            "/ipplan": cmd_ipplan, "/dupip": cmd_dupip}
ALIASES = {"/сетьпк": "/netcheck", "/трасса": "/trmatrix"}
CALLBACKS = {"netfix": cb_netfix}
