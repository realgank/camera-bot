# -*- coding: utf-8 -*-
"""Волна G — RTSP-пробы raw-сокетом (313-316) + NVR-заглушки (349-350):
313 OPTIONS+DESCRIBE с digest: порт 554 открыт, а SDP не отдаётся = «зомби»;
314 /bitrate — SETUP/PLAY по TCP-interleaved, чтение rtsp_bitrate_s секунд,
    реальный битрейт (только по явной команде — тяжёлая проба);
315 сводка SDP (кодек/разрешение/fps) против эталона в _facts_sdp.json —
    первый снятый SDP становится эталоном, дрейф = алерт;
316 фон — ротацией rtsp_batch камер раз в rtsp_period_min (не шторм).
349/350 NVR: в сети не инвентаризирован — честные заглушки /nvr /rec
    (конфиг nvr_list) с минимальной TCP-проверкой доступности.
Пароли камер НЕ меняются; PLAY только читает поток и делает TEARDOWN."""
import re
import time
import socket
import hashlib
from concurrent.futures import ThreadPoolExecutor

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_store as store
import bot_metrics as mx
from bot_tg import send, send_chunks, chat_action
from bot_util import log, log_exc, esc


def _facts():
    return store.jload(st.cget("sdp_facts_path"), {})


def _tmo():
    return float(st.cget("rtsp_timeout_s"))


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _auth_hdr(method: str, uri: str, challenge: str) -> str:
    """Digest/Basic из WWW-Authenticate (RFC 2069-стиль, как у Apix)."""
    if challenge.lower().startswith("basic"):
        import base64
        return "Basic " + base64.b64encode(
            f"{st.CAM_USER}:{st.CAM_PASS}".encode()).decode()
    d = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    ha1 = _md5(f"{st.CAM_USER}:{d.get('realm', '')}:{st.CAM_PASS}")
    ha2 = _md5(f"{method}:{uri}")
    resp = _md5(f"{ha1}:{d.get('nonce', '')}:{ha2}")
    return (f'Digest username="{st.CAM_USER}", realm="{d.get("realm", "")}", '
            f'nonce="{d.get("nonce", "")}", uri="{uri}", response="{resp}"')


