# -*- coding: utf-8 -*-
"""Сетевые утилиты: ping/ARP, TCP-проверки, скан подсети, вендор по OUI,
валидация IP и allowlist подсетей для /find.
Волна A: R27 (ipaddress), R28 (allowlist), R29 (таймауты subprocess),
R30 (CREATE_NO_WINDOW + cp866).
"""
import re
import socket
import ipaddress
import subprocess
from concurrent.futures import ThreadPoolExecutor

import bot_state as st

# R30: без чёрных окон консоли под Task Scheduler; вывод ping/arp — в cp866
_SUB_KW = dict(capture_output=True, text=True, encoding="cp866", errors="replace",
               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def valid_ip(s) -> bool:
    """R27/U23: настоящая валидация IPv4 (999.1.1.1 не пройдёт)."""
    try:
        ipaddress.IPv4Address((s or "").strip())
        return True
    except Exception:
        return False


def valid_prefix(s) -> bool:
    """Префикс /24 вида '10.20.50' — три октета, валидные как IP с '.0'."""
    return bool(re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$", s or "")) and valid_ip(s + ".0")


def prefix_allowed(prefix: str) -> bool:
    """R28: подсеть для /find должна входить в allowlist из конфига (find_allow)."""
    try:
        net = ipaddress.ip_network(prefix + ".0/24")
    except Exception:
        return False
    for a in st.cget("find_allow"):
        try:
            if net.subnet_of(ipaddress.ip_network(a)):
                return True
        except Exception:
            continue
    return False


def ping(ip, timeout_ms=None):
    """ICMP-пинг; None если не ответил. R29: жёсткий таймаут subprocess."""
    timeout_ms = timeout_ms or st.cget("ping_timeout_ms")
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", str(timeout_ms), ip],
                           timeout=st.cget("subproc_timeout_s"), **_SUB_KW)
        return ip if "TTL=" in r.stdout.upper() else None
    except Exception:
        return None


_ARP_RE = re.compile(r"\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9A-Fa-f-]{17})\s+(\w+)")
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def parse_arp(text: str) -> dict:
    """Чистый парсер вывода `arp -a` (Windows): ip -> mac (aa:bb:...)."""
    res: dict = {}
    for line in (text or "").splitlines():
        m = _ARP_RE.match(line)
        if m:
            res[m.group(1)] = m.group(2).lower().replace("-", ":")
    return res


def find_ips(text: str) -> list:
    """U24: все валидные IPv4 из произвольного текста (уникальные, по порядку)."""
    return list(dict.fromkeys(
        ip for ip in _IP_RE.findall(text or "") if valid_ip(ip)))


def arp_table() -> dict:
    """ARP-таблица Windows: ip -> mac (aa:bb:...)."""
    try:
        out = subprocess.run(["arp", "-a"], timeout=st.cget("subproc_timeout_s"),
                             **_SUB_KW).stdout
        return parse_arp(out)
    except Exception:
        return {}


def open_ports(ip, ports=None):
    ports = tuple(ports or st.cget("probe_ports"))
    found = []
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.7)
        try:
            if s.connect_ex((ip, p)) == 0:
                found.append(p)
        except Exception:
            pass
        finally:
            s.close()
    return found


def tcp_alive(ip: str, ports: tuple = (80, 554), t: float = 0.6) -> bool:
    """Жив ли хост — по факту TCP-коннекта (надёжнее ping+ARP, не зависит от кэша)."""
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(t)
        try:
            if s.connect_ex((ip, p)) == 0:
                return True
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
    return False


def scan_subnet(prefix):
    """Живые хосты подсети прямым TCP-сверком (80/554); MAC из ARP best-effort
    (коннект сам наполняет ARP, поэтому MAC обычно есть)."""
    ips = [f"{prefix}.{i}" for i in range(1, 255)]
    live = []
    with ThreadPoolExecutor(max_workers=st.cget("scan_workers")) as ex:
        for ip, ok in zip(ips, ex.map(tcp_alive, ips)):
            if ok:
                live.append(ip)
    arp = arp_table()
    return {ip: arp.get(ip, "—") for ip in live}


def probe_many(ips, ports=None):
    """Параллельная проверка портов у списка хостов: ip -> [открытые порты]."""
    ports = tuple(ports or st.cget("probe_ports"))

    def probe(ip):
        return ip, open_ports(ip, ports)

    if not ips:
        return {}
    with ThreadPoolExecutor(max_workers=min(40, max(4, len(ips)))) as ex:
        return dict(ex.map(probe, ips))


OUI_VENDOR = {"e0:7f:88": "Apix/Evidence"}


def vendor(mac):
    return OUI_VENDOR.get((mac or "").lower().replace("-", ":")[:8], "?")
