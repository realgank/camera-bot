# -*- coding: utf-8 -*-
"""Снимок (JPEG) с ONVIF-камеры без onvif_zeep (работает на Python 3.14).
GetProfiles -> GetSnapshotUri -> HTTP GET (digest/basic). Порт по умолчанию 80.
Также: device_info() и rtsp_uri()."""
import sys, base64, hashlib, os, datetime, re
import requests
from requests.auth import HTTPDigestAuth, HTTPBasicAuth

DEV_NS = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"

def _wss(user, pwd):
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + pwd.encode()).digest()).decode()
    n_b64 = base64.b64encode(nonce).decode()
    return f'''<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
   <UsernameToken><Username>{user}</Username>
    <Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
    <Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{n_b64}</Nonce>
    <Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
   </UsernameToken></Security>'''

def _soap(ip, port, ns, action, body, user, pwd, timeout=6, path="/onvif/device_service"):
    env = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
 <s:Header>{_wss(user,pwd)}</s:Header>
 <s:Body>{body}</s:Body></s:Envelope>'''
    url = f"http://{ip}:{port}{path}"
    ct = f'application/soap+xml; charset=utf-8; action="{ns}/{action}"'
    r = requests.post(url, data=env.encode(), headers={"Content-Type": ct}, timeout=timeout)
    return r.text

def _grab(t, tag):
    m = re.search(r"<(?:\w+:)?%s[ >](.*?)</(?:\w+:)?%s>" % (tag, tag), t, re.S)
    if m: return m.group(1).strip()
    m = re.search(r"<(?:\w+:)?%s>(.*?)</(?:\w+:)?%s>" % (tag, tag), t, re.S)
    return m.group(1).strip() if m else None

def device_info(ip, port=80, user="Admin", pwd="1234", timeout=6):
    try:
        t = _soap(ip, port, DEV_NS, "GetDeviceInformation",
                  f'<GetDeviceInformation xmlns="{DEV_NS}"/>', user, pwd, timeout)
    except Exception as e:
        return {"error": type(e).__name__}
    if _grab(t, "Model"):
        return {"manufacturer": _grab(t,"Manufacturer"), "model": _grab(t,"Model"),
                "firmware": _grab(t,"FirmwareVersion"), "serial": _grab(t,"SerialNumber")}
    return {"error": _grab(t,"Text") or _grab(t,"faultstring") or "no Model"}

def _profile_token(ip, port, user, pwd, timeout=6):
    t = _soap(ip, port, MEDIA_NS, "GetProfiles",
              f'<GetProfiles xmlns="{MEDIA_NS}"/>', user, pwd, timeout)
    # token либо атрибут Profiles token="...", либо <Token>
    m = re.search(r'<(?:\w+:)?Profiles[^>]*\btoken="([^"]+)"', t)
    if m: return m.group(1)
    return _grab(t, "Token") or _grab(t, "ProfileToken")

def snapshot_uri(ip, port=80, user="Admin", pwd="1234", timeout=6):
    tok = _profile_token(ip, port, user, pwd, timeout)
    if not tok:
        return None, "no profile token"
    body = (f'<GetSnapshotUri xmlns="{MEDIA_NS}"><ProfileToken>{tok}</ProfileToken></GetSnapshotUri>')
    t = _soap(ip, port, MEDIA_NS, "GetSnapshotUri", body, user, pwd, timeout)
    uri = _grab(t, "Uri")
    return uri, tok

def rtsp_uri(ip, port=80, user="Admin", pwd="1234", timeout=6):
    """RTSP URL основного потока через GetStreamUri (Media1).
    Возвращает (uri, None) либо (None, err)."""
    try:
        tok = _profile_token(ip, port, user, pwd, timeout)
        if not tok:
            return None, "no profile token"
        sch = "http://www.onvif.org/ver10/schema"
        body = (f'<GetStreamUri xmlns="{MEDIA_NS}">'
                f'<StreamSetup><Stream xmlns="{sch}">RTP-Unicast</Stream>'
                f'<Transport xmlns="{sch}"><Protocol>RTSP</Protocol></Transport>'
                f'</StreamSetup><ProfileToken>{tok}</ProfileToken></GetStreamUri>')
        t = _soap(ip, port, MEDIA_NS, "GetStreamUri", body, user, pwd, timeout)
        uri = _grab(t, "Uri")
        if uri:
            return uri, None
        return None, _grab(t, "Text") or _grab(t, "faultstring") or "no Uri"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

def system_reboot(ip, port=80, user="Admin", pwd="1234", timeout=8):
    """ONVIF SystemReboot (мягкая перезагрузка камеры).
    Возвращает (True, message) либо (False, err). Пароли НЕ меняются."""
    try:
        t = _soap(ip, port, DEV_NS, "SystemReboot",
                  f'<SystemReboot xmlns="{DEV_NS}"/>', user, pwd, timeout)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    msg = _grab(t, "Message")
    if msg is not None:
        return True, msg
    return False, _grab(t, "Text") or _grab(t, "faultstring") or "нет ответа Message"

def get_snapshot(ip, port=80, user="Admin", pwd="1234", out=None, timeout=8):
    """Возвращает (bytes_jpeg, info_str) либо (None, error)."""
    try:
        uri, tok = snapshot_uri(ip, port, user, pwd, timeout)
    except Exception as e:
        return None, f"ONVIF err: {type(e).__name__}: {e}"
    if not uri:
        return None, f"GetSnapshotUri не дал URL ({tok})"
    # некоторые камеры отдают URL с внутренним IP — заменим host на наш ip
    uri2 = re.sub(r"://[^/]+", f"://{ip}", uri) if re.search(r"://(?!%s)" % re.escape(ip), uri) else uri
    for u in dict.fromkeys([uri, uri2]):
        for auth in (HTTPDigestAuth(user,pwd), HTTPBasicAuth(user,pwd), None):
            try:
                r = requests.get(u, auth=auth, timeout=timeout, stream=True)
                ct = r.headers.get("Content-Type","")
                data = r.content
                if r.status_code == 200 and (ct.startswith("image") or data[:2]==b"\xff\xd8"):
                    if out:
                        with open(out,"wb") as f: f.write(data)
                    return data, f"{len(data)}B from {u} (auth={'digest' if isinstance(auth,HTTPDigestAuth) else 'basic' if isinstance(auth,HTTPBasicAuth) else 'none'})"
            except Exception:
                continue
    return None, f"snapshot fetch failed, uri={uri}"

if __name__ == "__main__":
    ip = sys.argv[1] if len(sys.argv) > 1 else "10.20.51.50"
    user = sys.argv[2] if len(sys.argv) > 2 else "Admin"
    pwd  = sys.argv[3] if len(sys.argv) > 3 else "1234"
    print("info:", device_info(ip, user=user, pwd=pwd))
    data, msg = get_snapshot(ip, user=user, pwd=pwd, out=f"snap_{ip.replace('.','_')}.jpg")
    print("snapshot:", "OK" if data else "FAIL", "-", msg)
