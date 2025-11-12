"""
NGE103B 3-Channel GUI Controller with Live Plot (Tkinter + Matplotlib)

Features:
- Per-channel Voltage/Current set and output control.
- Master output ON/OFF.
- Per-channel measurement readback (V/I/P).
- Live line plot with checkboxes to select series (V, A, P) per channel.
- Adjustable sampling rate (seconds) for the plot & logging.
- Start/Stop plot button.
- On Start, ask for log file name (default = current date/time). Logs only the selected series to CSV.

Requirements:
- Python 3.8+
- RsInstrument >= 1.53.0  (pip install RsInstrument)
- R&S VISA installed and configured.
- matplotlib (pip install matplotlib)

Notes:
- Default VISA resource string is prefilled; change it to match your device.
- Soft limits assume typical NGE103B specs (0..32 V, 0..3 A). Adjust if your model differs.
"""

import csv
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from collections import deque
from datetime import datetime

# Matplotlib for embedded plotting
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

try:
    from RsInstrument import RsInstrument
    RSINSTR_AVAILABLE = True
except Exception as ex:
    RsInstrument = None  # type: ignore
    RSINSTR_AVAILABLE = False

APP_TITLE = "R&S NGE103B GUI Controller + Plot"
DEFAULT_RESOURCE = "USB0::0x0AAD::0x0197::5601.3800k03-112953::INSTR"  # Change if needed
# Soft limits (edit to match your model/specs if needed)
MAX_VOLT = 32.0
MAX_CURR = 3.0
MIN_VOLT = 0.0
MIN_CURR = 0.0

PLOT_HISTORY_SEC = 300  # show last 5 minutes by default (based on sampling rate and x-limits update)

SERIES_KEYS = [
    ("CH1_V", 1, "V"),
    ("CH1_I", 1, "I"),
    ("CH1_P", 1, "P"),
    ("CH2_V", 2, "V"),
    ("CH2_I", 2, "I"),
    ("CH2_P", 2, "P"),
    ("CH3_V", 3, "V"),
    ("CH3_I", 3, "I"),
    ("CH3_P", 3, "P"),
]

