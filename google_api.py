# -*- coding: utf-8 -*-
"""Общий слой Google API для бота и скриптов: JWT-авторизация сервис-аккаунта
(RS256 через rsa+pyasn1 — тот же паттерн, что scripts/push_sheets.py, без gspread),
кэш access-токена (память + файл, 438), HTTP-запросы с ретраями.
Волна B: фундамент для bot_sheets (/sync, Drive-заливка снимков).
Волна I: 431 бэкофф с джиттером и уважением Retry-After (4xx не ретраим),
437 fields-маски параметром, 438 файловый кэш токена (_gtoken_cache.json —
меньше round-trip к oauth2 и подписей RSA между процессами),
445 resumable upload с докачкой чанками (multipart на дёрганом канале
теряет весь файл при обрыве).
Ключ сервис-аккаунта только читается; никакие данные тут не пишутся.
"""
import os
import base64
import json
import time
import random
import logging
import threading

import requests

from bot_util import log

DEFAULT_SA = r"C:\Users\1\.config\mcp-google-sheets\service-account.json"
SCOPE_SHEETS = "https://www.googleapis.com/auth/spreadsheets"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar"
SCOPE_ALL = SCOPE_SHEETS + " " + SCOPE_DRIVE

# 438: файловый кэш токенов — переживает рестарт бота и виден скриптам
TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_gtoken_cache.json")
CHUNK = 4 * 1024 * 1024          # 445: размер чанка (кратен 256 КиБ)

SESSION = requests.Session()

_lock = threading.Lock()
_keys = {}    # sa_path -> (sa_dict, rsa_priv)
_tokens = {}  # (sa_path, scope) -> {"tok": str, "exp": float}


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _load_key(sa_path):
    """Ключ сервис-аккаунта: PKCS#8 PEM -> rsa.PrivateKey (кэшируется).
    rsa/pyasn1_modules импортируются лениво — без них бот стартует,
    отваливаются только Google-функции."""
    with _lock:
        if sa_path in _keys:
            return _keys[sa_path]
    import rsa
    from pyasn1.codec.der import decoder as der_decoder
    from pyasn1_modules import rfc5208
    with open(sa_path, encoding="utf-8") as f:
        sa = json.load(f)
    body = "".join(l for l in sa["private_key"].splitlines() if "PRIVATE KEY" not in l)
    pki, _ = der_decoder.decode(base64.b64decode(body), asn1Spec=rfc5208.PrivateKeyInfo())
    priv = rsa.PrivateKey.load_pkcs1(bytes(pki["privateKey"]), format="DER")
    with _lock:
        _keys[sa_path] = (sa, priv)
    return sa, priv


def sa_email(sa_path=DEFAULT_SA):
    """client_email сервис-аккаунта (для диагностики 440/442)."""
    try:
        with open(sa_path, encoding="utf-8") as f:
            return json.load(f).get("client_email") or "?"
    except Exception:
        return "?"


# ---------- 438: файловый кэш токена ----------
def _cache_key(sa_path, scope):
    return f"{sa_path}|{scope}"


def _cache_load():
    try:
        with open(TOKEN_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cache_save(key, tok, exp):
    try:
        d = _cache_load()
        d[key] = {"tok": tok, "exp": exp}
        # чистим протухшие записи, чтобы файл не рос
        now = time.time()
        d = {k: v for k, v in d.items() if (v or {}).get("exp", 0) > now}
        tmp = TOKEN_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, TOKEN_CACHE)
    except Exception:
        pass  # кэш — оптимизация, не критично


def invalidate_token(sa_path=DEFAULT_SA, scope=SCOPE_ALL):
    """Сброс кэша (на 401 UNAUTHENTICATED — токен мог быть отозван)."""
    key = (sa_path, scope)
    with _lock:
        _tokens.pop(key, None)
    try:
        d = _cache_load()
        if d.pop(_cache_key(sa_path, scope), None) is not None:
            tmp = TOKEN_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, TOKEN_CACHE)
    except Exception:
        pass


def token(sa_path=DEFAULT_SA, scope=SCOPE_ALL):
    """Access-токен: память -> файл (438) -> новый JWT (кэш до истечения -60с)."""
    key = (sa_path, scope)
    with _lock:
        t = _tokens.get(key)
        if t and t["exp"] - 60 > time.time():
            return t["tok"]
    fc = _cache_load().get(_cache_key(sa_path, scope))
    if fc and fc.get("exp", 0) - 60 > time.time() and fc.get("tok"):
        with _lock:
            _tokens[key] = {"tok": fc["tok"], "exp": fc["exp"]}
        return fc["tok"]
    import rsa
    sa, priv = _load_key(sa_path)
    now = int(time.time())
    hdr = _b64u(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claim = _b64u(json.dumps({"iss": sa["client_email"], "scope": scope,
                              "aud": sa["token_uri"], "iat": now, "exp": now + 3600}).encode())
    si = hdr + b"." + claim
    assertion = (si + b"." + _b64u(rsa.sign(si, priv, "SHA-256"))).decode()
    r = SESSION.post(sa["token_uri"], timeout=30, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion})
    r.raise_for_status()
    tok = r.json()["access_token"]
    with _lock:
        _tokens[key] = {"tok": tok, "exp": now + 3600}
    _cache_save(_cache_key(sa_path, scope), tok, now + 3600)
    return tok