def _recv_resp(sock, deadline):
    """(code, headers, body) одного RTSP-ответа."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        sock.settimeout(max(0.2, deadline - time.time()))
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    try:
        code = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return 0, {}, ""
    hdrs = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        hdrs[k.strip().lower()] = v.strip()
    clen = int(hdrs.get("content-length") or 0)
    while len(rest) < clen:
        sock.settimeout(max(0.2, deadline - time.time()))
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    return code, hdrs, rest[:clen].decode("utf-8", "replace")


def _req(sock, method, uri, cseq, deadline, extra=None, auth=None):
    lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}",
             "User-Agent: camera_bot"]
    if auth:
        lines.append(f"Authorization: {auth}")
    lines += extra or []
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    return _recv_resp(sock, deadline)


def _cam_uri(ip: str) -> str:
    """RTSP-URL: из кэша фактов либо ONVIF GetStreamUri (кэшируется)."""
    f = _facts().get(ip) or {}
    if f.get("uri"):
        return f["uri"]
    try:
        from onvif_snap import rtsp_uri
        uri, _err = rtsp_uri(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
        if uri:
            uri = re.sub(r"://[^/@]+(?=/)",
                         f"://{ip}:554", uri, count=1)  # host -> сам ip
            return uri
    except Exception:
        log_exc(f"rtsp: rtsp_uri {ip}")
    return f"rtsp://{ip}:554/"


def describe(ip: str, uri: str = None) -> dict:
    """313: {'ok','code','sdp','ms','uri','conn'} — conn=False: 554 закрыт."""
    uri = uri or _cam_uri(ip)
    t0 = time.time()
    deadline = t0 + _tmo() * 2
    try:
        sock = socket.create_connection((ip, 554), timeout=_tmo())
    except OSError as e:
        return {"ok": False, "conn": False, "code": 0, "uri": uri,
                "err": type(e).__name__, "ms": (time.time() - t0) * 1000}
    try:
        extra = ["Accept: application/sdp"]
        code, hdrs, body = _req(sock, "DESCRIBE", uri, 2, deadline, extra)
        if code == 401 and hdrs.get("www-authenticate"):
            auth = _auth_hdr("DESCRIBE", uri, hdrs["www-authenticate"])
            code, hdrs, body = _req(sock, "DESCRIBE", uri, 3, deadline,
                                    extra, auth=auth)
        ms = (time.time() - t0) * 1000
        ok = code == 200 and "m=" in body
        return {"ok": ok, "conn": True, "code": code, "sdp": body if ok else "",
                "uri": uri, "ms": ms,
                "base": hdrs.get("content-base") or uri,
                "challenge": hdrs.get("www-authenticate", "")}
    except OSError as e:
        return {"ok": False, "conn": True, "code": 0, "uri": uri,
                "err": type(e).__name__, "ms": (time.time() - t0) * 1000}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def parse_sdp(sdp: str) -> dict:
    """315: сводка SDP: кодек/fps/разрешение/число дорожек/control видео."""
    out = {"codec": None, "fps": None, "res": None, "tracks": 0,
           "control": None}
    cur = None
    for ln in (sdp or "").splitlines():
        ln = ln.strip()
        if ln.startswith("m="):
            cur = ln[2:].split()[0]
            out["tracks"] += 1
        elif ln.startswith("a=rtpmap:") and cur == "video" and not out["codec"]:
            m = re.match(r"a=rtpmap:\d+\s+([A-Za-z0-9._-]+)/", ln)
            if m:
                out["codec"] = m.group(1).upper()
        elif ln.startswith("a=framerate:") and cur == "video":
            try:
                out["fps"] = round(float(ln.split(":", 1)[1]))
            except ValueError:
                pass
        elif ln.startswith("a=x-dimensions:") and cur == "video":
            m = re.findall(r"\d+", ln)
            if len(m) >= 2:
                out["res"] = f"{m[0]}x{m[1]}"
        elif ln.startswith("a=framesize:") and cur == "video":
            m = re.search(r"(\d+)-(\d+)", ln)
            if m:
                out["res"] = f"{m.group(1)}x{m.group(2)}"
        elif ln.startswith("a=control:") and cur == "video" \
                and not out["control"]:
            out["control"] = ln.split(":", 1)[1].strip()
    return out


def sdp_drift(ref: dict, cur: dict) -> list:
    """315: [(поле, было, стало)] по codec/res/fps (сравниваем заполненные)."""
    out = []
    for k in ("codec", "res", "fps"):
        if ref.get(k) is not None and cur.get(k) is not None \
                and ref[k] != cur[k]:
            out.append((k, ref[k], cur[k]))
    return out


def _store_sdp(ip: str, summ: dict, uri: str) -> list:
    """Эталон = первый снятый SDP; возвращает список дрейфов против него."""
    drifts = []
    def _fn(d):
        e = d.get(ip) or {}
        if not e.get("ref"):
            e["ref"] = summ
        else:
            drifts.extend(sdp_drift(e["ref"], summ))
        e.update({"cur": summ, "uri": uri, "ts": int(time.time())})
        d[ip] = e
        return d
    store.jupdate(st.cget("sdp_facts_path"), {}, _fn)
    return drifts


def _probe_one(ip: str) -> None:
    r = describe(ip)
    if not r.get("conn"):
        return  # 554 закрыт — не зомби, просто нет RTSP
    if not r["ok"]:
        if mx.event_add(ip, "rtsp_zombie", f"code={r.get('code')}"):
            mx.owner_alert(f"🧟 <b>RTSP-зомби</b>: {esc(inv.label(ip) or ip)} — "
                           f"порт 554 открыт, SDP не отдаётся "
                           f"(код {r.get('code') or esc(r.get('err') or '?')})."
                           f" Поток мёртв при живой камере. /rtsp_check {ip}")
        return
    mx.metric_add(ip, "rtsp_ms", r["ms"])
    summ = parse_sdp(r["sdp"])
    for k, was, now_ in _store_sdp(ip, summ, r["uri"]):
        if mx.event_add(ip, "sdp_drift", f"{k}: {was} -> {now_}"):
            mx.owner_alert(f"🎛 <b>Дрейф потока</b>: {esc(inv.label(ip) or ip)}"
                           f" — {esc(k)}: {esc(was)} → {esc(now_)} "
                           f"(кто-то менял настройки энкодера?)")


def _tick() -> None:
    if not st.cget("rtsp_enabled"):
        return
    if not mx.due("rtsp", float(st.cget("rtsp_period_min")), first_delay_s=420):
        return
    batch = mx.rotation_batch("rtsp", int(st.cget("rtsp_batch")))
    if not batch:
        return
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="rtsp") as ex:
        list(ex.map(_probe_one, batch))
    log(f"rtsp: ротация {len(batch)} камер за {time.time() - t0:.1f}s")


# ---------- 314: /bitrate ----------
def measure_bitrate(ip: str, secs: float = None) -> dict:
    """DESCRIBE + SETUP (TCP interleaved) + PLAY на ОДНОМ сокете (иначе
    камеры отвергают SETUP со старым nonce), чтение secs секунд, TEARDOWN."""
    secs = secs or float(st.cget("rtsp_bitrate_s"))
    uri = _cam_uri(ip)
    deadline = time.time() + _tmo() * 3 + secs
    try:
        sock = socket.create_connection((ip, 554), timeout=_tmo())
    except OSError as e:
        return {"ok": False, "err": type(e).__name__}
    cseq, chal = [1], [""]

    def rq(method, u, extra=None):
        cseq[0] += 1
        auth = _auth_hdr(method, u, chal[0]) if chal[0] else None
        code, hdrs, body = _req(sock, method, u, cseq[0], deadline,
                                extra, auth=auth)
        if code == 401 and hdrs.get("www-authenticate"):
            chal[0] = hdrs["www-authenticate"]
            cseq[0] += 1
            code, hdrs, body = _req(sock, method, u, cseq[0], deadline, extra,
                                    auth=_auth_hdr(method, u, chal[0]))
        return code, hdrs, body

    try:
        code, hdrs, sdp = rq("DESCRIBE", uri, ["Accept: application/sdp"])
        if code != 200 or "m=" not in sdp:
            return {"ok": False, "err": f"DESCRIBE: код {code}"}
        summ = parse_sdp(sdp)
        base = hdrs.get("content-base") or uri
        ctrl = summ.get("control") or "*"
        curl = (ctrl if ctrl.startswith("rtsp://")
                else base.rstrip("/") + "/" + ctrl.lstrip("/"))
        code, hdrs, _b = rq("SETUP", curl,
                            ["Transport: RTP/AVP/TCP;unicast;interleaved=0-1"])
        if code != 200:
            return {"ok": False, "err": f"SETUP: код {code}"}
        session = (hdrs.get("session") or "").split(";")[0].strip()
        code, hdrs, _b = rq("PLAY", base,
                            [f"Session: {session}", "Range: npt=0.000-"])
        if code != 200:
            return {"ok": False, "err": f"PLAY: код {code}"}
        total, t0 = 0, time.time()
        sock.settimeout(1.0)
        while time.time() - t0 < secs:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            total += len(chunk)
        dur = max(0.2, time.time() - t0)
        try:  # вежливо закрываем сессию
            _req(sock, "TEARDOWN", base, cseq[0] + 1, time.time() + 2,
                 [f"Session: {session}"],
                 auth=_auth_hdr("TEARDOWN", base, chal[0]) if chal[0] else None)
        except OSError:
            pass
        kbps = total * 8 / dur / 1000
        mx.metric_add(ip, "rtsp_kbps", kbps)
        return {"ok": True, "kbps": kbps, "bytes": total, "secs": dur,
                "sdp": summ}
    except OSError as e:
        return {"ok": False, "err": type(e).__name__}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _resolve(arg):
    a = (arg or "").strip()
    if net.valid_ip(a):
        return a
    return inv.resolve_ip(a) if a else None


def cmd_rtsp_check(chat, arg="", reply_to=None):
    ip = _resolve(arg)
    if not ip:
        send(chat, "RTSP-проба: <code>/rtsp_check 10.20.50.51</code>",
             reply_to=reply_to)
        return
    chat_action(chat)
    r = describe(ip)
    if not r.get("conn"):
        send(chat, f"❌ <code>{ip}</code>: порт 554 закрыт "
                   f"({esc(r.get('err') or '')}).", reply_to=reply_to)
        return
    if not r["ok"]:
        send(chat, f"🧟 <code>{ip}</code>: порт 554 открыт, но DESCRIBE не дал "
                   f"SDP (код {r.get('code') or esc(r.get('err') or '?')}) — "
                   f"«зомби»-RTSP (313).", reply_to=reply_to)
        return
    summ = parse_sdp(r["sdp"])
    drifts = _store_sdp(ip, summ, r["uri"])
    lines = [f"🎬 <b>RTSP {esc(inv.label(ip) or ip)}</b> — SDP за {r['ms']:.0f} мс",
             f"кодек: <b>{esc(summ.get('codec') or '?')}</b> · разрешение: "
             f"<b>{esc(summ.get('res') or '— (нет в SDP)')}</b> · fps: "
             f"<b>{esc(summ.get('fps') or '—')}</b> · дорожек: {summ['tracks']}",
             f"<code>{esc(r['uri'])}</code>"]
    if drifts:
        lines.append("⚠️ <b>Дрейф против эталона</b>: " + "; ".join(
            f"{k}: {was} → {now_}" for k, was, now_ in drifts))
    else:
        lines.append("✅ совпадает с эталоном SDP (или эталон только что снят)")
    lines.append(f"Битрейт живьём: /bitrate {ip} (315: разрешение/fps не все "
                 f"камеры кладут в SDP — тогда сверка только по кодеку)")
    send_chunks(chat, lines)


def cmd_bitrate(chat, arg="", reply_to=None):
    ip = _resolve(arg)
    if not ip:
        send(chat, "Замер битрейта: <code>/bitrate 10.20.50.51</code> "
                   f"(читает поток {st.cget('rtsp_bitrate_s')}с)",
             reply_to=reply_to)
        return
    chat_action(chat)
    send(chat, f"📼 Читаю поток <code>{ip}</code> "
               f"{st.cget('rtsp_bitrate_s')}с …", reply_to=reply_to, silent=True)
    r = measure_bitrate(ip)
    if not r.get("ok"):
        send(chat, f"❌ <code>{ip}</code>: {esc(r.get('err') or '?')}")
        return
    note = ""
    if r["kbps"] < 50:
        note = "\n⚠️ битрейт ~0 — поток пустой при успешном PLAY (314)!"
    send(chat, f"📼 <b>{esc(inv.label(ip) or ip)}</b>: "
               f"<b>{r['kbps']:.0f} кбит/с</b> за {r['secs']:.1f}с "
               f"({r['bytes'] // 1024} КБ, TCP-interleaved)" + note)


# ---------- 349/350: NVR ----------
def cmd_nvr(chat, arg="", reply_to=None):
    nvrs = st.cget("nvr_list") or []
    if not nvrs:
        send(chat, "🗄 <b>NVR не настроен</b> — регистраторы в сети не "
                   "инвентаризированы (349).\nЗадай в конфиге "
                   "<code>nvr_list</code>: [{\"name\": \"NVR-1\", "
                   "\"ip\": \"10.20.x.x\", \"port\": 80}] — тогда бот будет "
                   "проверять их доступность.", reply_to=reply_to)
        return
    chat_action(chat)
    lines = ["🗄 <b>NVR</b> (только доступность — API не подключён):"]
    for n in nvrs:
        ip, port = n.get("ip"), int(n.get("port") or 80)
        ok = bool(ip) and net.tcp_alive(ip, ports=(port,), t=1.5)
        lines.append(f"{'🟢' if ok else '🔴'} {esc(n.get('name') or ip)} "
                     f"<code>{esc(ip)}:{port}</code>")
    lines.append("Диски/RAID/каналы записи (349) требуют API конкретного NVR.")
    send(chat, "\n".join(lines), reply_to=reply_to)


def cmd_rec(chat, arg="", reply_to=None):
    send(chat, "🎞 <b>Проверка наличия записи</b> (350): NVR не настроен — "
               "задай <code>nvr_list</code> в конфиге. Проверка архива "
               "требует API регистратора или ONVIF Profile G (у Apix на "
               "камерах записи нет — пишет NVR).", reply_to=reply_to)


try:
    import bot_health as _bh
    _bh.MINUTE_TICKS.append(_tick)
except Exception:
    pass

HANDLERS = {"/rtsp_check": cmd_rtsp_check, "/bitrate": cmd_bitrate,
            "/nvr": cmd_nvr, "/rec": cmd_rec}
ALIASES = {"/поток": "/rtsp_check", "/битрейт": "/bitrate"}
CALLBACKS: dict = {}
