# -*- coding: utf-8 -*-
"""Волна G — read-only ONVIF-запросы raw-SOAP (паттерны onvif_snap):
GetSystemDateAndTime (309-312, 321), GetHostname (332), GetNTP (311),
GetNetworkInterfaces / GetNetworkDefaultGateway (331/333), GetUsers (334 —
строго чтение), GetVideoEncoderConfigurations (339).
Отличие от onvif_snap._soap: возвращаем и HTTP-статус — 401/NotAuthorized
трактуется как «канарейка кредов» (335, ключ "auth"), а не как офлайн.
Пароли камер НИКОГДА не меняются; все вызовы read-only."""
import re
import time
import datetime

import requests

import bot_state as st
from onvif_snap import _wss, _grab, DEV_NS, MEDIA_NS


def _post(ip, ns, action, body, timeout=6, port=80):
    env = (f'<?xml version="1.0" encoding="UTF-8"?>'
           f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           f'<s:Header>{_wss(st.CAM_USER, st.CAM_PASS)}</s:Header>'
           f'<s:Body>{body}</s:Body></s:Envelope>')
    ct = f'application/soap+xml; charset=utf-8; action="{ns}/{action}"'
    r = requests.post(f"http://{ip}:{port}/onvif/device_service",
                      data=env.encode(), headers={"Content-Type": ct},
                      timeout=timeout)
    return r.status_code, r.text


def _is_auth_fault(status: int, text: str) -> bool:
    low = (text or "").lower()
    return (status == 401 or "notauthorized" in low
            or "not authorized" in low or "sender not authorized" in low)


def call(ip, action, body, ns=DEV_NS, timeout=6):
    """{'text','ms'} либо {'error':..., 'auth':bool, 'ms':...}."""
    t0 = time.time()
    try:
        status, text = _post(ip, ns, action, body, timeout=timeout)
    except Exception as e:
        return {"error": type(e).__name__, "auth": False,
                "ms": (time.time() - t0) * 1000}
    ms = (time.time() - t0) * 1000
    if _is_auth_fault(status, text):
        return {"error": "auth", "auth": True, "ms": ms}
    if status >= 400 and "<" not in (text or ""):
        return {"error": f"HTTP {status}", "auth": False, "ms": ms}
    return {"text": text, "ms": ms}


def _int(t, tag):
    v = _grab(t, tag)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------- 309-312, 321: часы ----------
def get_datetime(ip, timeout=6) -> dict:
    """{'offset_s','type','tz','dst','year','ms'} либо {'error','auth','ms'}.
    offset_s = часы камеры (UTC) минус часы ПК на середину запроса."""
    t0 = time.time()
    r = call(ip, "GetSystemDateAndTime",
             f'<GetSystemDateAndTime xmlns="{DEV_NS}"/>', timeout=timeout)
    if "text" not in r:
        return r
    t = r["text"]
    out = {"ms": round(r["ms"]), "type": _grab(t, "DateTimeType"),
           "tz": _grab(t, "TZ"), "dst": _grab(t, "DaylightSavings")}
    m = re.search(r"<(?:\w+:)?UTCDateTime>(.*?)</(?:\w+:)?UTCDateTime>", t, re.S)
    if not m:
        out["error"] = "нет UTCDateTime"
        return out
    sec = m.group(1)
    try:
        cam = datetime.datetime(
            _int(sec, "Year"), _int(sec, "Month"), _int(sec, "Day"),
            _int(sec, "Hour"), _int(sec, "Minute"), _int(sec, "Second") or 0,
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        out["error"] = "битая дата"
        return out
    mid = t0 + (time.time() - t0) / 2  # компенсация RTT
    out["offset_s"] = round(cam.timestamp() - mid, 1)
    out["year"] = cam.year
    return out


# ---------- 332: hostname ----------
def get_hostname(ip, timeout=6) -> dict:
    r = call(ip, "GetHostname", f'<GetHostname xmlns="{DEV_NS}"/>',
             timeout=timeout)
    if "text" not in r:
        return r
    return {"name": _grab(r["text"], "Name") or "",
            "from_dhcp": (_grab(r["text"], "FromDHCP") or "") == "true"}


# ---------- 311: NTP ----------
def get_ntp(ip, timeout=6) -> dict:
    r = call(ip, "GetNTP", f'<GetNTP xmlns="{DEV_NS}"/>', timeout=timeout)
    if "text" not in r:
        return r
    t = r["text"]
    addr = _grab(t, "IPv4Address") or _grab(t, "DNSname") or ""
    return {"from_dhcp": (_grab(t, "FromDHCP") or "") == "true", "addr": addr}


# ---------- 331/333: сеть ----------
def get_net(ip, timeout=6) -> dict:
    """{'dhcp': bool, 'gateway': str} по GetNetworkInterfaces + Gateway."""
    out = {}
    r = call(ip, "GetNetworkInterfaces",
             f'<GetNetworkInterfaces xmlns="{DEV_NS}"/>', timeout=timeout)
    if "text" not in r:
        return r
    out["dhcp"] = (_grab(r["text"], "DHCP") or "").strip() == "true"
    g = call(ip, "GetNetworkDefaultGateway",
             f'<GetNetworkDefaultGateway xmlns="{DEV_NS}"/>', timeout=timeout)
    out["gateway"] = (_grab(g.get("text") or "", "IPv4Address") or "").strip()
    return out


# ---------- 334: пользователи (read-only!) ----------
def get_users(ip, timeout=6) -> dict:
    r = call(ip, "GetUsers", f'<GetUsers xmlns="{DEV_NS}"/>', timeout=timeout)
    if "text" not in r:
        return r
    users = re.findall(r"<(?:\w+:)?Username>(.*?)</(?:\w+:)?Username>",
                       r["text"], re.S)
    return {"users": sorted(u.strip() for u in users if u.strip())}


# ---------- 339: конфигурации энкодеров ----------
def get_encoders(ip, timeout=8) -> dict:
    """[{'name','codec','res','fps','kbps','gop'}] из
    GetVideoEncoderConfigurations (media_service)."""
    r = call(ip, "GetVideoEncoderConfigurations",
             f'<GetVideoEncoderConfigurations xmlns="{MEDIA_NS}"/>',
             ns=MEDIA_NS, timeout=timeout)
    if "text" not in r:
        return r
    out = []
    for m in re.finditer(r"<(?:\w+:)?Configurations[ >](.*?)"
                         r"</(?:\w+:)?Configurations>", r["text"], re.S):
        b = m.group(1)
        w, hgt = _int(b, "Width"), _int(b, "Height")
        out.append({"name": _grab(b, "Name") or "?",
                    "codec": _grab(b, "Encoding") or "?",
                    "res": f"{w}x{hgt}" if w and hgt else "?",
                    "fps": _int(b, "FrameRateLimit"),
                    "kbps": _int(b, "BitrateLimit"),
                    "gop": _int(b, "GovLength")})
    return {"encoders": out, "ms": round(r["ms"])}
