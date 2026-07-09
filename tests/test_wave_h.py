# -*- coding: utf-8 -*-
"""Волна H (351-400 + 322-329): парсеры Cross-24 (lang-артефакты, аптайм,
methods), парсеры Huawei VRP на живых образцах, нормализация фактов (399),
топо-дерево и аплинки (351-352), неизвестные соседи (354), PoE-бюджет и
события портов из фонового опроса (322-329, 356-359, 376, 400) на моке
транспорта, дифф фактов (398), дрейф конфигов (372), netcheck-парсеры (390),
Confirm-TTL. Сеть и прод-файлы не трогаются; ЗАПИСЬ (set.cgi) в тестах
не выполняется вообще."""
import os
import sys
import json
import time
import base64
import tempfile
import unittest

sys.path.insert(0, r"C:\Users\1\camera")

import bot_state as st  # noqa: E402
import bot_sw_api as sw  # noqa: E402

HW_VERSION = """<2.88_AC-1>display version
Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.170 (S5736 V200R022C00SPC500)
Copyright (C) 2000-2022 HUAWEI TECH Co., Ltd.
HUAWEI S5736-S48U4XC Routing Switch uptime is 16 weeks, 2 days, 17 hours, 5 minutes
<2.88_AC-1>"""

HW_POE = """PortName               Class  REFPW(mW) USMPW(mW) CURPW(mW) PKPW(mW)  AVGPW(mW)
--------------------------------------------------------------------------------
GigabitEthernet0/0/1   -      -         90000     0         0         0
GigabitEthernet0/0/3   1      4000      90000     900       1200      800
GigabitEthernet0/0/28  1      4000      90000     1200      1700      1200
"""

HW_POE_INFO = """PSE Information of slot 0:
    User Set Max Power(mW)     : 1809000
    PoE Power Supply(mW)       : 1808000
    Available Total Power(mW)  : 1796200
    Total Power Consumption(mW): 11800
    Power Peak Value(mW)       : 14900
"""

HW_BRIEF = """Interface                   PHY   Protocol  InUti OutUti   inErrors  outErrors
Eth-Trunk1                  up    up        0.09%  0.09%          0          0
  XGigabitEthernet0/0/1     up    up        0.09%  0.09%          0          0
GigabitEthernet0/0/2        down  down         0%     0%          0          0
GigabitEthernet0/0/3        up    up           0%     0%         12          3
NULL0                       up    up(s)        0%     0%          0          0
"""

HW_LLDP = """Local Intf       Neighbor Dev             Neighbor Intf             Exptime(s)
GE0/0/1          -                        d843-ae33-d748            2759
GE0/0/7          SIP-T31P                 44db-d2fc-2dcf            171
XGE0/0/1         01.68_Stuff-Core         10GE1/0/31                99
"""

HW_TEMP = """-------------------------------------------------------------------------------
 Slot  Card  Sensor Status    Current(C) Lower(C) Lower     Upper(C) Upper
                                                  Resume(C)          Resume(C)
-------------------------------------------------------------------------------
 0     NA    NA     Normal            28       -3         1       53        49
"""

HW_NTP = """ clock status: unsynchronized
 clock stratum: 16
 reference clock ID: none
"""

HW_MAC = """4c11-bf2a-3c5d 50/-    -      -      GE0/0/5   dynamic
e07f-8812-3456 1/-     -      -      GE0/0/7   dynamic
"""


def fact_entry(ip="10.10.60.52", host="SW-1.2", mac="AE:31:9D:A9:9F:48",
               lldp=None, mac_table=None, ok=True):
    return {"ip": ip, "row": 3, "ok": ok, "err": None if ok else "boom",
            "sys": {"location": "01.68", "contact": "default",
                    "hostname": host, "syssn": "PS1", "sysMac": mac,
                    "sysUpTime": "lang('sys','txtSysUptimeArg',[63,0,27,48])",
                    "sysCurrTime": "2022-03-05 08:26:48 UTC+8 ",
                    "sec": 1646468808, "currIpv4": ip,
                    "loaderVer": "3.6.7", "fwVer": "2.0.1.10",
                    "methods": [
                        {"txt": "lang('line','lblTelnet')", "state": False},
                        {"txt": "lang('line','lblSsh')", "state": False},
                        {"txt": "lang('line','lblHttp')", "state": True},
                        {"txt": "lang('line','lblHttps')", "state": False},
                        {"txt": "lang('line','lblSnmp')", "state": False}]},
            "lldp": lldp or [], "mac_table": mac_table or [],
            "port_density": {}, "vlan_density": {}}


