# -*- coding: utf-8 -*-
"""Пульт camera_bot — GUI без консоли (tkinter, запускать через pythonw).

Показывает: статус процесса бота (PID/аптайм задачи), хвост camera_bot.log
с автообновлением. Кнопки: запустить/остановить/перезапустить (через задачу
планировщика camera_bot + добивание процессов), открыть лог, Google-таблицу.
Закрытие окна бота НЕ трогает — он живёт в планировщике."""
import json
import os
import subprocess
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "camera_bot.log")
CFG = os.path.join(BASE, "tg_bot_config.json")
TASK = "camera_bot"
NOWIN = subprocess.CREATE_NO_WINDOW
TAIL_LINES = 80


def _run(args, timeout=15):
    """Тихий запуск команды без окна; вернуть stdout (cp866→как есть)."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=NOWIN)
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def _ps(cmd, timeout=20):
    return _run(["powershell", "-NoProfile", "-Command", cmd], timeout)


def bot_pids():
    """PID'ы python-процессов camera_bot.py и cmd-обёртки run_bot.cmd."""
    out = _ps("Get-CimInstance Win32_Process -Filter \"Name like 'python%' or "
              "Name='cmd.exe'\" | ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")
    py, wrap = [], []
    for line in out.splitlines():
        pid, _, cl = line.partition("|")
        if "camera_bot.py" in cl:
            py.append(pid.strip())
        elif "run_bot.cmd" in cl:
            wrap.append(pid.strip())
    return py, wrap


def sheet_url():
    try:
        sid = json.load(open(CFG, encoding="utf-8-sig")).get("sheet_id", "")
        return f"https://docs.google.com/spreadsheets/d/{sid}/edit" if sid else ""
    except Exception:
        return ""


def tail_log(n=TAIL_LINES):
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 120_000))
            lines = f.read().decode("utf-8-sig", "replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"(лог недоступен: {e})"


class Gui:
    def __init__(self, root):
        self.root = root
        root.title("Пульт camera_bot")
        root.geometry("900x560")
        root.minsize(640, 400)

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        self.status = ttk.Label(top, text="…", font=("Segoe UI", 11, "bold"))
        self.status.pack(side="left")

        btns = ttk.Frame(root, padding=(8, 0, 8, 8))
        btns.pack(fill="x")
        for text, cmd in (("▶ Запустить", self.start),
                          ("⏹ Остановить", self.stop),
                          ("🔄 Перезапустить", self.restart),
                          ("📄 Лог в блокноте", self.open_log),
                          ("📊 Google-таблица", self.open_sheet)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=3)

        self.txt = tk.Text(root, wrap="none", font=("Consolas", 9),
                           bg="#111418", fg="#d7dce2", state="disabled")
        self.txt.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        sb = ttk.Scrollbar(self.txt, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        self._busy = False
        self.refresh_log()
        self.refresh_status()

    # ---------- статус/лог ----------
    def refresh_status(self):
        if not self._busy:
            py, wrap = bot_pids()
            if py:
                self.status.config(
                    text=f"🟢 Бот работает (PID {', '.join(py)}; обёртка: "
                         f"{'да' if wrap else 'нет'})", foreground="#1a7f37")
            else:
                self.status.config(text="🔴 Бот остановлен", foreground="#b42318")
        self.root.after(3000, self.refresh_status)

    def refresh_log(self):
        at_end = self.txt.yview()[1] > 0.98
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", tail_log())
        self.txt.config(state="disabled")
        if at_end:
            self.txt.see("end")
        self.root.after(2000, self.refresh_log)

    # ---------- управление ----------
    def _kill_all(self):
        py, wrap = bot_pids()
        for pid in py + wrap:
            _run(["taskkill", "/PID", pid, "/T", "/F"])

    def start(self):
        self._busy = True
        self.status.config(text="⏳ Запускаю…", foreground="#9a6700")
        self.root.update_idletasks()
        _run(["schtasks", "/run", "/tn", TASK])
        time.sleep(2)
        self._busy = False

    def stop(self):
        self._busy = True
        self.status.config(text="⏳ Останавливаю…", foreground="#9a6700")
        self.root.update_idletasks()
        _run(["schtasks", "/end", "/tn", TASK])
        self._kill_all()
        self._busy = False

    def restart(self):
        self._busy = True
        self.status.config(text="⏳ Перезапускаю…", foreground="#9a6700")
        self.root.update_idletasks()
        _run(["schtasks", "/end", "/tn", TASK])
        self._kill_all()
        time.sleep(1)
        _run(["schtasks", "/run", "/tn", TASK])
        time.sleep(2)
        self._busy = False

    def open_log(self):
        try:
            os.startfile(LOG)
        except Exception:
            pass

    def open_sheet(self):
        url = sheet_url()
        if url:
            webbrowser.open(url)


if __name__ == "__main__":
    _root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    Gui(_root)
    _root.mainloop()
