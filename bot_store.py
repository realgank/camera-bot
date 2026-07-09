# -*- coding: utf-8 -*-
"""Волна D: общий JSON-стор для рантайм-состояния (зоны, проблемы, ППР,
напоминания, смены …). Атомарная запись (tmp + os.replace), по-файловые локи,
безопасное чтение с дефолтом. Никогда не бросает наружу при чтении."""
import os
import json
import threading

from bot_util import log_exc

_locks: dict = {}
_glock = threading.Lock()


def lock_for(path: str) -> threading.RLock:
    with _glock:
        if path not in _locks:
            _locks[path] = threading.RLock()
        return _locks[path]


def jload(path: str, default):
    """Читает JSON; при любой беде — копия default (тип должен совпасть)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, type(default)):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        log_exc(f"store: не смог прочитать {os.path.basename(path)}")
    return json.loads(json.dumps(default))  # глубокая копия дефолта


def jsave(path: str, obj) -> None:
    with lock_for(path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def jupdate(path: str, default, fn):
    """Атомарное read-modify-write: fn(data) -> data (или None — не писать).
    Возвращает актуальные данные."""
    with lock_for(path):
        data = jload(path, default)
        res = fn(data)
        if res is not None:
            jsave(path, res)
            return res
        return data