class Base(unittest.TestCase):
    KEYS = ("facts_switches", "sw_state_path", "metrics_db_path",
            "facts_prev_path", "config_backups_dir", "hw_switches")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wh_test_")
        self._back = {k: st.CFG.get(k) for k in self.KEYS}
        for k in self.KEYS:
            st.CFG[k] = os.path.join(self.tmp, k + ".dat")
        st.CFG["hw_switches"] = []
        st.CFG["config_backups_dir"] = os.path.join(self.tmp, "cfgbak")
        import bot_metrics as mx
        self.mx = mx
        mx.close_db()
        sw._facts.update(mtime=None, raw=[], norm=[])

    def tearDown(self):
        self.mx.close_db()
        for k, v in self._back.items():
            if v is None:
                st.CFG.pop(k, None)
            else:
                st.CFG[k] = v
        sw._facts.update(mtime=None, raw=[], norm=[])

    def write_facts(self, entries, path_key="facts_switches"):
        with open(st.CFG[path_key], "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)
        sw._facts.update(mtime=None)


class TestCross24Parsers(unittest.TestCase):
    def test_lang(self):
        self.assertEqual(sw.lang_args("lang('sys','txtSysUptimeArg',[63,0,27,48])"),
                         [63, 0, 27, 48])
        self.assertEqual(sw.lang_args("no lang"), [])
        self.assertEqual(sw.lang_label("lang('lldp','lblMacAddr')"), "MacAddr")
        self.assertEqual(sw.lang_label("lang('diag','lblCopperShort')"),
                         "CopperShort")
        self.assertEqual(sw.lang_label("plain"), "plain")

    def test_uptime_methods_ports(self):
        e = fact_entry()
        self.assertEqual(sw.c24_uptime_s(e["sys"]),
                         ((63 * 24 + 0) * 60 + 27) * 60 + 48)
        m = sw.c24_methods(e["sys"])
        self.assertTrue(m["http"])
        self.assertFalse(m["ssh"])
        self.assertEqual(sw.port_index("GE7"), 6)
        self.assertEqual(sw.port_index("TE2"), 25)
        self.assertIsNone(sw.port_index("Eth1"))

    def test_norm_switch(self):
        n = sw.norm_switch(fact_entry(lldp=[
            {"localPort": "GE25", "chassisId": "aa:bb:cc:dd:ee:ff",
             "portId": "gi14", "sysName": ""}]))
        self.assertEqual(n["kind"], "cross24")
        self.assertEqual(n["host"], "SW-1.2")
        self.assertEqual(n["lldp"][0]["id"], "AA:BB:CC:DD:EE:FF")
        self.assertGreater(n["uptime_s"], 63 * 86400 - 1)

    def test_rsa_pkcs1_structure(self):
        # e=1 -> шифртекст равен padded-сообщению: проверяем структуру PKCS#1
        modulus = "F" * 128  # 512 бит, больше любого em
        c = base64.b64decode(sw.rsa_encrypt_b64("admin", modulus, exp_hex="1"))
        self.assertEqual(len(c), 64)
        self.assertEqual(c[:2], b"\x00\x02")
        sep = c.index(b"\x00", 2)
        self.assertGreaterEqual(sep, 10)             # PS >= 8 байт
        self.assertEqual(c[sep + 1:], b"admin")
        self.assertNotIn(0, c[2:sep])                # PS без нулей


class TestHuaweiParsers(unittest.TestCase):
    def test_version(self):
        v = sw.hw_parse_version(HW_VERSION)
        self.assertEqual(v["model"], "S5736-S48U4XC")
        self.assertEqual(v["version"], "V200R022C00SPC500")
        self.assertEqual(v["uptime_s"],
                         16 * 604800 + 2 * 86400 + 17 * 3600 + 5 * 60)

    def test_poe(self):
        p = sw.hw_parse_poe_ports(HW_POE)
        self.assertEqual(p["GE0/0/3"]["cur_mw"], 900)
        self.assertEqual(p["GE0/0/28"]["peak_mw"], 1700)
        self.assertEqual(p["GE0/0/1"]["cur_mw"], 0)
        i = sw.hw_parse_poe_info(HW_POE_INFO)
        self.assertEqual(i["supply_mw"], 1808000)
        self.assertEqual(i["consume_mw"], 11800)

    def test_int_brief(self):
        b = sw.hw_parse_int_brief(HW_BRIEF)
        self.assertEqual(b["GE0/0/3"]["in_err"], 12)
        self.assertEqual(b["GE0/0/2"]["phy"], "down")
        self.assertEqual(b["XGE0/0/1"]["phy"], "up")
        self.assertNotIn("NULL0", b)  # виртуальные интерфейсы пропускаются
        self.assertEqual(sw.hw_short_if("GigabitEthernet0/0/9"), "GE0/0/9")

    def test_lldp_temp_ntp_clock_mac(self):
        n = sw.hw_parse_lldp_brief(HW_LLDP)
        self.assertEqual(len(n), 3)
        self.assertEqual(n[2]["dev"], "01.68_Stuff-Core")
        t = sw.hw_parse_temperature(HW_TEMP)
        self.assertEqual(t["current_c"], 28)
        self.assertEqual(t["upper_c"], 53)
        self.assertFalse(sw.hw_parse_ntp(HW_NTP)["sync"])
        self.assertEqual(sw.hw_parse_ntp(HW_NTP)["stratum"], 16)
        self.assertEqual(sw.hw_parse_clock("<x>display clock\n2026-07-09 00:17:37\nThursday"),
                         "2026-07-09 00:17:37")
        m = sw.hw_parse_mac_table(HW_MAC)
        self.assertEqual(m[0], {"mac": "4C:11:BF:2A:3C:5D", "vlan": 50,
                                "port": "GE0/0/5"})

    def test_hw_section(self):
        out = HW_VERSION
        sec = sw.hw_section(out, "display version")
        self.assertIn("uptime is", sec)
        self.assertNotIn("display version", sec)
        self.assertEqual(sw.hw_sysname(out), "2.88_AC-1")


class TestTopo(Base):
    def _facts3(self):
        # core (60.51) <-GE25/gi14-> leaf1 (60.59); core <-GE26-> leaf2 (60.60)
        core_mac, l1_mac, l2_mac = "AA:00:00:00:00:01", "AA:00:00:00:00:02", \
            "AA:00:00:00:00:03"
        self.write_facts([
            fact_entry("10.10.60.51", "SW-CORE", core_mac, lldp=[
                {"localPort": "gi14", "chassisId": l1_mac, "portId": "GE25",
                 "sysName": ""},
                {"localPort": "gi15", "chassisId": l2_mac, "portId": "GE25",
                 "sysName": ""}]),
            fact_entry("10.10.60.59", "SW-L1", l1_mac, lldp=[
                {"localPort": "GE25", "chassisId": core_mac, "portId": "gi14",
                 "sysName": ""},
                {"localPort": "GE7", "chassisId": "DE:AD:BE:EF:00:01",
                 "portId": "DE:AD:BE:EF:00:01", "sysName": ""}],
                mac_table=[{"vlan": 1, "mac": "E0:7F:88:00:00:01",
                            "port": "GE7"}]),
            fact_entry("10.10.60.60", "SW-L2", l2_mac, lldp=[
                {"localPort": "GE25", "chassisId": core_mac, "portId": "gi15",
                 "sysName": ""}])])

    def test_links_core_tree(self):
        self._facts3()
        import bot_topo as tp
        ls = tp.links()
        self.assertEqual(len(ls), 2)
        self.assertEqual(tp.core_ip(), "10.10.60.51")
        self.assertIn("GE25", tp.uplink_ports("10.10.60.59"))
        tree = tp.tree_lines()
        self.assertTrue(any("SW-CORE" in ln and "ядро" in ln for ln in tree))
        self.assertTrue(any("SW-L1" in ln for ln in tree))

    def test_unknown_neighbors(self):
        self._facts3()
        import bot_topo as tp
        u = tp.unknown_neighbors()
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0]["mac"], "DE:AD:BE:EF:00:01")
        self.assertEqual(u[0]["port"], "GE7")

    def test_svg(self):
        self._facts3()
        import bot_topo as tp
        svg = tp.svg_map()
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("SW-CORE", svg)
        self.assertIn("<line", svg)


