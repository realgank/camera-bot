# -*- coding: utf-8 -*-
"""Волна H — Huawei VRP (S5736) транспорт и парсеры (вынос из bot_sw_api).
Транспорт: plink subprocess, интерактивный shell через stdin (exec-канал VRP
не работает), host-key TOFU в _sw_hostkeys.json, перебор паролей #/@.
Парсеры display-выводов — чистые функции, тестируются на живых образцах."""
import re
import subprocess

import bot_state as st
import bot_store as store
from bot_util import log_exc  # noqa: F401

_CF = getattr(subprocess, "CREATE_NO_WINDOW", 0)
import os as _os
HK_PATH = _os.path.join(st.BASE, "_sw_hostkeys.json")


class SwError(Exception):
    """Ошибка транспорта/логина коммутатора (общая с bot_sw_api)."""


# ---------- Huawei VRP через plink ----------
def plink_ok() -> bool:
    try:
        p = subprocess.run(["plink", "-V"], capture_output=True, text=True,
                           timeout=10, creationflags=_CF)
        return "plink" in (p.stdout + p.stderr).lower()
    except Exception:
        return False


def _hostkey(ip: str) -> str:
    """TOFU: fingerprint хост-ключа из -batch прогона, кэш в _sw_hostkeys.json."""
    hks = store.jload(HK_PATH, {})
    if hks.get(ip):
        return hks[ip]
    p = subprocess.run(
        ["plink", "-ssh", "-batch", "-pw", "x",
         f"{st.cget('hw_user')}@{ip}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=25, creationflags=_CF)
    m = re.search(r"(SHA256:[A-Za-z0-9+/=]+)", p.stderr or "")
    if not m:
        raise SwError(f"{ip}: не получил host-key ({(p.stderr or '')[:120]})")
    def _save(d):
        d[ip] = m.group(1)
        return d
    store.jupdate(HK_PATH, {}, _save)
    return m.group(1)


def huawei_cli(ip: str, commands, timeout: float = None) -> str:
    """Read/write-команды VRP интерактивным shell'ом (stdin), пейджер отключён.
    Перебирает пароли hw_passwords. Возвращает сырой stdout сессии."""
    if isinstance(commands, str):
        commands = [commands]
    timeout = timeout or float(st.cget("plink_timeout_s"))
    hk = _hostkey(ip)
    inp = "screen-length 0 temporary\n" + "\n".join(commands) + "\nquit\n"
    last = ""
    for pw in st.cget("hw_passwords") or []:
        try:
            p = subprocess.run(
                ["plink", "-ssh", "-batch", "-no-antispoof", "-hostkey", hk,
                 "-pw", pw, f"{st.cget('hw_user')}@{ip}"],
                input=inp, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, creationflags=_CF)
        except subprocess.TimeoutExpired:
            raise SwError(f"{ip}: plink таймаут {timeout:.0f}s")
        last = (p.stderr or "")[-200:]
        if p.returncode == 0 and p.stdout:
            return p.stdout
        if "Access denied" not in (p.stderr or ""):
            break
    raise SwError(f"{ip}: SSH не удался ({last.strip()[:150]})")


def hw_section(out: str, cmd: str) -> str:
    """Вырезает из сырого вывода сессии кусок после echo команды cmd
    до следующего промпта <sysname>."""
    lines = (out or "").splitlines()
    buf, on = [], False
    for ln in lines:
        if on:
            if ln.startswith("<") and ">" in ln:
                break
            buf.append(ln)
        elif ln.startswith("<") and ln.rstrip().endswith(cmd):
            on = True
    return "\n".join(buf)


def hw_sysname(out: str):
    m = re.search(r"^<([^<>\n]+)>", out or "", re.M)
    return m.group(1) if m else None



# ---------- парсеры Huawei VRP (чистые функции) ----------
def hw_short_if(name: str) -> str:
    """GigabitEthernet0/0/1 -> GE0/0/1, XGigabitEthernet0/0/2 -> XGE0/0/2."""
    return (str(name or "").replace("XGigabitEthernet", "XGE")
            .replace("GigabitEthernet", "GE").replace("MultiGE", "MGE"))


def hw_parse_version(text: str) -> dict:
    """display version -> {model, version, uptime_s}."""
    out = {"model": None, "version": None, "uptime_s": None}
    m = re.search(r"Version\s+([\d.]+)\s*\(([^)]+)\)", text or "")
    if m:
        out["version"] = m.group(2).split()[-1] if " " in m.group(2) else m.group(2)
    m = re.search(r"HUAWEI\s+(\S+)\s+.*?uptime is\s+(.+)", text or "")
    if m:
        out["model"] = m.group(1)
        up, sec = m.group(2), 0
        for val, unit in re.findall(r"(\d+)\s+(week|day|hour|minute)", up):
            sec += int(val) * {"week": 604800, "day": 86400,
                               "hour": 3600, "minute": 60}[unit]
        out["uptime_s"] = sec or None
    return out


def hw_parse_poe_ports(text: str) -> dict:
    """display poe power -> {GE0/0/3: {'cls': '1', 'cur_mw': 900, 'peak_mw': 1200}}."""
    out = {}
    for ln in (text or "").splitlines():
        m = re.match(r"\s*((?:X?Gigabit|Multi)\S*Ethernet\S+|MultiGE\S+)\s+(\S+)\s+"
                     r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", ln)
        if not m:
            continue
        cur = m.group(5)
        out[hw_short_if(m.group(1))] = {
            "cls": m.group(2),
            "cur_mw": int(cur) if cur.isdigit() else 0,
            "peak_mw": int(m.group(6)) if m.group(6).isdigit() else 0}
    return out


def hw_parse_poe_info(text: str) -> dict:
    """display poe information -> {supply_mw, avail_mw, consume_mw, peak_mw}."""
    def num(pat):
        m = re.search(pat + r"\(mW\)\s*:\s*(\d+)", text or "")
        return int(m.group(1)) if m else None
    return {"supply_mw": num(r"PoE Power Supply"),
            "avail_mw": num(r"Available Total Power"),
            "consume_mw": num(r"Total Power Consumption"),
            "peak_mw": num(r"Power Peak Value")}


def hw_parse_int_brief(text: str) -> dict:
    """display interface brief -> {ifname: {phy, proto, in_err, out_err}}.
    Виртуальные NULL/Vlanif/LoopBack пропускаются."""
    out = {}
    for ln in (text or "").splitlines():
        m = re.match(r"\s*([A-Za-z][\w/.-]*\d)\s+(\*?down|up|#down|-down)\s+"
                     r"(\S+)\s+(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s*$", ln)
        if not m:
            continue
        if m.group(1).startswith(("NULL", "Vlanif", "LoopBack")):
            continue
        out[hw_short_if(m.group(1))] = {
            "phy": m.group(2).lstrip("*#-"), "proto": m.group(3),
            "in_uti": m.group(4), "out_uti": m.group(5),
            "in_err": int(m.group(6)), "out_err": int(m.group(7))}
    return out


def hw_parse_lldp_brief(text: str) -> list:
    """display lldp neighbor brief -> [{'local','dev','port'}]."""
    out = []
    for ln in (text or "").splitlines():
        m = re.match(r"\s*((?:X?GE|MGE|MultiGE|Eth-Trunk)\S*)\s+(\S.*?)\s{2,}"
                     r"(\S+)\s+(\d+)\s*$", ln)
        if m and "Local" not in m.group(1):
            out.append({"local": m.group(1), "dev": m.group(2).strip(),
                        "port": m.group(3)})
    return out


def hw_parse_temperature(text: str) -> dict:
    """display temperature all -> {current_c, upper_c, status}."""
    for ln in (text or "").splitlines():
        m = re.match(r"\s*\d+\s+\S+\s+\S+\s+(\w+)\s+(-?\d+)\s+(-?\d+)\s+"
                     r"(-?\d+)\s+(-?\d+)", ln)
        if m:
            return {"status": m.group(1), "current_c": int(m.group(2)),
                    "upper_c": int(m.group(5))}
    return {}


def hw_parse_ntp(text: str) -> dict:
    """display ntp-service status -> {sync: bool, stratum: int|None}."""
    sync = "unsynchronized" not in (text or "")and "clock status" in (text or "")
    m = re.search(r"clock stratum:\s*(\d+)", text or "")
    return {"sync": sync, "stratum": int(m.group(1)) if m else None}


def hw_parse_clock(text: str):
    """display clock -> 'YYYY-MM-DD HH:MM:SS' или None."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", text or "")
    return f"{m.group(1)} {m.group(2)}" if m else None


def hw_parse_mac_table(text: str) -> list:
    """display mac-address -> [{'mac','vlan','port'}] (мак вида aaaa-bbbb-cccc)."""
    out = []
    for ln in (text or "").splitlines():
        m = re.match(r"\s*([0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4})\s+(\d+)\S*\s+"
                     r".*?([A-Za-z][\w/.-]*\d)\s+\w+", ln, re.I)
        if m:
            h = m.group(1).replace("-", "")
            mac = ":".join(h[i:i + 2] for i in range(0, 12, 2)).upper()
            out.append({"mac": mac, "vlan": int(m.group(2)),
                        "port": hw_short_if(m.group(3))})
    return out


