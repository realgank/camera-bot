# -*- coding: utf-8 -*-
"""R46: диспетчер команд — вызов, алиасы, fuzzy-подсказка, обработка ошибок."""
import sys
import unittest
from unittest import mock

sys.path.insert(0, r"C:\Users\1\camera")

import bot_handlers as h  # noqa: E402


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.sent = []
        p = mock.patch.object(
            h, "send",
            lambda chat, text, **kw: self.sent.append((chat, text)))
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

        def fake(chat, arg="", reply_to=None):
            self.calls.append((chat, arg))

        h.HANDLERS["/testcmd"] = fake
        h.ALIASES["/тесткоманда"] = "/testcmd"
        self.addCleanup(lambda: (h.HANDLERS.pop("/testcmd", None),
                                 h.ALIASES.pop("/тесткоманда", None)))

    def test_direct_call(self):
        h.dispatch(0, "/testcmd", "арг")
        self.assertEqual(self.calls, [(0, "арг")])

    def test_alias(self):
        h.dispatch(1, "/тесткоманда", "x")
        self.assertEqual(self.calls, [(1, "x")])

    def test_unknown_suggests(self):
        h.dispatch(1, "/reprot", "")  # опечатка от /report
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("/report", self.sent[0][1])

    def test_handler_exception_reported(self):
        def boom(chat, arg="", reply_to=None):
            raise RuntimeError("bang")

        h.HANDLERS["/boom"] = boom
        self.addCleanup(lambda: h.HANDLERS.pop("/boom", None))
        h.dispatch(1, "/boom", "")
        self.assertTrue(any("упала" in t for _c, t in self.sent))


class TestCallbacks(unittest.TestCase):
    def test_cb_ext_registered(self):
        # Волна C: ключевые внешние колбэки зарегистрированы
        for key in ("fpg", "fmore", "fstop", "fsub", "fgo", "favshots",
                    "help", "mshot", "mdiag", "rbt", "rbty", "unk"):
            self.assertIn(key, h.CB_EXT, key)

    def test_deeplink_parse(self):
        # U43: /start shot_10-20-50-1 -> run_action("shot", "10.20.50.1")
        ran = []
        with mock.patch.object(h, "run_action",
                               lambda chat, a, ip, **k: ran.append((a, ip))):
            h.cmd_start(1, "shot_10-20-50-1")
        self.assertEqual(ran, [("shot", "10.20.50.1")])


if __name__ == "__main__":
    unittest.main()
