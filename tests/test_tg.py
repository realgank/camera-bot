# -*- coding: utf-8 -*-
"""R47: tg() на моках — 429 (retry_after), 409 (exit), таймаут, не-JSON (DPI),
плюс R46: send_chunks не превышает лимит Telegram 4096."""
import sys
import unittest
from unittest import mock

sys.path.insert(0, r"C:\Users\1\camera")

import requests  # noqa: E402
import bot_tg as tgm  # noqa: E402


class Resp:
    """Мок requests.Response: json() либо отдаёт словарь, либо ValueError."""
    def __init__(self, j=None, status=200):
        self._j = j
        self.status_code = status

    def json(self):
        if self._j is None:
            raise ValueError("not json (DPI-заглушка)")
        return self._j


class TestTg(unittest.TestCase):
    def setUp(self):
        # без реального сна и без троттлинга
        p1 = mock.patch.object(tgm.time, "sleep", lambda *_a: None)
        p1.start()
        self.addCleanup(p1.stop)

    def test_timeout_retries_then_none(self):
        post = mock.Mock(side_effect=requests.Timeout("boom"))
        with mock.patch.object(tgm.SESSION, "post", post):
            r = tgm.tg("getMe", {}, retries=3)
        self.assertIsNone(r)
        self.assertEqual(post.call_count, 3)  # R4: каждая попытка сделана

    def test_non_json_dpi_stub(self):
        post = mock.Mock(return_value=Resp(None, status=200))
        with mock.patch.object(tgm.SESSION, "post", post):
            r = tgm.tg("getMe", {}, retries=2)
        self.assertIsNone(r)
        self.assertEqual(post.call_count, 2)

    def test_429_waits_and_retries(self):
        seq = [Resp({"ok": False, "error_code": 429,
                     "parameters": {"retry_after": 0}}),
               Resp({"ok": True, "result": {"id": 1}})]
        post = mock.Mock(side_effect=seq)
        with mock.patch.object(tgm.SESSION, "post", post):
            r = tgm.tg("sendMessage", {"chat_id": 1, "text": "x"}, retries=3)
        self.assertTrue(r and r["ok"])
        self.assertEqual(post.call_count, 2)

    def test_409_exits(self):
        post = mock.Mock(return_value=Resp(
            {"ok": False, "error_code": 409, "description": "Conflict"}))
        with mock.patch.object(tgm.SESSION, "post", post):
            with self.assertRaises(SystemExit):  # главный поток -> sys.exit(1)
                tgm.tg("getUpdates", {}, retries=3)

    def test_400_no_retry(self):
        post = mock.Mock(return_value=Resp(
            {"ok": False, "error_code": 400, "description": "Bad Request"}))
        with mock.patch.object(tgm.SESSION, "post", post):
            r = tgm.tg("sendMessage", {}, retries=5)
        self.assertFalse(r["ok"])
        self.assertEqual(post.call_count, 1)  # R33: 400 не ретраим


class TestSendChunks(unittest.TestCase):
    def test_limit_4096(self):
        sent = []
        with mock.patch.object(tgm, "send", lambda chat, text: sent.append(text)):
            lines = [f"строка {i} " + "x" * 90 for i in range(300)]
            tgm.send_chunks(1, lines)
        self.assertGreater(len(sent), 1)
        for msg in sent:
            self.assertLessEqual(len(msg), 4096)
        # контент не потерян и порядок сохранён
        self.assertEqual("\n".join(sent), "\n".join(lines))

    def test_single_message(self):
        sent = []
        with mock.patch.object(tgm, "send", lambda chat, text: sent.append(text)):
            tgm.send_chunks(1, ["a", "b"])
        self.assertEqual(sent, ["a\nb"])


if __name__ == "__main__":
    unittest.main()