class TestSwMon(Base):
    def _mock_transport(self, poe_ports_mw, linkups, uptime_args,
                        dev_power=38844, macs=None):
        def fake_get(ip, cmd):
            if cmd == "sys_sysinfo":
                return {"hostname": "SW-T", "sysUpTime":
                        f"lang('sys','txtSysUptimeArg',{uptime_args})"}
            if cmd == "poe_poe":
                return {"devPower": dev_power, "devTemp": 22,
                        "ports": [{"portPower": mw, "portStatus": bool(mw),
                                   "portEnable": True}
                                  for mw in poe_ports_mw]}
            if cmd == "panel_info":
                return {"ports": [{"linkup": u, "speed": "100", "dupFull": True}
                                  for u in linkups]}
            if cmd == "sys_cpumem":
                return {"cpu": 40, "mem": 60}
            if cmd == "mac_dynamic":
                return {"entries": macs or []}
            raise AssertionError(cmd)
        return fake_get

    def test_poll_events(self):
        self.write_facts([fact_entry("10.10.60.99", "SW-T",
                                     "AA:00:00:00:00:09")])
        import bot_sw_mon as mon
        alerts = []
        old_get, old_alert = sw.cross24_get, self.mx.owner_alert
        sw.cross24_get = self._mock_transport([2000, 0], [True, True],
                                              "[10,0,0,0]")
        self.mx.owner_alert = lambda text, silent=False: alerts.append(text)
        mon.owner_alert = self.mx.owner_alert
        try:
            mon._poll_cross24("10.10.60.99")
            # второй прогон: порт 2 упал, аптайм сбросился -> события
            sw.cross24_get = self._mock_transport([2000, 0], [True, False],
                                                  "[0,0,5,0]")
            mon._poll_cross24("10.10.60.99")
        finally:
            sw.cross24_get = old_get
            self.mx.owner_alert = old_alert
        kinds = {e["kind"] for e in self.mx.events(days=1)}
        self.assertIn("sw_reboot", kinds)          # 376
        self.assertIn("port_down", kinds)          # 400
        self.assertTrue(any("перезагрузился" in a for a in alerts))

    def test_poe_budget_alert(self):
        self.write_facts([fact_entry("10.10.60.98", "SW-B",
                                     "AA:00:00:00:00:08")])
        import bot_sw_mon as mon
        alerts = []
        old_get, old_alert = sw.cross24_get, self.mx.owner_alert
        # 350 Вт из 370 = 94% > 85%
        sw.cross24_get = self._mock_transport([15000] * 23, [True] * 23,
                                              "[1,0,0,0]", dev_power=350000)
        self.mx.owner_alert = lambda text, silent=False: alerts.append(text)
        try:
            mon._poll_cross24("10.10.60.98")
        finally:
            sw.cross24_get = old_get
            self.mx.owner_alert = old_alert
        self.assertIn("poe_budget", self.mx.event_counts(1))     # 358
        self.assertTrue(any("PoE-бюджет" in a for a in alerts))

    def test_multi_mac_event(self):
        # камера E0:7F:88… + чужой MAC на том же порту -> multi_mac (329)
        cam_mac = None
        import bot_inventory as inv
        for c in inv.cams():
            if c.get("nmac"):
                cam_mac = c
                break
        if not cam_mac:
            self.skipTest("в инвентаре нет камер с MAC")
        raw = cam_mac["nmac"]
        mac_h = ":".join(raw[i:i + 2] for i in range(0, 12, 2)).upper()
        self.write_facts([fact_entry("10.10.60.97", "SW-M",
                                     "AA:00:00:00:00:07")])
        import bot_sw_mon as mon
        alerts = []
        old_get, old_alert = sw.cross24_get, self.mx.owner_alert
        macs = [{"macAddr": mac_h, "port": "GE5", "vlan": 1},
                {"macAddr": "DE:AD:BE:EF:11:22", "port": "GE5", "vlan": 1}]
        sw.cross24_get = self._mock_transport([2000], [True], "[1,0,0,0]",
                                              macs=macs)
        self.mx.owner_alert = lambda text, silent=False: alerts.append(text)
        try:
            mon._poll_cross24("10.10.60.97")
        finally:
            sw.cross24_get = old_get
            self.mx.owner_alert = old_alert
        self.assertIn("multi_mac", self.mx.event_counts(1))

    def test_gw_targets_default(self):
        import bot_sw_mon as mon
        t = mon.gw_targets()
        self.assertIn("10.20.5.1", t)
        self.assertTrue(any(x.endswith(".254") for x in t))


