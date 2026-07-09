# -*- coding: utf-8 -*-
"""R46: чистые функции bot_net — валидация IP, парсер arp, find_ips, allowlist."""
import sys
import unittest

sys.path.insert(0, r"C:\Users\1\camera")

import bot_net as net  # noqa: E402


class TestValidIp(unittest.TestCase):
    def test_ok(self):
        for ip in ("10.20.50.1", "192.168.0.250", "1.2.3.4", " 10.20.52.100 "):
            self.assertTrue(net.valid_ip(ip), ip)

    def test_bad(self):
        for ip in ("999.1.1.1", "10.20.50", "10.20.50.256", "", None,
                   "abc", "10.20.50.1.2", "10,20,50,1"):
            self.assertFalse(net.valid_ip(ip), repr(ip))


class TestValidPrefix(unittest.TestCase):
    def test_ok(self):
        for p in ("10.20.50", "192.168.0", "10.10.60"):
            self.assertTrue(net.valid_prefix(p), p)

    def test_bad(self):
        for p in ("10.20", "10.20.50.1", "999.1.1", "", None, "a.b.c"):
            self.assertFalse(net.valid_prefix(p), repr(p))


class TestPrefixAllowed(unittest.TestCase):
    """Дефолтный allowlist: 192.168.0.0/24, 10.20.0.0/16, 10.10.0.0/16."""
    def test_allowed(self):
        for p in ("10.20.50", "10.20.53", "10.10.60", "192.168.0"):
            self.assertTrue(net.prefix_allowed(p), p)

    def test_denied(self):
        for p in ("8.8.8", "172.16.0", "192.168.1"):
            self.assertFalse(net.prefix_allowed(p), p)


ARP_SAMPLE = """
Интерфейс: 10.20.53.249 --- 0x14
  адрес в Интернете      Физический адрес      Тип
  10.20.50.51           e0-7f-88-06-43-51     динамический
  10.20.50.52           E0-7F-88-06-43-52     динамический
  10.20.53.255          ff-ff-ff-ff-ff-ff     статический
  224.0.0.22            01-00-5e-00-00-16     статический
  строка мусора без MAC
"""


class TestParseArp(unittest.TestCase):
    def test_parse(self):
        t = net.parse_arp(ARP_SAMPLE)
        self.assertEqual(t["10.20.50.51"], "e0:7f:88:06:43:51")
        self.assertEqual(t["10.20.50.52"], "e0:7f:88:06:43:52")
        self.assertIn("10.20.53.255", t)  # broadcast тоже парсится (фильтр выше)
        self.assertEqual(len(t), 4)       # мусорная строка не попала

    def test_empty(self):
        self.assertEqual(net.parse_arp(""), {})
        self.assertEqual(net.parse_arp(None), {})


class TestFindIps(unittest.TestCase):
    def test_multi(self):
        txt = "гляньте 10.20.50.51 и 10.20.50.52, ещё раз 10.20.50.51 и 999.1.1.1"
        self.assertEqual(net.find_ips(txt), ["10.20.50.51", "10.20.50.52"])

    def test_none(self):
        self.assertEqual(net.find_ips("нет адресов"), [])
        self.assertEqual(net.find_ips(""), [])


if __name__ == "__main__":
    unittest.main()