def _retry_sleep(r, attempt):
    """431: Retry-After, если сервер сказал; иначе экспонента с джиттером."""
    ra = None
    try:
        ra = float((getattr(r, "headers", None) or {}).get("Retry-After") or "")
    except (TypeError, ValueError):
        ra = None
    if ra is not None:
        time.sleep(min(ra, 60.0) + 0.2)
    else:
        base = min(30.0, 1.5 * (2 ** attempt))
        time.sleep(base * (0.7 + 0.6 * random.random()))


def request(method, url, sa_path=DEFAULT_SA, scope=SCOPE_ALL,
            retries=4, timeout=60, fields=None, **kw):
    """HTTP-запрос к Google API с Bearer-токеном и ретраями (431).
    Ретраим сеть/5xx/429 (уважая Retry-After); 401 — сброс токена и одна
    повторная попытка; прочие 4xx возвращаем как есть (не ретраим).
    437: fields=... добавляет маску полей к запросу (меньше трафика).
    Возвращает requests.Response; при полном провале — последнее исключение."""
    last = None
    base_hdrs = dict(kw.pop("headers", {}) or {})
    if fields:
        params = dict(kw.pop("params", {}) or {})
        params["fields"] = fields
        kw["params"] = params
    retried_401 = False
    for i in range(retries):
        try:
            hdrs = dict(base_hdrs)
            hdrs["Authorization"] = f"Bearer {token(sa_path, scope)}"
            r = SESSION.request(method, url, headers=hdrs, timeout=timeout, **kw)
        except requests.RequestException as e:
            last = e
            log(f"google {method} {url.split('?')[0]}: попытка {i + 1}/{retries}: "
                f"{type(e).__name__}", logging.WARNING)
            time.sleep(min(30.0, 1.5 * (2 ** i)) * (0.7 + 0.6 * random.random()))
            continue
        if r.status_code == 401 and not retried_401:
            retried_401 = True
            invalidate_token(sa_path, scope)
            log(f"google {method} 401 — сбросил кэш токена, повтор", logging.WARNING)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            log(f"google {method} HTTP {r.status_code}: попытка {i + 1}/{retries}",
                logging.WARNING)
            last = requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}")
            _retry_sleep(r, i)
            continue
        return r
    raise last if last else RuntimeError("google request: нет попыток")


def gjson(method, url, **kw):
    """request() + raise_for_status + .json()."""
    r = request(method, url, **kw)
    r.raise_for_status()
    return r.json()


# ---------- 445: resumable upload ----------
def chunks(total, size=CHUNK):
    """Разбиение файла на чанки: [(start, end_excl), ...] (для тестов — pure)."""
    if total <= 0:
        return [(0, 0)]
    return [(a, min(a + size, total)) for a in range(0, total, size)]


def upload_resumable(name, data, parents=None, mime="application/octet-stream",
                     app_properties=None, sa_path=DEFAULT_SA, chunk=CHUNK,
                     fields="id,md5Checksum,size"):
    """445: заливка в Drive c uploadType=resumable и докачкой по чанкам.
    На обрыве чанк ретраится, uже переданное не теряется. Возвращает dict файла."""
    meta = {"name": name}
    if parents:
        meta["parents"] = list(parents)
    if app_properties:
        meta["appProperties"] = {k: str(v)[:120] for k, v in app_properties.items()}
    r = request("POST",
                "https://www.googleapis.com/upload/drive/v3/files"
                f"?uploadType=resumable&fields={fields}",
                sa_path=sa_path, scope=SCOPE_DRIVE, json=meta,
                headers={"X-Upload-Content-Type": mime,
                         "X-Upload-Content-Length": str(len(data))})
    r.raise_for_status()
    session_url = r.headers.get("Location")
    if not session_url:
        raise RuntimeError("resumable: нет Location в ответе инициации")
    total = len(data)
    pos = 0
    fails = 0
    while pos < total or total == 0:
        a, b = pos, min(pos + chunk, total)
        try:
            rr = SESSION.put(
                session_url, data=data[a:b], timeout=120,
                headers={"Content-Range": f"bytes {a}-{b - 1}/{total}"
                         if total else f"bytes */{total}",
                         "Content-Type": mime})
        except requests.RequestException as e:
            fails += 1
            if fails > 5:
                raise
            log(f"resumable: чанк {a}-{b}: {type(e).__name__}, ретрай", logging.WARNING)
            time.sleep(1.5 * fails)
            pos = _resumable_pos(session_url, total, pos)
            continue
        if rr.status_code in (200, 201):
            return rr.json()
        if rr.status_code == 308:  # чанк принят, продолжаем
            rng = rr.headers.get("Range") or ""
            pos = int(rng.rsplit("-", 1)[-1]) + 1 if "-" in rng else b
            fails = 0
            continue
        fails += 1
        if fails > 5 or 400 <= rr.status_code < 500:
            raise requests.HTTPError(f"resumable HTTP {rr.status_code}: {rr.text[:200]}")
        _retry_sleep(rr, fails)
        pos = _resumable_pos(session_url, total, pos)
    return {}


def _resumable_pos(session_url, total, fallback):
    """Опрос статуса докачки: сколько байт сервер уже принял."""
    try:
        rr = SESSION.put(session_url, timeout=30,
                         headers={"Content-Range": f"bytes */{total}"})
        if rr.status_code == 308:
            rng = rr.headers.get("Range") or ""
            return int(rng.rsplit("-", 1)[-1]) + 1 if "-" in rng else 0
    except requests.RequestException:
        pass
    return fallback