class NGEGui(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            self.state('zoomed')  # Windows
        except:
            self.attributes('-zoomed', True)  # macOS/Linux
        self.title(APP_TITLE)
        self.geometry("1200x720")
        self.resizable(True, True)

        # State
        self.inst = None
        self.connected = False

        # Polling (simple readbacks) state
        self.polling = tk.BooleanVar(value=False)
        self.stop_poll_event = threading.Event()

        # Plot state
        self.plot_active = False
        self.sample_interval_var = tk.StringVar(value="1.0")  # seconds
        self.series_vars = {}  # key -> tk.BooleanVar
        self.lines = {}        # key -> matplotlib line
        self.buffers = {}      # key -> deque of (t, y)
        self.start_time = None
        self.log_file = None
        self.csv_writer = None
        self.csv_header = []

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # --------------------------- UI ---------------------------
    def _build_ui(self):
        pad = 10

        # Connection Frame
        conn = ttk.LabelFrame(self, text="Connection")
        conn.pack(fill="x", padx=pad, pady=(pad, 0))

        ttk.Label(conn, text="VISA Resource:").grid(row=0, column=0, padx=pad, pady=pad, sticky="e")
        self.resource_var = tk.StringVar(value=DEFAULT_RESOURCE)
        ttk.Entry(conn, textvariable=self.resource_var, width=70).grid(row=0, column=1, padx=pad, pady=pad, sticky="w")

        self.btn_connect = ttk.Button(conn, text="Connect", command=self.connect)
        self.btn_connect.grid(row=0, column=2, padx=pad, pady=pad)
        self.btn_disconnect = ttk.Button(conn, text="Disconnect", command=self.disconnect, state="disabled")
        self.btn_disconnect.grid(row=0, column=3, padx=pad, pady=pad)

        self.idn_label = ttk.Label(conn, text="Not connected")
        self.idn_label.grid(row=1, column=0, columnspan=4, padx=pad, pady=(0, pad), sticky="w")

        # Channels Frame
        chs = ttk.LabelFrame(self, text="Channels (CH1..CH3)")
        chs.pack(fill="x", padx=pad, pady=pad)

        headers = ["Channel", "Voltage (V)", "Current (A)", "Apply V/I", "Output ON/OFF", "Measured V (V)", "Measured I (A)", "Measured P (W)", "Soft Limit P (W)", "Hard Limit P (W)", "Set Limit (W)"]
        for i, h in enumerate(headers):
            ttk.Label(chs, text=h, font=("", 9, "bold")).grid(row=0, column=i, padx=4, pady=4)

        self.ch_vars = {}
        for ch in (1, 2, 3):
            row = ch
            ttk.Label(chs, text=f"CH{ch}").grid(row=row, column=0, padx=4, pady=4)

            v_var = tk.StringVar(value="0.0")
            i_var = tk.StringVar(value="0.0")
            ttk.Entry(chs, textvariable=v_var, width=10).grid(row=row, column=1, padx=4, pady=4)
            ttk.Entry(chs, textvariable=i_var, width=10).grid(row=row, column=2, padx=4, pady=4)

            apply_btn = ttk.Button(chs, text="Apply", command=lambda c=ch: self.apply_vi(c))
            apply_btn.grid(row=row, column=3, padx=4, pady=4)

            out_btn = ttk.Button(chs, text="Toggle", command=lambda c=ch: self.toggle_channel_output(c))
            out_btn.grid(row=row, column=4, padx=4, pady=4)

            mv = ttk.Label(chs, text="—")
            mi = ttk.Label(chs, text="—")
            mp = ttk.Label(chs, text="—")

            mv.grid(row=row, column=5, padx=4, pady=4)
            mi.grid(row=row, column=6, padx=4, pady=4)
            mp.grid(row=row, column=7, padx=4, pady=4)

            soft_lim_var = tk.StringVar(value="0.0")
            hard_lim_var = tk.StringVar(value="0.0")
            ttk.Entry(chs, textvariable=soft_lim_var, width=10).grid(row=row, column=8, padx=4, pady=4)
            ttk.Entry(chs, textvariable=hard_lim_var, width=10).grid(row=row, column=9, padx=4, pady=4)

            setlim_btn = ttk.Button(chs, text="Set", command=lambda c=ch: self.set_lim(c))
            setlim_btn.grid(row=row, column=10, padx=4, pady=4)

            self.ch_vars[ch] = {
                "v_var": v_var,
                "i_var": i_var,
                "mv": mv,
                "mi": mi,
                "mp": mp,
                "soft_lim": soft_lim_var,
                "hard_lim": hard_lim_var
            }

        # General Controls
        gen = ttk.LabelFrame(self, text="General Output & Measurements")
        gen.pack(fill="x", padx=pad, pady=pad)

        self.btn_master_on = ttk.Button(gen, text="Master ON", command=lambda: self.master_output(1), state="disabled")
        self.btn_master_on.grid(row=0, column=0, padx=pad, pady=pad)
        self.btn_master_off = ttk.Button(gen, text="Master OFF", command=lambda: self.master_output(0), state="disabled")
        self.btn_master_off.grid(row=0, column=1, padx=pad, pady=pad)

        self.btn_read = ttk.Button(gen, text="Read Measurements (All CH)", command=self.read_all_measurements, state="disabled")
        self.btn_read.grid(row=0, column=2, padx=pad, pady=pad)

        ttk.Checkbutton(gen, text="Auto-poll every 1s", variable=self.polling, command=self.on_toggle_poll).grid(row=0, column=3, padx=pad, pady=pad)

        # ---- Plot & Logging Controls ----
        plot_controls = ttk.LabelFrame(self, text="Live Plot & Logging")
        plot_controls.pack(fill="x", padx=pad, pady=pad)

        # Checkboxes per channel/metric
        grid_row = 0
        ttk.Label(plot_controls, text="Select series to plot/log:").grid(row=grid_row, column=0, padx=pad, pady=pad, sticky="w")
        grid_row += 1

        # Headers for CH columns
        for c, ch in enumerate((1,2,3), start=1):
            ttk.Label(plot_controls, text=f"CH{ch}", font=("", 9, "bold")).grid(row=grid_row, column=c, padx=4, pady=4)
        grid_row += 1

        # Rows for V, I, P
        for metric in ("V", "I", "P"):
            ttk.Label(plot_controls, text=metric).grid(row=grid_row, column=0, padx=4, pady=4, sticky="e")
            for c, ch in enumerate((1,2,3), start=1):
                key = f"CH{ch}_{'V' if metric=='V' else ('I' if metric=='I' else 'P')}"
                var = tk.BooleanVar(value=(metric == "V" and ch == 1))  # default only CH1_V selected
                self.series_vars[key] = var
                ttk.Checkbutton(plot_controls, variable=var).grid(row=grid_row, column=c, padx=4, pady=4)
            grid_row += 1

        # Sampling interval
        ttk.Label(plot_controls, text="Sampling interval (s):").grid(row=grid_row, column=0, padx=pad, pady=pad, sticky="e")
        ttk.Entry(plot_controls, textvariable=self.sample_interval_var, width=8).grid(row=grid_row, column=1, padx=4, pady=pad, sticky="w")
        grid_row += 1

        # Start/Stop button
        self.btn_plot_toggle = ttk.Button(plot_controls, text="Start Plot & Log", command=self.toggle_plot, state="disabled")
        self.btn_plot_toggle.grid(row=grid_row, column=0, padx=pad, pady=pad, sticky="w")

        # ---- Matplotlib Figure ----
        self.figure = Figure(figsize=(10, 3.6), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.ax.set_title("Live Measurements")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Value")
        self.ax.grid(True)
        self.ax.margins(x=2, y=2)

        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill="both", expand=True, padx=pad, pady=(0, pad))

        # Status bar
        self.status = ttk.Label(self, text="Ready.", relief="sunken", anchor="w")
        self.status.pack(fill="x", side="bottom")

    # --------------------------- VISA/Instrument ---------------------------
    def connect(self):
        if not RSINSTR_AVAILABLE:
            messagebox.showerror("RsInstrument missing", "RsInstrument is not installed or failed to import.\nInstall with:\n  pip install RsInstrument")
            return
        res = self.resource_var.get().strip()
        if not res:
            messagebox.showerror("Invalid resource", "Please enter a VISA resource string.")
            return
        try:
            self.inst = RsInstrument(res, id_query=True, reset=True, options="SelectVisa='rs',")
            self.inst.assert_minimum_version("1.53.0")
            idn = self.inst.query_str("*IDN?").strip()
            self.idn_label.config(text=f"Connected: {idn}")
            self.connected = True
            self.status.config(text="Connected.")
            self.btn_connect.config(state="disabled")
            self.btn_disconnect.config(state="normal")
            self.btn_master_on.config(state="normal")
            self.btn_master_off.config(state="normal")
            self.btn_read.config(state="normal")
            self.btn_plot_toggle.config(state="normal")
        except Exception as ex:
            self.inst = None
            self.connected = False
            messagebox.showerror("Connection failed", f"Could not connect:\n{ex}")
            self.status.config(text="Connection failed.")

    def disconnect(self):
        try:
            self.stop_poll()
            self.stop_plot()
            if self.inst is not None:
                # Safe master OFF before closing (optional; comment if undesired)
                try:
                    self.inst.write("OUTPut:GENeral 0")
                except Exception:
                    pass
                self.inst.close()
        except Exception:
            pass
        self.inst = None
        self.connected = False
        self.idn_label.config(text="Not connected")
        self.btn_connect.config(state="normal")
        self.btn_disconnect.config(state="disabled")
        self.btn_master_on.config(state="disabled")
        self.btn_master_off.config(state="disabled")
        self.btn_read.config(state="disabled")
        self.btn_plot_toggle.config(state="disabled")
        self.status.config(text="Disconnected.")

    def on_close(self):
        try:
            self.disconnect()
        finally:
            self.destroy()

    # --------------------------- Helpers ---------------------------
    def _validate_vi(self, v_str: str, i_str: str):
        try:
            v = float(v_str)
            i = float(i_str)
        except ValueError:
            raise ValueError("Voltage/Current must be numeric.")
        if not (MIN_VOLT <= v <= MAX_VOLT):
            raise ValueError(f"Voltage out of range [{MIN_VOLT}, {MAX_VOLT}] V.")
        if not (MIN_CURR <= i <= MAX_CURR):
            raise ValueError(f"Current out of range [{MIN_CURR}, {MAX_CURR}] A.")
        return v, i

    def _scpi_select_ch(self, ch: int):
        if self.inst is None:
            raise RuntimeError("Not connected.")
        self.inst.write(f"INSTrument:NSELect {ch}")

    # --------------------------- Actions ---------------------------
    def apply_vi(self, ch: int):
        if not self.connected or self.inst is None:
            messagebox.showwarning("Not connected", "Connect to the instrument first.")
            return
        v_str = self.ch_vars[ch]["v_var"].get()
        i_str = self.ch_vars[ch]["i_var"].get()
        try:
            v, i = self._validate_vi(v_str, i_str)
        except Exception as ex:
            messagebox.showerror("Invalid input", str(ex))
            return
        try:
            self._scpi_select_ch(ch)
            self.inst.write(f"SOURce:VOLTage:LEVel:IMMediate:AMPLitude {v}")
            self.inst.write(f"SOURce:CURRent:LEVel:IMMediate:AMPLitude {i}")
            # Optional: OPC sync
            self.inst.query_opc()
            self.status.config(text=f"CH{ch}: Set V={v:.3f} V, I={i:.3f} A")
        except Exception as ex:
            messagebox.showerror("SCPI Error", f"Failed to set CH{ch}:\n{ex}")

    def set_lim(self, ch: int):
        pass

    def toggle_channel_output(self, ch: int):
        if not self.connected or self.inst is None:
            messagebox.showwarning("Not connected", "Connect to the instrument first.")
            return
        try:
            self._scpi_select_ch(ch)
            try:
                state = int(self.inst.query_str("OUTPut:STATe?").strip())
                new_state = 0 if state else 1
            except Exception:
                state = 0
                new_state = 1
            self.inst.write(f"OUTPut:STATe {new_state}")
            self.status.config(text=f"CH{ch} Output {'ON' if new_state else 'OFF'}")
        except Exception as ex:
            messagebox.showerror("SCPI Error", f"Failed to toggle CH{ch} output:\n{ex}")

    def master_output(self, onoff: int):
        if not self.connected or self.inst is None:
            messagebox.showwarning("Not connected", "Connect to the instrument first.")
            return
        try:
            self.inst.write(f"OUTPut:GENeral {1 if onoff else 0}")
            self.status.config(text=f"Master Output {'ON' if onoff else 'OFF'}")
        except Exception as ex:
            messagebox.showerror("SCPI Error", f"Failed to set master output:\n{ex}")

    def read_all_measurements(self):
        if not self.connected or self.inst is None:
            messagebox.showwarning("Not connected", "Connect to the instrument first.")
            return
        try:
            for ch in (1, 2, 3):
                self._scpi_select_ch(ch)
                v = self.inst.query_str("MEASure:SCALar:VOLTage:DC?").strip()
                i = self.inst.query_str("MEASure:SCALar:CURRent:DC?").strip()
                p = self.inst.query_str("MEASure:SCALar:POWer?").strip()
                self.ch_vars[ch]["mv"].config(text=v)
                self.ch_vars[ch]["mi"].config(text=i)
                self.ch_vars[ch]["mp"].config(text=p)
            self.status.config(text="Measurements updated.")
        except Exception as ex:
            messagebox.showerror("SCPI Error", f"Failed to read measurements:\n{ex}")

    # --------------------------- Polling ---------------------------
    def on_toggle_poll(self):
        if self.polling.get():
            if not self.connected:
                self.polling.set(False)
                messagebox.showwarning("Not connected", "Connect first to enable auto-polling.")
                return
            self.start_poll()
        else:
            self.stop_poll()

    def start_poll(self):
        self.stop_poll_event.clear()
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        self.status.config(text="Auto-polling started.")

    def stop_poll(self):
        self.stop_poll_event.set()
        self.status.config(text="Auto-polling stopped.")

    def _poll_loop(self):
        while not self.stop_poll_event.is_set():
            try:
                self.read_all_measurements()
            except Exception:
                pass
            for _ in range(10):
                if self.stop_poll_event.is_set():
                    break
                time.sleep(0.1)

    # --------------------------- Plot & Logging ---------------------------
    def toggle_plot(self):
        if not self.plot_active:
            self.start_plot()
        else:
            self.stop_plot()

    def _collect_selected_keys(self):
        return [k for k, var in self.series_vars.items() if var.get()]

    def start_plot(self):
        if not self.connected or self.inst is None:
            messagebox.showwarning("Not connected", "Connect to the instrument first.")
            return

        # Validate sampling interval
        try:
            interval = float(self.sample_interval_var.get())
            if interval <= 0:
                raise ValueError
        except Exception:
            messagebox.showerror("Invalid interval", "Sampling interval must be a positive number (seconds).")
            return

        selected = self._collect_selected_keys()
        if not selected:
            messagebox.showwarning("No series selected", "Select at least one series to plot/log.")
            return

        # Ask for log filename (default = current date/time)
        default_name = datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        initialdir = os.getcwd()
        file_path = filedialog.asksaveasfilename(
            title="Select log file",
            defaultextension=".csv",
            initialfile=default_name,
            initialdir=initialdir,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not file_path:
            # User canceled
            return

        # Prepare buffers and lines
        self.start_time = time.time()
        self.buffers = {k: deque(maxlen=100000) for k in selected}  # large cap; we trim view via x-limits
        self.ax.cla()
        self.ax.set_title("Live Measurements")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Value")
        self.ax.grid(True)
        self.lines = {}
        for key in selected:
            line, = self.ax.plot([], [], label=key)  # use default colors
            line.sticky_edges.x[:] = []
            line.sticky_edges.y[:] = []
            self.lines[key] = line
        self.ax.legend(loc="upper left")
        self.canvas.draw()

        # Open CSV and write header
        self.log_file = open(file_path, "w", newline="")
        self.csv_writer = csv.writer(self.log_file)
        self.csv_header = ["timestamp_iso", "t_rel_s"] + selected
        self.csv_writer.writerow(self.csv_header)

        # Set active + schedule first tick
        self.plot_active = True
        self.btn_plot_toggle.config(text="Stop Plot & Log")
        self.status.config(text=f"Plotting & logging to: {file_path}")
        # Kick off loop
        self.after(int(interval * 1000), self._plot_tick)

    def stop_plot(self):
        if not self.plot_active:
            return
        self.plot_active = False
        self.btn_plot_toggle.config(text="Start Plot & Log")
        # Close log file if open
        try:
            if self.log_file:
                self.log_file.flush()
                self.log_file.close()
        except Exception:
            pass
        self.log_file = None
        self.csv_writer = None
        self.status.config(text="Plotting stopped.")

    def _parse_series_key(self, key):
        # key like "CH1_V" -> (ch:int, metric:str)
        ch = int(key[2])
        metric = key[-1]  # V/I/P
        return ch, metric

    def _read_metrics_for_selection(self, selected_keys):
        """
        Reads only what is needed for the selected series.
        Returns dict key->float value for all selected keys.
        """
        results = {}
        # Group by channel to avoid extra selects
        by_ch = {}
        for key in selected_keys:
            ch, metric = self._parse_series_key(key)
            by_ch.setdefault(ch, set()).add(metric)

        for ch, metrics in by_ch.items():
            self._scpi_select_ch(ch)
            # If multiple metrics are needed for this channel, read them appropriately
            # We'll read only what is needed
            if "V" in metrics:
                try:
                    v = float(self.inst.query_str("MEASure:SCALar:VOLTage:DC?").strip())
                except Exception:
                    v = float("nan")
                results[f"CH{ch}_V"] = v
            if "I" in metrics:
                try:
                    i = float(self.inst.query_str("MEASure:SCALar:CURRent:DC?").strip())
                except Exception:
                    i = float("nan")
                results[f"CH{ch}_I"] = i
            if "P" in metrics:
                try:
                    p = float(self.inst.query_str("MEASure:SCALar:POWer?").strip())
                except Exception:
                    p = float("nan")
                results[f"CH{ch}_P"] = p
        return results

    def _plot_tick(self):
        if not self.plot_active:
            return

        # Interval re-read allows live changes
        try:
            interval = float(self.sample_interval_var.get())
            if interval <= 0:
                interval = 1.0
        except Exception:
            interval = 1.0

        selected = self._collect_selected_keys()
        # Sync selection: if user changes checkboxes during run, adjust lines/buffers/log header
        # For simplicity, we keep logging columns fixed for a given run to avoid CSV inconsistency.
        # So during an active run, we only plot selected lines that exist, but do not add/remove CSV columns.
        # If a key is newly selected mid-run and wasn't in csv_header, it will be plotted but logged as blank (not present).
        # If a key was deselected, we keep plotting its existing line but skip new points.
        if self.csv_writer:
            fixed_keys = set(self.csv_header[2:])
        else:
            fixed_keys = set()

        try:
            now = time.time()
            t_rel = now - (self.start_time or now)
            iso = datetime.utcnow().isoformat()

            # Read instrument for currently selected series (even if not part of CSV header)
            if selected:
                read_keys = selected
            else:
                read_keys = []  # nothing to read

            values = self._read_metrics_for_selection(read_keys)

            # Update buffers/lines for selected ones; if a line didn't exist (checkbox turned on mid-run), create it
            # but only if we are not logging or logging allows it for plotting. We'll still add the line visually.
            for key in selected:
                if key not in self.lines:
                    line, = self.ax.plot([], [], label=key)
                    line.sticky_edges.x[:] = []
                    line.sticky_edges.y[:] = []
                    self.lines[key] = line
                    self.ax.legend(loc="upper left")
                if key not in self.buffers:
                    self.buffers[key] = deque(maxlen=100000)
                y = values.get(key, float("nan"))
                self.buffers[key].append((t_rel, y))

            # Also keep existing lines that may no longer be selected (we append nothing to them).

            # Update plot data
            for key, line in self.lines.items():
                buf = self.buffers.get(key, None)
                if buf and len(buf) > 0:
                    xs = [pt[0] for pt in buf]
                    ys = [pt[1] for pt in buf]
                    line.set_data(xs, ys)
            # Adjust x-limits to last PLOT_HISTORY_SEC seconds
            xmin = max(0.0, t_rel - PLOT_HISTORY_SEC)
            xmax = max(10.0, t_rel if t_rel > 10 else 10.0)
            self.ax.set_xlim(xmin, xmax)

            # --- Robust Y padding (works even for constant signals) ---
            all_ys = []
            for key, line in self.lines.items():
                xdata = line.get_xdata()
                ydata = line.get_ydata()
                # filter to visible x-range only
                ys_vis = [y for x, y in zip(xdata, ydata) if x >= xmin]
                all_ys.extend(ys_vis)

            if all_ys:
                ymin = min(all_ys)
                ymax = max(all_ys)
                yrange = ymax - ymin
                if yrange < 1e-9:
                    # Flat/near-flat: center around value with absolute + relative pad
                    ymid = 0.5 * (ymin + ymax)
                    abs_pad = max(0.5, 0.05 * max(abs(ymid), 1.0))  # e.g., 5V => ±0.5V
                    self.ax.set_ylim(ymid - abs_pad, ymid + abs_pad)
                else:
                    rel_pad = 0.1 * yrange
                    abs_pad = 0.05 * max(abs(ymax), abs(ymin), 1.0)
                    pad = max(rel_pad, abs_pad)
                    self.ax.set_ylim(ymin - pad, ymax + pad)

            self.canvas.draw_idle()

            # Write CSV row with fixed header subset
            if self.csv_writer:
                row = [iso, f"{t_rel:.3f}"]
                for key in self.csv_header[2:]:
                    if key in values:
                        row.append(values[key])
                    else:
                        # If a key is in header but not read this tick (e.g., deselected now), try buffers latest
                        if key in self.buffers and len(self.buffers[key]) > 0:
                            row.append(self.buffers[key][-1][1])
                        else:
                            row.append("")
                self.csv_writer.writerow(row)
                if self.log_file:
                    self.log_file.flush()

        except Exception as ex:
            # Show error but keep loop running
            self.status.config(text=f"Plot tick error: {ex}")

        # Schedule next tick
        if self.plot_active:
            self.after(int(interval * 1000), self._plot_tick)

    # --------------------------- End Plot & Logging ---------------------------


def main():
    app = NGEGui()
    app.mainloop()


if __name__ == "__main__":
    main()