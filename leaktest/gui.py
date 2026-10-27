"""
Tkinter front end.

The test sequence runs on a worker thread; it talks to the UI only through
a queue that the Tk main loop drains every 100 ms (Tk is not thread-safe).
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .config import Config
from .excel_log import log_result, read_recent
from .fbus import FBusInstrument
from .runner import Progress, TestAborted, TestResult, run_leak_test
from .simulator import SimulatedInstrument

# ── palette ──────────────────────────────────────────────────────
NAVY = "#1F4E79"
BG = "#F4F6F9"
CARD = "#FFFFFF"
MUTED = "#6B7280"
TEXT = "#111827"
PASS_BG, PASS_FG = "#DCFCE7", "#166534"
FAIL_BG, FAIL_FG = "#FEE2E2", "#991B1B"
IDLE_BG, IDLE_FG = "#EEF2F7", "#6B7280"
BORDER = "#E5E7EB"


def _font(size=10, weight="normal", family=None):
    if family is None:
        family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
    return (family, size, weight)


class Card(tk.Frame):
    """White panel with a small heading."""

    def __init__(self, master, title: str, **kw):
        super().__init__(master, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1, highlightcolor=BORDER, **kw)
        tk.Label(self, text=title.upper(), bg=CARD, fg=MUTED,
                 font=_font(8, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        self.body = tk.Frame(self, bg=CARD)
        self.body.pack(fill="both", expand=True, padx=14, pady=(0, 14))


class LeakTestApp(tk.Tk):
    def __init__(self, cfg: Config, force_simulate: bool = False):
        super().__init__()
        self.cfg = cfg
        self.force_simulate = force_simulate   # --simulate: not saved to config
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()

        self.title("Leak Tester")
        self.geometry("1040x780")
        self.minsize(920, 720)
        self.configure(bg=BG)
        self._init_styles()
        self._build()
        self._refresh_mode_badge()
        self._load_history()
        self._set_running(False)
        self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ══════════════════════════════════════════════════════════════
    #  Layout
    # ══════════════════════════════════════════════════════════════
    def _init_styles(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("TEntry", padding=6)
        st.configure("Primary.TButton", font=_font(11, "bold"), padding=(12, 10),
                     background=NAVY, foreground="white", borderwidth=0)
        st.map("Primary.TButton",
               background=[("disabled", "#9CA3AF"), ("active", "#163A5C")])
        st.configure("Secondary.TButton", font=_font(10), padding=(10, 6))
        st.configure("Test.Horizontal.TProgressbar", troughcolor=IDLE_BG,
                     background=NAVY, thickness=12, borderwidth=0)
        st.configure("Treeview", rowheight=26, font=_font(10), borderwidth=0)
        st.configure("Treeview.Heading", font=_font(9, "bold"), foreground=MUTED)

    def _build(self):
        # Header bar
        header = tk.Frame(self, bg=NAVY, height=56)
        header.pack(fill="x")
        tk.Label(header, text="Leak Tester", bg=NAVY, fg="white",
                 font=_font(15, "bold")).pack(side="left", padx=18, pady=12)
        tk.Label(header, text=f"F-BUS  ·  v{__version__}", bg=NAVY, fg="#B9CBE0",
                 font=_font(9)).pack(side="left", pady=(6, 0))
        self.mode_badge = tk.Label(header, bg="#2E6497", fg="white",
                                   font=_font(9, "bold"), padx=10, pady=3)
        self.mode_badge.pack(side="right", padx=18)

        # ── footer ───────────────────────────────────────────────
        foot = tk.Frame(self, bg=BG)
        foot.pack(side="bottom", fill="x", padx=16, pady=(0, 12))
        self.file_lbl = tk.Label(foot, bg=BG, fg=MUTED, font=_font(9), anchor="w")
        self.file_lbl.pack(side="left", fill="x", expand=True)
        ttk.Button(foot, text="Settings", style="Secondary.TButton",
                   command=self.open_settings).pack(side="right")
        ttk.Button(foot, text="Open results file", style="Secondary.TButton",
                   command=self.open_results).pack(side="right", padx=(0, 8))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=16)
        body.columnconfigure(0, weight=0, minsize=300)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)

        # ── left: setup ──────────────────────────────────────────
        setup = Card(body, "New test")
        setup.grid(row=0, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        b = setup.body
        tk.Label(b, text="Operator", bg=CARD, fg=TEXT, font=_font(10)).pack(anchor="w")
        self.operator_var = tk.StringVar(value=self.cfg.last_operator)
        self.operator_entry = ttk.Entry(b, textvariable=self.operator_var, font=_font(11))
        self.operator_entry.pack(fill="x", pady=(2, 10))
        tk.Label(b, text="Device number", bg=CARD, fg=TEXT, font=_font(10)).pack(anchor="w")
        self.device_var = tk.StringVar()
        self.device_entry = ttk.Entry(b, textvariable=self.device_var, font=_font(13))
        self.device_entry.pack(fill="x", pady=(2, 4))
        self.device_entry.bind("<Return>", lambda e: self.start_test())
        tk.Label(b, text="Type or scan, then press Enter", bg=CARD, fg=MUTED,
                 font=_font(8)).pack(anchor="w", pady=(0, 12))
        self.start_btn = ttk.Button(b, text="Start test", style="Primary.TButton",
                                    command=self.start_test)
        self.start_btn.pack(fill="x")
        self.abort_btn = ttk.Button(b, text="Abort", style="Secondary.TButton",
                                    command=self.abort_test)
        self.abort_btn.pack(fill="x", pady=(8, 0))

        # ── left: last result ────────────────────────────────────
        res = Card(body, "Result")
        res.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        rb = res.body
        self.result_tile = tk.Label(rb, text="—", font=_font(28, "bold"),
                                    bg=IDLE_BG, fg=IDLE_FG, pady=12)
        self.result_tile.pack(fill="x")
        grid = tk.Frame(rb, bg=CARD)
        grid.pack(fill="x", pady=(12, 0))
        self.result_fields = {}
        for i, (key, label) in enumerate([("ref", "Test reference"),
                                          ("device", "Device"),
                                          ("leak", "Leak rate"),
                                          ("drop", "Pressure drop")]):
            tk.Label(grid, text=label, bg=CARD, fg=MUTED, font=_font(9)).grid(
                row=i, column=0, sticky="w", pady=2)
            v = tk.Label(grid, text="—", bg=CARD, fg=TEXT, font=_font(10, "bold"))
            v.grid(row=i, column=1, sticky="e", pady=2)
            self.result_fields[key] = v
        grid.columnconfigure(1, weight=1)

        # ── right: live ──────────────────────────────────────────
        live = Card(body, "Live")
        live.grid(row=0, column=1, sticky="nsew", pady=(0, 12))
        lb = live.body
        top = tk.Frame(lb, bg=CARD)
        top.pack(fill="x")
        self.stage_lbl = tk.Label(top, text="Idle", bg=CARD, fg=NAVY, font=_font(12, "bold"))
        self.stage_lbl.pack(side="left")
        self.elapsed_lbl = tk.Label(top, text="", bg=CARD, fg=MUTED, font=_font(10))
        self.elapsed_lbl.pack(side="right")
        pr = tk.Frame(lb, bg=CARD)
        pr.pack(anchor="w", pady=(6, 8))
        self.pressure_lbl = tk.Label(pr, text="—", bg=CARD, fg=TEXT,
                                     font=_font(40, "bold"))
        self.pressure_lbl.pack(side="left")
        tk.Label(pr, text="mbar", bg=CARD, fg=MUTED, font=_font(14)).pack(
            side="left", anchor="s", padx=(6, 0), pady=(0, 10))
        self.progress = ttk.Progressbar(lb, style="Test.Horizontal.TProgressbar",
                                        maximum=1.0)
        self.progress.pack(fill="x")
        self.limits_lbl = tk.Label(lb, bg=CARD, fg=MUTED, font=_font(9))
        self.limits_lbl.pack(anchor="w", pady=(8, 0))

        # ── right: history + log ─────────────────────────────────
        hist = Card(body, "Recent results")
        hist.grid(row=1, column=1, sticky="nsew")
        hb = hist.body
        cols = ("ref", "device", "time", "operator", "leak", "result")
        self.tree = ttk.Treeview(hb, columns=cols, show="headings", height=6)
        for c, label, w, anchor in [
            ("ref", "Test ref", 100, "w"), ("device", "Device", 120, "w"),
            ("time", "Date / time", 150, "w"), ("operator", "Operator", 90, "w"),
            ("leak", "Leak (mbar/s)", 100, "e"), ("result", "Result", 70, "center")]:
            self.tree.heading(c, text=label, anchor=anchor)
            self.tree.column(c, width=w, anchor=anchor, stretch=c in ("device", "time"))
        self.tree.tag_configure("PASS", foreground=PASS_FG)
        self.tree.tag_configure("FAIL", foreground=FAIL_FG, background="#FFF5F5")
        sb = ttk.Scrollbar(hb, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="top", fill="both", expand=True)
        sb.place(in_=self.tree, relx=1.0, rely=0, relheight=1.0, anchor="ne")

        self.log_text = tk.Text(hb, height=5, bg="#F9FAFB", fg="#374151",
                                relief="flat", font=_font(9, family="Consolas" if
                                sys.platform == "win32" else "Courier"),
                                highlightthickness=1, highlightbackground=BORDER,
                                highlightcolor=BORDER)
        self.log_text.pack(fill="x", pady=(10, 0))
        self.log_text.configure(state="disabled")

    # ══════════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════════
    @property
    def simulating(self) -> bool:
        return self.cfg.simulate or self.force_simulate

    def _refresh_mode_badge(self):
        if self.simulating:
            self.mode_badge.configure(text="SIMULATION", bg="#B45309")
        else:
            self.mode_badge.configure(text=f"{self.cfg.serial_port}  ·  ID {self.cfg.fbus_id}",
                                      bg="#2E6497")
        self.limits_lbl.configure(
            text=f"Test time {self.cfg.test_duration_s:g} s   ·   "
                 f"Limit {self.cfg.pass_threshold_mbar_s:g} mbar/s")
        self.file_lbl.configure(text=f"Results: {os.path.abspath(self.cfg.excel_file)}")

    def _log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_running(self, running: bool):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.abort_btn.configure(state="normal" if running else "disabled")
        self.device_entry.configure(state="disabled" if running else "normal")
        self.operator_entry.configure(state="disabled" if running else "normal")

    def _load_history(self):
        self.tree.delete(*self.tree.get_children())
        try:
            rows = read_recent(self.cfg.excel_file)
        except Exception as exc:  # corrupt / locked file shouldn't stop the app
            self._log(f"Could not read results file: {exc}")
            return
        for r in reversed(rows):
            self._add_history_row(r[1], r[2], r[3], r[4], r[8], r[10])

    def _add_history_row(self, ref, device, when, operator, leak, result):
        leak_txt = f"{leak:.5f}" if isinstance(leak, (int, float)) else str(leak or "")
        self.tree.insert("", 0, values=(ref, device, when, operator, leak_txt, result),
                         tags=(str(result),))

    def _make_instrument(self):
        log = lambda m: self.events.put(("log", m))
        if self.simulating:
            return SimulatedInstrument(self.cfg.target_pressure_mbar, self.cfg.test_duration_s,
                                       self.cfg.pass_threshold_mbar_s, self.cfg.sim_speed, log)
        return FBusInstrument(self.cfg.serial_port, self.cfg.baud_rate, self.cfg.fbus_id, log)

    # ══════════════════════════════════════════════════════════════
    #  Test control
    # ══════════════════════════════════════════════════════════════
    def start_test(self):
        if self.worker and self.worker.is_alive():
            return
        device = self.device_var.get().strip()
        operator = self.operator_var.get().strip()
        if not operator:
            messagebox.showwarning("Operator required", "Enter the operator name or ID.")
            self.operator_entry.focus_set()
            return
        if not device:
            messagebox.showwarning("Device number required", "Enter or scan a device number.")
            self.device_entry.focus_set()
            return
        errors = [e for e in self.cfg.validate()
                  if not (self.simulating and e.startswith("Serial port"))]
        if errors:
            messagebox.showerror("Settings", "\n".join(errors))
            return

        if operator != self.cfg.last_operator:
            self.cfg.last_operator = operator
            self._save_config_quietly()

        self._reset_result_panel()
        self.progress["value"] = 0
        self.cancel.clear()
        self._set_running(True)
        self._log(f"── {device} ──")
        self.worker = threading.Thread(target=self._worker, args=(device, operator), daemon=True)
        self.worker.start()

    def abort_test(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self._log("Aborting…")

    def _worker(self, device: str, operator: str):
        inst = self._make_instrument()
        try:
            inst.open()
            result = run_leak_test(
                inst, device, operator, self.cfg,
                on_progress=lambda p: self.events.put(("progress", p)),
                log=lambda m: self.events.put(("log", m)),
                cancel=self.cancel)
            self.events.put(("result", result))
        except TestAborted as exc:
            self.events.put(("aborted", str(exc)))
        except Exception as exc:
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            try:
                inst.close()
            except Exception:
                pass

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    self._show_progress(payload)
                elif kind == "result":
                    self._finish(payload)
                elif kind == "aborted":
                    self._log(payload)
                    self.stage_lbl.configure(text="Aborted")
                    self._set_running(False)
                elif kind == "error":
                    self._log(f"ERROR {payload}")
                    self.stage_lbl.configure(text="Error")
                    self._set_running(False)
                    messagebox.showerror("Test failed to run", payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _show_progress(self, p: Progress):
        self.stage_lbl.configure(text=p.stage)
        self.pressure_lbl.configure(text="—" if p.pressure is None else f"{p.pressure:.3f}")
        if p.test_fraction is not None:
            self.progress["value"] = p.test_fraction
            self.elapsed_lbl.configure(
                text=f"{p.test_fraction * self.cfg.test_duration_s:4.0f} / "
                     f"{self.cfg.test_duration_s:g} s")
        else:
            self.elapsed_lbl.configure(text=f"{p.elapsed_s:.0f} s")

    def _finish(self, r: TestResult):
        self.stage_lbl.configure(text="Complete")
        self.progress["value"] = 1.0
        ok = r.passed
        self.result_tile.configure(text=r.result, bg=PASS_BG if ok else FAIL_BG,
                                   fg=PASS_FG if ok else FAIL_FG)
        self.result_fields["ref"].configure(text=r.test_reference)
        self.result_fields["device"].configure(text=r.device_number)
        self.result_fields["leak"].configure(
            text=f"{r.leak_rate:.5f} mbar/s", fg=PASS_FG if ok else FAIL_FG)
        self.result_fields["drop"].configure(text=f"{r.pressure_drop:.3f} mbar")
        self._log(f"{r.result}  leak {r.leak_rate:.5f} mbar/s  ref {r.test_reference}")

        self._save_result(r)
        self._add_history_row(r.test_reference, r.device_number,
                              r.timestamp.strftime("%Y-%m-%d  %H:%M:%S"),
                              r.operator, r.leak_rate, r.result)
        self._set_running(False)
        self.device_var.set("")
        self.device_entry.focus_set()

    def _save_result(self, r: TestResult):
        while True:
            try:
                entry = log_result(self.cfg.excel_file, r)
                self._log(f"Saved as entry #{entry}")
                return
            except PermissionError:
                retry = messagebox.askretrycancel(
                    "Results file is open",
                    "Couldn't save — the results file is probably open in Excel.\n\n"
                    "Close it and press Retry.")
                if not retry:
                    self._log(f"NOT SAVED: {r.device_number} {r.result} {r.leak_rate}")
                    return
            except Exception as exc:
                messagebox.showerror("Save failed", f"{type(exc).__name__}: {exc}")
                self._log(f"NOT SAVED: {r.device_number} {r.result} {r.leak_rate}")
                return

    def _reset_result_panel(self):
        self.result_tile.configure(text="Testing…", bg=IDLE_BG, fg=IDLE_FG)
        for k, v in self.result_fields.items():
            v.configure(text="—", fg=TEXT)

    # ══════════════════════════════════════════════════════════════
    #  Misc actions
    # ══════════════════════════════════════════════════════════════
    def open_results(self):
        path = os.path.abspath(self.cfg.excel_file)
        if not os.path.exists(path):
            messagebox.showinfo("No results yet", "The results file is created after the first test.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror("Could not open file", str(exc))

    def open_settings(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Test running", "Wait for the current test to finish.")
            return
        SettingsDialog(self, self.cfg, on_save=self._settings_saved)

    def _settings_saved(self):
        self._save_config_quietly()
        self._refresh_mode_badge()
        self._load_history()
        self._log("Settings saved")

    def _save_config_quietly(self):
        try:
            self.cfg.save()
        except OSError as exc:
            self._log(f"Could not save config.json: {exc}")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Test running", "A test is running. Abort and quit?"):
                return
            self.cancel.set()
        self.destroy()


class SettingsDialog(tk.Toplevel):
    FIELDS = [
        ("Connection", None, None),
        ("serial_port", "Serial port", "port"),
        ("baud_rate", "Baud rate", "int"),
        ("fbus_id", "F-BUS ID", "str"),
        ("Test", None, None),
        ("test_duration_s", "Test time (s)", "float"),
        ("pass_threshold_mbar_s", "Pass limit (mbar/s)", "float"),
        ("target_pressure_mbar", "Test pressure (mbar)", "float"),
        ("Advanced", None, None),
        ("poll_interval_s", "Poll interval (s)", "float"),
        ("max_wait_s", "Max wait (s)", "float"),
        ("excel_file", "Results file", "file"),
        ("simulate", "Simulation mode", "bool"),
    ]

    def __init__(self, master, cfg: Config, on_save):
        super().__init__(master)
        self.cfg, self.on_save = cfg, on_save
        self.title("Settings")
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.transient(master)
        self.vars = {}

        frm = tk.Frame(self, bg=CARD)
        frm.pack(padx=20, pady=16)
        row = 0
        for key, label, kind in self.FIELDS:
            if label is None:
                tk.Label(frm, text=key.upper(), bg=CARD, fg=MUTED, font=_font(8, "bold")).grid(
                    row=row, column=0, columnspan=3, sticky="w", pady=(10 if row else 0, 4))
                row += 1
                continue
            tk.Label(frm, text=label, bg=CARD, fg=TEXT, font=_font(10)).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=3)
            value = getattr(cfg, key)
            if kind == "bool":
                var = tk.BooleanVar(value=value)
                ttk.Checkbutton(frm, variable=var).grid(row=row, column=1, sticky="w")
            else:
                var = tk.StringVar(value=str(value))
                if kind == "port":
                    w = ttk.Combobox(frm, textvariable=var, values=self._ports(), width=28)
                else:
                    w = ttk.Entry(frm, textvariable=var, width=30)
                w.grid(row=row, column=1, sticky="we", pady=3)
                if kind == "file":
                    ttk.Button(frm, text="…", width=3, command=lambda v=var: self._browse(v)).grid(
                        row=row, column=2, padx=(4, 0))
                if kind == "port":
                    ttk.Button(frm, text="↻", width=3,
                               command=lambda w=w: w.configure(values=self._ports())).grid(
                        row=row, column=2, padx=(4, 0))
            self.vars[key] = (var, kind)
            row += 1

        tk.Label(frm, text="Test time must match the programme set on the instrument.",
                 bg=CARD, fg=MUTED, font=_font(8)).grid(row=row, column=0, columnspan=3,
                                                         sticky="w", pady=(10, 0))
        btns = tk.Frame(self, bg=CARD)
        btns.pack(fill="x", padx=20, pady=(0, 16))
        ttk.Button(btns, text="Save", style="Primary.TButton", command=self._save).pack(side="right")
        ttk.Button(btns, text="Cancel", style="Secondary.TButton",
                   command=self.destroy).pack(side="right", padx=(0, 8))
        self.grab_set()

    @staticmethod
    def _ports():
        try:
            from serial.tools import list_ports
            return [p.device for p in list_ports.comports()]
        except Exception:
            return []

    def _browse(self, var):
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".xlsx", filetypes=[("Excel workbook", "*.xlsx")],
            initialfile=os.path.basename(var.get()))
        if path:
            var.set(path)

    def _save(self):
        backup = {k: getattr(self.cfg, k) for k in self.vars}
        try:
            for key, (var, kind) in self.vars.items():
                v = var.get()
                if kind == "int":
                    v = int(str(v).strip())
                elif kind == "float":
                    v = float(str(v).strip())
                elif kind != "bool":
                    v = str(v).strip()
                setattr(self.cfg, key, v)
            self.cfg._coerce()
        except ValueError as exc:
            for k, v in backup.items():
                setattr(self.cfg, k, v)
            messagebox.showerror("Invalid value", str(exc), parent=self)
            return
        errors = self.cfg.validate()
        if errors:
            for k, v in backup.items():
                setattr(self.cfg, k, v)
            messagebox.showerror("Invalid settings", "\n".join(errors), parent=self)
            return
        self.on_save()
        self.destroy()