class TestCfg(Base):
    def test_strip_volatile(self):
        import bot_sw_cfg as cfg
        d = {"a": 1, "sysUpTime": "x", "nested": [{"portPower": 5, "b": 2}]}
        s = cfg.strip_volatile(d)
        self.assertEqual(s, {"a": 1, "nested": [{"b": 2}]})

    def test_drift_check(self):
        import bot_sw_cfg as cfg
        d = os.path.join(st.CFG["config_backups_dir"], "10.10.60.52")
        os.makedirs(d)
        with open(os.path.join(d, "2026-07-08.json"), "w") as f:
            f.write('{"hostname": "SW-1"}')
        with open(os.path.join(d, "2026-07-09.json"), "w") as f:
            f.write('{"hostname": "SW-2"}')
        diff = cfg.drift_check("10.10.60.52")
        self.assertIn("-", diff)
        self.assertIn("SW-2", diff)
        # одинаковые -> пусто
        with open(os.path.join(d, "2026-07-10.json"), "w") as f:
            f.write('{"hostname": "SW-2"}')
        self.assertEqual(cfg.drift_check("10.10.60.52"), "")

    def test_facts_diff(self):
        import bot_sw_cfg as cfg
        prev = [fact_entry("10.10.60.52", mac_table=[
            {"vlan": 1, "mac": "E0:7F:88:00:00:01", "port": "GE1"}])]
        cur = [fact_entry("10.10.60.52", mac_table=[
            {"vlan": 1, "mac": "E0:7F:88:00:00:02", "port": "GE2"}])]
        with open(st.CFG["facts_prev_path"], "w", encoding="utf-8") as f:
            json.dump(prev, f)
        self.write_facts(cur)
        d = cfg.facts_diff()
        self.assertEqual(len(d["macs_new"]), 1)
        self.assertEqual(len(d["macs_gone"]), 1)
        self.assertEqual(d["macs_new"][0][2], "GE2")


