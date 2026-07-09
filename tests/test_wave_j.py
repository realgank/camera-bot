# -*- coding: utf-8 -*-
"""Волна J (DEFER): юнит-тесты чистых функций. НИКАКИХ живых операций записи
и PoE — только моки и временные файлы."""
import os
import sys
import json
import time
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, r"C:\Users\1\camera")

import bot_state as st  # noqa: E402
import bot_inventory as inv  # noqa: E402


IFACES_XML = """<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">
<SOAP-ENV:Body><tds:GetNetworkInterfacesResponse>
<tds:NetworkInterfaces token="NetworkInterfaceToken_1">
 <tt:Enabled>true</tt:Enabled>
 <tt:Info><tt:Name>eth0</tt:Name><tt:HwAddress>e0:7f:88:06:43:51</tt:HwAddress></tt:Info>
 <tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config>
  <tt:Manual><tt:Address>192.168.0.250</tt:Address><tt:PrefixLength>24</tt:PrefixLength></tt:Manual>
  <tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4>
</tds:NetworkInterfaces>
</tds:GetNetworkInterfacesResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""


class TestProvisionParse(unittest.TestCase):
    def test_parse_ifaces(self):
        import bot_provision as bp
        r = bp.parse_ifaces(IFACES_XML)
        self.assertEqual(r["token"], "NetworkInterfaceToken_1")
        self.assertEqual(r["hwaddr"], "E0:7F:88:06:43:51")
        self.assertEqual(r["addr"], "192.168.0.250")
        self.assertEqual(r["prefix"], 24)
        self.assertFalse(r["dhcp"])

    def test_parse_ifaces_broken(self):
        import bot_provision as bp
        r = bp.parse_ifaces("<xml>мусор</xml>")
        self.assertIsNone(r["token"])
        self.assertEqual(r["hwaddr"], "")

    def test_set_ip_body(self):
        import bot_provision as bp
        b = bp._set_ip_body("tok1", "10.20.50.123", 24)
        self.assertIn("<InterfaceToken>tok1</InterfaceToken>", b)
        self.assertIn("<Address>10.20.50.123</Address>", b)
        self.assertIn("<PrefixLength>24</PrefixLength>", b)
        self.assertIn("<DHCP>false</DHCP>", b)


class TestProvisionTarget(unittest.TestCase):
    """I38: валидация целевого IP."""
    def test_target_check(self):
        import bot_provision as bp
        taken = {"10.20.50.51", "10.20.50.52"}
        ok, why = bp.target_check("10.20.50.123", taken, alive=False)
        self.assertTrue(ok, why)
        self.assertFalse(bp.target_check("999.1.1.1", taken, False)[0])
        self.assertFalse(bp.target_check("10.20.50.51", taken, False)[0])  # инвентарь
        self.assertFalse(bp.target_check("10.20.50.123", taken, True)[0])  # живой
        self.assertFalse(bp.target_check("10.20.50.254", taken, False)[0])  # шлюз
        self.assertFalse(bp.target_check("10.20.50.0", taken, False)[0])
        self.assertFalse(bp.target_check("10.20.50.255", taken, False)[0])
        self.assertFalse(bp.target_check(st.cget("health_factory_ip"),
                                         taken, False)[0])  # заводской

    def test_gateway_retry_http500(self):
        """Ловушка окна ребута: два провала (HTTP 500) -> успех с 3-й."""
        import bot_provision as bp
        calls = {"n": 0}

        def fake_call(ip, action, body, ns=None, timeout=6):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"error": "HTTP 500", "auth": False, "ms": 1}
            return {"text": "<ok/>", "ms": 1}

        def fake_get_net(ip, timeout=6):
            if calls["n"] < 3:
                raise ConnectionError("RemoteDisconnected")
            return {"dhcp": False, "gateway": "10.20.50.254"}

        with mock.patch.object(bp.oq, "call", fake_call), \
                mock.patch.object(bp.oq, "get_net", fake_get_net):
            ok, tries = bp.set_gateway("10.20.50.123", "10.20.50.254",
                                       retries=6, delay=0, sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(tries, 3)

    def test_gateway_fail_all(self):
        import bot_provision as bp
        with mock.patch.object(bp.oq, "call",
                               lambda *a, **k: {"error": "HTTP 500"}), \
                mock.patch.object(bp.oq, "get_net",
                                  lambda *a, **k: {"gateway": ""}):
            ok, msg = bp.set_gateway("10.20.50.123", "10.20.50.254",
                                     retries=3, delay=0, sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("3", str(msg))


class TestMacfillPlan(unittest.TestCase):
    """I44: превью заполнений — ONVIF приоритетнее ARP, формат канонический."""
    def test_plan_writes(self):
        import bot_macfill as mf
        cams = {"10.20.50.1": {"ip": "10.20.50.1", "row": 5, "mac": None},
                "10.20.50.2": {"ip": "10.20.50.2", "row": 6, "mac": None},
                "10.20.50.3": {"ip": "10.20.50.3", "row": 7, "mac": None}}
        results = [("10.20.50.1", "e0:7f:88:01:02:03", True),   # ONVIF
                   ("10.20.50.2", None, True),                  # только ARP
                   ("10.20.50.3", None, False)]                 # молчит
        arp = {"10.20.50.1": "aa-bb-cc-dd-ee-ff",
               "10.20.50.2": "e0-7f-88-0a-0b-0c"}
        writes, lines = mf.plan_writes(results, arp, cams)
        self.assertEqual(len(writes), 2)
        w1 = [w for w in writes if w[1] == 5][0]
        self.assertEqual(w1[4], "E0:7F:88:01:02:03")  # ONVIF, не ARP
        self.assertEqual(w1[2], "MAC-адрес")
        w2 = [w for w in writes if w[1] == 6][0]
        self.assertEqual(w2[4], "E0:7F:88:0A:0B:0C")
        self.assertEqual(len(lines), 3)  # молчащая тоже в превью

    def test_plan_skips_alien_rows(self):
        import bot_macfill as mf
        writes, _ = mf.plan_writes([("1.2.3.4", "e0:7f:88:01:02:03", True)],
                                   {}, {})
        self.assertEqual(writes, [])


class TestUnknownQueue(unittest.TestCase):
    """I36: фильтр и накопление очереди «Неизвестных»."""
    def test_filter_new(self):
        import bot_unknownq as uq
        hosts = {"10.20.50.51": "e0:7f:88:aa:bb:cc",    # IP в инвентаре
                 "10.20.50.99": "e0:7f:88:aa:bb:cc",    # MAC в инвентаре
                 "10.20.50.98": "00:11:22:33:44:55",    # настоящий новый
                 "10.20.50.97": "—",                    # новый без MAC
                 "10.10.60.52": "00:11:22:33:44:66",    # свитч-подсеть
                 "10.20.50.254": "00:11:22:33:44:77",   # шлюз
                 "192.168.0.250": "00:11:22:33:44:88"}  # заводской
        out = uq.filter_new(hosts, {"10.20.50.51"},
                            {inv.norm_mac("e0:7f:88:aa:bb:cc")},
                            ["10.10.60"], "192.168.0.250")
        self.assertEqual(sorted(e["ip"] for e in out.values()),
                         ["10.20.50.97", "10.20.50.98"])
        self.assertIn(inv.norm_mac("00:11:22:33:44:55"), out)
        self.assertIn("10.20.50.97", out)  # ключ = IP, когда MAC не пойман

    def test_note_hosts_accumulate(self):
        import bot_unknownq as uq
        with tempfile.TemporaryDirectory() as td:
            qp = os.path.join(td, "_unknown_queue.json")
            with mock.patch.dict(st.CFG, {"unknown_queue_path": qp}), \
                    mock.patch.object(inv, "cams", lambda: [
                        {"ip": "10.20.50.51", "nmac": "e07f88aabbcc"}]):
                n = uq.note_hosts({"10.20.50.98": "00:11:22:33:44:55",
                                   "10.20.50.51": "e0:7f:88:aa:bb:cc"},
                                  source="test")
                self.assertEqual(n, 1)
                n = uq.note_hosts({"10.20.50.98": "00:11:22:33:44:55"},
                                  source="test")
                q = uq.queue()
                self.assertEqual(len(q), 1)
                e = list(q.values())[0]
                self.assertEqual(e["seen"], 2)
                self.assertEqual(e["ip"], "10.20.50.98")


class TestAutosync(unittest.TestCase):
    """I27: логика «пора ли синкать»."""
    def test_should_sync(self):
        import bot_autosync as a
        now = time.time()
        self.assertFalse(a.should_sync(True, 0, 0, now))       # выключен
        self.assertFalse(a.should_sync(False, 6, 0, now))      # чистый
        self.assertTrue(a.should_sync(True, 6, 0, now))        # давно не бегал
        self.assertFalse(a.should_sync(True, 6, now - 3600, now))  # рано
        self.assertTrue(a.should_sync(True, 1, now - 3700, now))
        self.assertFalse(a.should_sync(True, "мусор", 0, now))

    def test_mark_clear_dirty(self):
        import bot_autosync as a
        with tempfile.TemporaryDirectory() as td:
            sp = os.path.join(td, "_autosync.json")
            with mock.patch.dict(st.CFG, {"autosync_state_path": sp}):
                a.mark_dirty("/note 1.2.3.4")
                s = a.state()
                self.assertTrue(s["dirty"])
                self.assertEqual(s["reason"], "/note 1.2.3.4")
                a.clear_dirty()
                self.assertFalse(a.state()["dirty"])
                self.assertTrue(a.state()["last_run"] > 0)


class TestInlineBuilder(unittest.TestCase):
    """U44: билдер inline-результатов."""
    RECS = [{"ip": f"10.20.50.{i}", "name": f"AS-7C.{i:02d}", "row": i,
             "location": "Лестница", "model": "APIX Bullet",
             "mac": "E0:7F:88:00:00:0" + str(i % 10)} for i in range(1, 15)]

    def test_limit_and_fields(self):
        import bot_inline as il
        res = il.build_inline_results("as7c", self.RECS)
        self.assertEqual(len(res), 10)  # лимит Telegram
        ids = [r["id"] for r in res]
        self.assertEqual(len(ids), len(set(ids)))  # уникальные id
        r0 = res[0]
        self.assertEqual(r0["type"], "article")
        self.assertEqual(r0["title"], "AS-7C.01")
        self.assertIn("10.20.50.1", r0["description"])
        msg = r0["input_message_content"]
        self.assertEqual(msg["parse_mode"], "HTML")
        self.assertIn("<code>10.20.50.1</code>", msg["message_text"])
        self.assertIn("APIX Bullet", msg["message_text"])

    def test_empty_and_garbage(self):
        import bot_inline as il
        self.assertEqual(il.build_inline_results("x", []), [])
        res = il.build_inline_results("x", [None, "мусор",
                                            {"ip": "1.2.3.4", "row": 1}])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "1.2.3.4")

    def test_html_escaped(self):
        import bot_inline as il
        res = il.build_inline_results("", [{"ip": "1.2.3.4", "row": 1,
                                            "name": "<b>злая</b>"}])
        self.assertNotIn("<b>злая", res[0]["input_message_content"]["message_text"])


class TestPoePortGuard(unittest.TestCase):
    """I12: отказ при >1 MAC на порту / не-медном порте."""
    def test_guard(self):
        import bot_poe as bp
        nmac = inv.norm_mac("e0:7f:88:01:02:03")
        base = {"sw_ip": "10.10.60.52", "host": "sw", "port": "GE7"}
        self.assertIsNone(bp.port_guard(
            {**base, "macs": ["e0:7f:88:01:02:03"]}, nmac))
        self.assertIsNone(bp.port_guard({**base, "macs": []}, nmac))  # молчит
        # два MAC = отказ
        self.assertIsNotNone(bp.port_guard(
            {**base, "macs": ["e0:7f:88:01:02:03", "aa:bb:cc:dd:ee:ff"]}, nmac))
        # один, но чужой MAC = отказ
        self.assertIsNotNone(bp.port_guard(
            {**base, "macs": ["aa:bb:cc:dd:ee:ff"]}, nmac))
        # аплинк TE = отказ
        self.assertIsNotNone(bp.port_guard(
            {**base, "port": "TE1", "macs": ["e0:7f:88:01:02:03"]}, nmac))
        self.assertIsNotNone(bp.port_guard(
            {**base, "port": "мусор", "macs": []}, nmac))

    def test_find_port_live(self):
        """MAC виден живьём на access-порту (мок транспорта)."""
        import bot_poe as bp
        mac = "e0:7f:88:01:02:03"

        def fake_get(ip, cmd):
            assert cmd == "mac_dynamic"
            return {"entries": [
                {"port": "GE7", "macAddr": mac},
                {"port": "TE1", "macAddr": "aa:aa:aa:aa:aa:01"},
                {"port": "TE1", "macAddr": "aa:aa:aa:aa:aa:02"}]}

        with mock.patch.object(bp.sw, "cross24_get", fake_get), \
                mock.patch.object(bp.inv, "get", lambda ip: {"sw_ip": None}), \
                mock.patch.object(bp.inv, "switch_ports", lambda m: [
                    {"host": "SW-1", "sw_ip": "10.10.60.52", "port": "GE7",
                     "vlan": 1, "density": 1}]):
            info, err = bp.find_port("10.20.50.51", mac)
        self.assertIsNone(err)
        self.assertEqual(info["port"], "GE7")
        self.assertTrue(info["live"])
        self.assertIsNone(bp.port_guard(info, inv.norm_mac(mac)))

    def test_find_port_no_switch(self):
        import bot_poe as bp
        with mock.patch.object(bp.inv, "get", lambda ip: None), \
                mock.patch.object(bp.inv, "switch_ports", lambda m: []):
            info, err = bp.find_port("10.20.50.51", "e0:7f:88:01:02:03")
        self.assertIsNone(info)
        self.assertIn("не знаю свитч", err)


class TestClipHelpers(unittest.TestCase):
    def test_rtsp_creds(self):
        import bot_media_ops as mo
        u = mo._rtsp_with_creds("rtsp://10.20.50.51:554/ch01", "Admin", "1234")
        self.assertEqual(u, "rtsp://Admin:1234@10.20.50.51:554/ch01")
        # креды уже есть — не дублируем
        self.assertEqual(mo._rtsp_with_creds(u, "Admin", "1234"), u)

    def test_ffmpeg_version_live(self):
        """Живой read-only вызов: просто не должен падать."""
        import bot_media_ops as mo
        v = mo.ffmpeg_version()
        self.assertTrue(v is None or isinstance(v, str))


if __name__ == "__main__":
    unittest.main()
