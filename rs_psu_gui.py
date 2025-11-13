#!/usr/bin/env python3
# rs_psu_gui.py
# GUI only: Tkinter + Matplotlib. Talks to worker via queues.

import os
import csv
import math
import time 
import queue
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from collections import deque
from datetime import datetime

import multiprocessing as mp
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# Worker protocol
from rs_psu_worker import (
    Watchdog,
    CMD_CONNECT, CMD_DISCONNECT, CMD_QUIT, CMD_SET_INTERVAL, CMD_SET_VI, CMD_TOGGLE_CH, CMD_MASTER, CMD_SET_LIMITS,
    MSG_STATUS, MSG_CONNECTED, MSG_DISCONNECTED, MSG_MEAS, MSG_EVENT,
    MAX_VOLT, MIN_VOLT, MAX_CURR, MIN_CURR
)

APP_TITLE = "R&S NGE103B GUI (Worker-owned VISA)"
DEFAULT_RESOURCE = "USB0::0x0AAD::0x0197::5601.3800k03-112953::INSTR"
PLOT_HISTORY_SEC = 300
SOFT_LIM_SCALE = 0.90
HARD_LIM_SCALE = 0.99

def now_iso():
    return datetime.utcnow().isoformat()

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        try:
            self.state("zoomed")
        except Exception:
            try: self.attributes("-zoomed", True)
            except Exception: pass
        self.geometry("1200x780")
        self.resizable(True, True)

        # Queues + worker
        self.cmd_q  = mp.Queue()
        self.meas_q = mp.Queue()
        self.worker = Watchdog(self.cmd_q, self.meas_q, init_interval=0.5)
        self.worker.start()

        # State
        self.connected = False
        self.plot_active = False
        self._starting_plot = False
        self.poll_interval_var = tk.StringVar(value="0.5")   # worker sampling
        self.plot_interval_var = tk.StringVar(value="0.5")   # repaint cadence
        self.series_vars = {}
        self.lines = {}
        self.buffers = {}
        self.start_time = None
        self.csv_file = None
        self.csv_writer = None
        self.csv_header = []
        self.log_path = None

        self._build_ui()
        # background drains (non-blocking)
        self.after(150, self._drain_messages_status)
        self.after(200, self._drain_messages_plot_buffer)

    # ---------- UI ----------
    def _build_ui(self):
        pad = 8

        conn = ttk.LabelFrame(self, text="Connection")
        conn.pack(fill="x", padx=pad, pady=(pad,0))
        ttk.Label(conn, text="VISA Resource:").grid(row=0, column=0, padx=pad, pady=pad, sticky="e")
        self.resource_var = tk.StringVar(value=DEFAULT_RESOURCE)
        ttk.Entry(conn, textvariable=self.resource_var, width=70).grid(row=0, column=1, padx=pad, pady=pad, sticky="w")
        ttk.Button(conn, text="Connect", command=self.connect).grid(row=0, column=2, padx=pad, pady=pad)
        self.btn_disconnect = ttk.Button(conn, text="Disconnect", command=self.disconnect, state="disabled")
        self.btn_disconnect.grid(row=0, column=3, padx=pad, pady=pad)
        self.idn_label = ttk.Label(conn, text="Not connected")
        self.idn_label.grid(row=1, column=0, columnspan=4, padx=pad, pady=(0,pad), sticky="w")

        # --- Create a horizontal container for Channels + Plot ---
        main_hframe = ttk.Frame(self)
        main_hframe.pack(fill="both", expand=True, padx=pad, pady=pad)

        # Channels Frame (left half)
        chs = ttk.LabelFrame(main_hframe, text="Channels (CH1..CH3)")
        chs.pack(side="left", fill="both", expand=True, padx=(0, pad//2), pady=pad)
        headers = ["Channel","Voltage (V)","Current (A)","Apply V/I","Output","Soft P (W)","Hard P (W)","Set Limits"]
        for i,h in enumerate(headers):
            ttk.Label(chs, text=h, font=("",9,"bold")).grid(row=0, column=i, padx=4, pady=4)
        self.ch_vars = {}
        for ch in (1,2,3):
            r = ch
            ttk.Label(chs, text=f"CH{ch}").grid(row=r, column=0, padx=4, pady=4)
            v = tk.StringVar(value="0.0"); i = tk.StringVar(value="0.0")
            ttk.Entry(chs, textvariable=v, width=10).grid(row=r, column=1, padx=4, pady=4)
            ttk.Entry(chs, textvariable=i, width=10).grid(row=r, column=2, padx=4, pady=4)
            ttk.Button(chs, text="Apply", command=lambda c=ch: self.apply_vi(c)).grid(row=r, column=3, padx=4, pady=4)
            ttk.Button(chs, text="Toggle", command=lambda c=ch: self.toggle_ch(c)).grid(row=r, column=4, padx=4, pady=4)
            soft = tk.StringVar(value="inf"); hard = tk.StringVar(value="inf")
            ttk.Entry(chs, textvariable=soft, width=10).grid(row=r, column=5, padx=4, pady=4)
            ttk.Entry(chs, textvariable=hard, width=10).grid(row=r, column=6, padx=4, pady=4)
            ttk.Button(chs, text="Set", command=lambda c=ch: self.push_limits(c)).grid(row=r, column=7, padx=4, pady=4)
            self.ch_vars[ch] = {"v": v, "i": i, "soft": soft, "hard": hard}

        gen = ttk.LabelFrame(self, text="General / Polling / Logging")
        gen.pack(fill="x", padx=pad, pady=pad)
        self.btn_master_on  = ttk.Button(gen, text="Master ON",  command=lambda: self.master_out(True),  state="disabled")
        self.btn_master_off = ttk.Button(gen, text="Master OFF", command=lambda: self.master_out(False), state="disabled")
        self.btn_master_on.grid(row=0, column=0, padx=pad, pady=pad); self.btn_master_off.grid(row=0, column=1, padx=pad, pady=pad)
        ttk.Label(gen, text="Polling interval (s):").grid(row=0, column=2, padx=pad, pady=pad, sticky="e")
        ttk.Entry(gen, textvariable=self.poll_interval_var, width=8).grid(row=0, column=3, padx=4, pady=pad, sticky="w")
        ttk.Button(gen, text="Apply", command=self.push_interval).grid(row=0, column=4, padx=pad, pady=pad)

        pc = ttk.LabelFrame(main_hframe, text="Live Plot & Logging")
        pc.pack(side="left", fill="both", expand=True, padx=(pad//2, 0), pady=pad)
        r = 0
        ttk.Label(pc, text="Select series to plot/log:").grid(row=r, column=0, padx=pad, pady=pad, sticky="w"); r += 1
        for c, ch in enumerate((1,2,3), start=1):
            ttk.Label(pc, text=f"CH{ch}", font=("",9,"bold")).grid(row=r, column=c, padx=4, pady=4)
        r += 1
        for m in ("V","I","P"):
            ttk.Label(pc, text=m).grid(row=r, column=0, padx=4, pady=4, sticky="e")
            for c, ch in enumerate((1,2,3), start=1):
                key = f"CH{ch}_{m}"
                var = tk.BooleanVar(value=(m=="V" and ch==1))
                self.series_vars[key] = var
                ttk.Checkbutton(pc, variable=var).grid(row=r, column=c, padx=4, pady=4)
            r += 1

        ttk.Label(pc, text="Plot repaint interval (s):").grid(row=r, column=0, padx=pad, pady=pad, sticky="e")
        ttk.Entry(pc, textvariable=self.plot_interval_var, width=8).grid(row=r, column=1, padx=4, pady=pad, sticky="w")
        self.btn_plot = ttk.Button(pc, text="Start Plot & Log", command=self.toggle_plot, state="disabled")
        self.btn_plot.grid(row=r, column=2, padx=pad, pady=pad, sticky="w")

        self.figure = Figure(figsize=(10,3.6), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.ax.set_title("Live Measurements"); self.ax.set_xlabel("Time (s)"); self.ax.set_ylabel("Value"); self.ax.grid(True)
        self.ax.margins(x=0.02, y=0.05)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill="both", expand=True, padx=pad, pady=(0, pad))

        self.status = ttk.Label(self, text="Ready.", relief="sunken", anchor="w")
        self.status.pack(fill="x", side="bottom")

    # ---------- background drains ----------
    def _drain_messages_status(self):
        # Handle connection/state messages; push measurements back for plot loop
        stash = []
        while True:
            try:
                msg = self.meas_q.get_nowait()
            except queue.Empty:
                break
            tp = msg.get("type")
            if tp == MSG_STATUS:
                text = msg.get("msg","")
                self.status.config(text=text)

                if text.startswith("Connected:"):
                    self.connected = True
                    self.idn_label.config(text=text)  # shows 'Connected: <IDN>'
                    self.btn_disconnect.config(state="normal")
                    self.btn_master_on.config(state="normal")
                    self.btn_master_off.config(state="normal")
                    self.btn_plot.config(state="normal")

                if not msg.get("ok", True) and "Connect failed" in msg.get("msg",""):
                    messagebox.showerror("Connection", msg.get("msg",""))

            elif tp == MSG_CONNECTED:
                self.connected = True
                self.idn_label.config(text=f"Connected: {msg.get('idn','')}")
                self.btn_disconnect.config(state="normal")
                self.btn_master_on.config(state="normal")
                self.btn_master_off.config(state="normal")
                self.btn_plot.config(state="normal")
                self.status.config(text="Connected.")
            elif tp == MSG_DISCONNECTED:
                self.connected = False
                self.idn_label.config(text="Not connected")
                self.btn_disconnect.config(state="disabled")
                self.btn_master_on.config(state="disabled")
                self.btn_master_off.config(state="disabled")
                self.btn_plot.config(state="disabled")
                self.status.config(text="Disconnected.")
            else:
                stash.append(msg)  # meas/event kept for plot drain

        # return non-status messages to queue (so plot loop can consume)
        for m in stash:
            try: self.meas_q.put_nowait(m)
            except Exception: pass

        self.after(200, self._drain_messages_status)

    def _drain_messages_plot_buffer(self):
        # This doesn’t draw; it just ensures the queue doesn’t pile up when not plotting
        if not getattr(self, "plot_active", False) and not getattr(self, "_starting_plot", False):
            # Drop old measurement bursts (we only need the last few for a quick redraw later)
            trimmed = 0
            while True:
                try:
                    msg = self.meas_q.get_nowait()
                except queue.Empty:
                    break
                if msg.get("type") in (MSG_MEAS, MSG_EVENT):
                    trimmed += 1
                # statuses will be handled by status drain; ignore here
            if trimmed:
                self.status.config(text=f"Buffered {trimmed} messages (idle).")
        self.after(500, self._drain_messages_plot_buffer)

    # ---------- Actions (send commands only) ----------
    def connect(self):
        res = self.resource_var.get().strip()
        if not res:
            messagebox.showerror("VISA", "Enter a VISA resource"); return
        self.cmd_q.put({"type": CMD_CONNECT, "resource": res})
        self.status.config(text="Connecting…")

    def disconnect(self):
        self.cmd_q.put({"type": CMD_DISCONNECT})
        self.status.config(text="Disconnecting…")

    def master_out(self, on: bool):
        if not self.connected: messagebox.showwarning("Not connected","Connect first."); return
        self.cmd_q.put({"type": CMD_MASTER, "on": bool(on)})

    def push_interval(self):
        try:
            val = float(self.poll_interval_var.get())
            if val <= 0: raise ValueError
            self.cmd_q.put({"type": CMD_SET_INTERVAL, "interval": val})
            self.status.config(text=f"Polling interval set to {val}s")
        except Exception:
            messagebox.showerror("Interval","Enter a positive number")

    def apply_vi(self, ch: int):
        if not self.connected: messagebox.showwarning("Not connected","Connect first."); return
        try:
            v = float(self.ch_vars[ch]["v"].get()); i = float(self.ch_vars[ch]["i"].get())
        except Exception:
            messagebox.showerror("Input","V/I must be numeric"); return
        if not (MIN_VOLT<=v<=MAX_VOLT): messagebox.showerror("Voltage",f"Range [{MIN_VOLT},{MAX_VOLT}]"); return
        if not (MIN_CURR<=i<=MAX_CURR): messagebox.showerror("Current",f"Range [{MIN_CURR},{MAX_CURR}]"); return
        self.cmd_q.put({"type": CMD_SET_VI, "ch": ch, "v": v, "i": i})
        # suggest limits and push
        p = v*i
        self.ch_vars[ch]["soft"].set(f"{SOFT_LIM_SCALE*p:.3f}")
        self.ch_vars[ch]["hard"].set(f"{HARD_LIM_SCALE*p:.3f}")
        self.push_limits(ch)

    def toggle_ch(self, ch: int):
        if not self.connected: messagebox.showwarning("Not connected","Connect first."); return
        self.cmd_q.put({"type": CMD_TOGGLE_CH, "ch": ch})

    def push_limits(self, ch: int):
        soft = self.ch_vars[ch]["soft"].get()
        hard = self.ch_vars[ch]["hard"].get()
        # Optional sanity (if both finite): 0 ≤ soft ≤ hard ≤ min(V*I, ABSOLUTE_MAX_PWR)
        try:
            s = float(soft); h = float(hard)
            v = float(self.ch_vars[ch]["v"].get()); i = float(self.ch_vars[ch]["i"].get())
            pmax = v*i if v*i > 0 else math.inf
            if not (0.0 <= s <= h <= pmax):
                messagebox.showerror("Limits", f"CH{ch}: 0 ≤ soft ≤ hard ≤ {pmax:.3f} W"); return
        except Exception:
            pass  # allow "inf" or blanks
        self.cmd_q.put({"type": CMD_SET_LIMITS, "ch": ch, "soft": soft, "hard": hard})
        self.status.config(text=f"CH{ch} limits set.")

    # ---------- Plot & logging ----------
    def toggle_plot(self):
        if not getattr(self, "plot_active", False):
            self.start_plot()
        else: 
            self.stop_plot()

    def _selected(self): return [k for k,v in self.series_vars.items() if v.get()]

    def start_plot(self):
        if not self.connected: messagebox.showwarning("Not connected","Connect first."); return
        try:
            repaint = float(self.plot_interval_var.get()); assert repaint > 0
        except Exception:
            messagebox.showerror("Plot interval","Must be a positive number"); return

        sel = self._selected()
        if not sel:
            messagebox.showwarning("No series","Select at least one series."); return

        default_name = datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        path = filedialog.asksaveasfilename(title="Select log file", defaultextension=".csv",
                                            initialfile=default_name, initialdir=os.getcwd(),
                                            filetypes=[("CSV files","*.csv"),("All files","*.*")])
        if not path: return

        self.start_time = time.time()
        self.ax.cla()
        self.ax.set_title("Live Measurements"); self.ax.set_xlabel("Time (s)"); self.ax.set_ylabel("Value"); self.ax.grid(True)
        self.ax.margins(x=0.02, y=0.05)
        self.lines = {}
        self.buffers = {k: deque(maxlen=100000) for k in sel}
        for key in sel:
            line, = self.ax.plot([], [], label=key)
            try: line.sticky_edges.x[:]=[]; line.sticky_edges.y[:]=[]
            except Exception: pass
            self.lines[key] = line
        self.ax.legend(loc="upper left"); self.canvas.draw()

        self.log_path = path
        self.csv_file = open(self.log_path, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_header = ["timestamp_iso","t_rel_s"] + sel + ["event","event_ch","event_v","event_i","event_p"]
        self.csv_writer.writerow(self.csv_header); self.csv_file.flush()

        self.plot_active = True
        self.btn_plot.config(text="Stop Plot & Log")
        self.status.config(text=f"Plotting & logging to: {self.log_path}")
        self.after(int(repaint*1000), self._plot_tick)

    def stop_plot(self):
        self.plot_active = False
        self.btn_plot.config(text="Start Plot & Log")
        try:
            if self.csv_file: self.csv_file.flush(); self.csv_file.close()
        except Exception: pass
        self.csv_file = None; self.csv_writer = None
        self.status.config(text="Plotting stopped.")

    def _plot_tick(self):
        if not self.plot_active: return
        try:
            repaint = float(self.plot_interval_var.get()); repaint = max(0.1, repaint)
        except Exception:
            repaint = 0.5

        sel = self._selected()
        meas_msgs = []
        event_msgs = []

        # drain everything available now
        while True:
            try:
                msg = self.meas_q.get_nowait()
            except queue.Empty:
                break
            tp = msg.get("type")
            if tp == MSG_MEAS:   meas_msgs.append(msg)
            elif tp == MSG_EVENT: event_msgs.append(msg)
            elif tp == MSG_STATUS:
                text = msg.get("msg","")
                self.status.config(text=text)

                if text.startswith("Connected:"):
                    self.connected = True
                    self.idn_label.config(text=text)
                    self.btn_disconnect.config(state="normal")
                    self.btn_master_on.config(state="normal")
                    self.btn_master_off.config(state="normal")
                    self.btn_plot.config(state="normal")

            elif tp == MSG_CONNECTED:
                self.connected = True
                self.idn_label.config(text=f"Connected: {msg.get('idn','')}")
                self.btn_disconnect.config(state="normal")
                self.btn_master_on.config(state="normal")
                self.btn_master_off.config(state="normal")
                self.btn_plot.config(state="normal")
                self.status.config(text="Connected.")
            elif tp == MSG_DISCONNECTED:
                self.connected = False
                self.idn_label.config(text="Not connected")
                self.btn_disconnect.config(state="disabled")
                self.btn_master_on.config(state="disabled")
                self.btn_master_off.config(state="disabled")
                self.btn_plot.config(state="disabled")
                self.status.config(text="Disconnected.")

        # log events
        if self.csv_writer and event_msgs:
            for ev in event_msgs:
                row = [ev["iso"], f"{ev['t']:.3f}"] + [""]*len(sel) + [ev["event"], f"CH{ev['ch']}", ev["V"], ev["I"], ev["P"]]
                self.csv_writer.writerow(row)
            self.csv_file.flush()

        # update plot per meas
        for meas in meas_msgs:
            iso = meas["iso"]; t_rel = meas["t"]; data = meas["data"]
            # update buffers for selected keys
            for key in sel:
                ch = int(key[2]); metric = key[-1]
                chd = data.get(f"CH{ch}")
                if chd and metric in chd:
                    if key not in self.buffers:
                        self.buffers[key] = deque(maxlen=100000)
                        line, = self.ax.plot([], [], label=key)
                        self.lines[key] = line; self.ax.legend(loc="upper left")
                    self.buffers[key].append((t_rel, chd[metric]))

            # apply new data to lines
            for key, line in self.lines.items():
                buf = self.buffers.get(key)
                if buf:
                    xs = [p[0] for p in buf]; ys = [p[1] for p in buf]
                    line.set_data(xs, ys)

            xmin = max(0.0, t_rel - PLOT_HISTORY_SEC)
            xmax = max(10.0, t_rel if t_rel > 10 else 10.0)
            self.ax.set_xlim(xmin, xmax)

            # robust y padding
            all_ys = []
            for line in self.lines.values():
                xs = line.get_xdata(); ys = line.get_ydata()
                all_ys += [y for x,y in zip(xs,ys) if x>=xmin]
            if all_ys:
                ymin, ymax = min(all_ys), max(all_ys); yr = ymax - ymin
                if yr < 1e-9:
                    ymid = 0.5*(ymin+ymax); pad = max(0.5, 0.05*max(abs(ymid),1.0))
                    self.ax.set_ylim(ymid - pad, ymid + pad)
                else:
                    pad = max(0.1*yr, 0.05*max(abs(ymax),abs(ymin),1.0))
                    self.ax.set_ylim(ymin - pad, ymax + pad)

            self.canvas.draw_idle()

            # write GUI sample row
            if self.csv_writer:
                row = [iso, f"{t_rel:.3f}"]
                for key in sel:
                    ch = int(key[2]); metric = key[-1]
                    chd = data.get(f"CH{ch}")
                    row.append(chd[metric] if chd and metric in chd else "")
                row += ["","","",""]
                self.csv_writer.writerow(row)
                self.csv_file.flush()

        if self.plot_active:
            self.after(int(repaint*1000), self._plot_tick)

    # ---------- teardown ----------
    def destroy(self):
        try: self.stop_plot()
        except Exception: pass
        try: self.cmd_q.put({"type": CMD_QUIT})
        except Exception: pass
        try:
            if self.worker.is_alive():
                self.worker.join(timeout=2.0)
        except Exception:
            pass
        super().destroy()

def main():
    mp.set_start_method("spawn", force=True)
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