class TestAudit(Base):
    def test_reports_on_synthetic(self):
        self.write_facts([fact_entry("10.10.60.52", mac_table=[
            {"vlan": 1, "mac": "E0:7F:88:99:99:99", "port": "GE3"},
            {"vlan": 85, "mac": "DE:AD:BE:EF:00:02", "port": "GE4"}])])
        import bot_sw_audit as au
        wo, _dead = au.audit_ports()
        self.assertTrue(any(x["mac"] == "E0:7F:88:99:99:99" for x in wo))  # 360
        vs = au.vlan_summary()
        self.assertIn(1, vs)
        self.assertIn(85, vs)
        sv = au.svc_report()          # telnet off, http без https, ssh нет
        self.assertEqual(len(sv), 1)
        self.assertIn("http без https", sv[0][2])
        ct = au.contact_report()      # contact=default
        self.assertEqual(len(ct), 1)
        tr = au.time_report()         # часы 2022 года — большой дрейф
        self.assertEqual(len(tr), 1)
        self.assertLess(tr[0][3], -300)


class TestNetcheck(unittest.TestCase):
    def test_parsers(self):
        import bot_netcheck as nc
        txt = ('Конфигурация интерфейса "Ethernet 4"\n'
               "    DHCP включен:                         Нет\n"
               "    IP-адрес                           10.20.50.240\n"
               "    IP-адрес                           10.10.60.240\n"
               'Конфигурация интерфейса "Ethernet"\n'
               "    IP-адрес                           10.20.5.178\n")
        a = nc.parse_ipcfg_addrs(txt)
        self.assertEqual(len(a["Ethernet 4"]), 2)
        self.assertEqual(a["Ethernet"], ["10.20.5.178"])
        # реальная RU-локаль Windows 11 этой машины
        self.assertEqual(nc.parse_dad(
            "Передачи в рамках обнаружения повторяющихся адресов"
            "                      : 0"), 0)
        self.assertEqual(nc.parse_dad("DAD Transmits : 3"), 3)
        self.assertEqual(nc.parse_metric(
            "MTU канала : 1500 байт\nМетрика       : 1"), 1)
        self.assertEqual(nc.parse_metric("Metric : 25"), 25)
        dup = nc.parse_duplicates(" 10.20.51.240   Duplicate\n"
                                  " 169.254.1.2  Tentative\n 1.2.3.4 Preferred")
        self.assertEqual(dup, [("10.20.51.240", "Duplicate")])
        tr = "Трассировка\n  1     2 ms   1 ms   1 ms  10.20.5.1\n"
        self.assertEqual(nc.parse_tracert_first_hop(tr), "10.20.5.1")

    def test_fix_commands(self):
        import bot_netcheck as nc
        cmds = nc.fix_commands("Ethernet 4")
        self.assertTrue(any("dadtransmits=0" in " ".join(c) for c in cmds))
        self.assertTrue(any("metric=1" in " ".join(c) for c in cmds))
        # /25-маршруты на каждую камерную и свитчевую /24 (по две половины)
        n_routes = sum(1 for c in cmds if "add" in c)
        self.assertEqual(n_routes, 2 * len(nc.cam_prefixes()))


class TestConfirm(unittest.TestCase):
    def test_ttl(self):
        c = sw.Confirm()
        c.put("k", {"x": 1})
        self.assertEqual(c.take("k"), {"x": 1})
        self.assertIsNone(c.take("k"))            # одноразовое
        c.put("k2", 1)
        c._p["k2"] = (time.time() - 999, 1)       # протухло
        self.assertIsNone(c.take("k2"))


if __name__ == "__main__":
    unittest.main()
