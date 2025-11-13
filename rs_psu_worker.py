#!/usr/bin/env python3
# rs_psu_worker.py
# Watchdog process for R&S NGE series. Owns VISA. Enforces power limits, streams measurements.
# Threaded design: a command thread fills an internal action queue; the poll loop executes
# a bounded number of actions per cycle, then polls/enforces. VISA is only touched by poll thread.

import math
import time
import queue
import threading
import multiprocessing as mp
from datetime import datetime

try:
    from RsInstrument import RsInstrument
    RSINSTR_AVAILABLE = True
except Exception:
    RsInstrument = None  # type: ignore
    RSINSTR_AVAILABLE = False

# ---- Protocol strings (exported) ----
CMD_CONNECT      = "connect"
CMD_DISCONNECT   = "disconnect"
CMD_QUIT         = "quit"
CMD_SET_INTERVAL = "set_interval"
CMD_SET_VI       = "set_vi"
CMD_TOGGLE_CH    = "toggle_ch"
CMD_MASTER       = "master"
CMD_SET_LIMITS   = "set_limits"

MSG_STATUS       = "status"
MSG_CONNECTED    = "connected"
MSG_DISCONNECTED = "disconnected"
MSG_MEAS         = "meas"
MSG_EVENT        = "event"

# ---- Limits ----
MAX_VOLT, MIN_VOLT = 32.0, 0.0
MAX_CURR, MIN_CURR = 3.0, 0.0
ABSOLUTE_MAX_PWR   = MAX_VOLT * MAX_CURR

# ---- Utils ----
def now_iso() -> str:
    return datetime.utcnow().isoformat()

def ffloat(v, default=float("inf")) -> float:
    try:
        s = str(v).strip()
        if s.lower() in ("inf", ""):
            return float("inf")
        return float(s)
    except Exception:
        return default

class Watchdog(mp.Process):
    """
    Sole VISA owner. Command thread takes GUI commands -> enqueues actions.
    Poll thread (self.run) executes a bounded number of actions, then polls/enforces.
    """

    def __init__(self, cmd_q: mp.Queue, meas_q: mp.Queue, init_interval: float = 0.5):
        super().__init__(daemon=True)
        # IPC queues
        self.cmd_q  = cmd_q
        self.meas_q = meas_q

        # Control
        self.interval = max(0.05, float(init_interval))   # polling interval (seconds)
        self.max_actions_per_cycle = 10                   # prevent command starvation of polling

        # VISA / connection
        self.inst = None
        self.resource = None
        self.connected = False

        # Setpoints and limits (shared read by poll thread; written by command thread)
        self.v_set = {1: 0.0, 2: 0.0, 3: 0.0}
        self.i_set = {1: 0.0, 2: 0.0, 3: 0.0}
        self.lim_soft = {1: float("inf"), 2: float("inf"), 3: float("inf")}
        self.lim_hard = {1: float("inf"), 2: float("inf"), 3: float("inf")}

        # Crossings / latching
        self.prev_soft = {1: False, 2: False, 3: False}
        self.prev_hard = {1: False, 2: False, 3: False}
        self.latched   = {1: False, 2: False, 3: False}  # require P <= soft to clear

        self.start_t = time.time()
    
    # ---------------- Sending back to GUI ----------------
    def _send(self, obj: dict):
        try:
            self.meas_q.put_nowait(obj)
        except Exception:
            pass

    def _status(self, ok: bool, msg: str):
        self._send({"type": MSG_STATUS, "ok": ok, "msg": msg})

    # ---------------- VISA helpers (only call from poll thread) ----------------
    def _connect(self, resource: str):
        if not RSINSTR_AVAILABLE:
            self._status(False, "RsInstrument not available in worker")
            return
        try:
            self.inst = RsInstrument(resource, id_query=True, reset=True, options="SelectVisa='rs',")
            self.inst.assert_minimum_version("1.53.0")
            # Tighter timeouts avoid long stalls
            self.inst.visa_timeout = 2000  # ms
            self.inst.opc_timeout  = 2000
            try:
                self.inst.instrument_status_checking = False
            except Exception:
                pass
            idn = self.inst.query_str("*IDN?").strip()
            self.connected = True
            self.resource  = resource
            self._status(True, f"Connected: {idn}")
            self._send({"type": MSG_CONNECTED, "idn": idn})
        except Exception as ex:
            self.inst = None
            self.connected = False
            self._status(False, f"Connect failed: {ex}")

    def _disconnect(self):
        try:
            if self.inst:
                try:
                    self.inst.write("OUTPut:GENeral 0")
                except Exception:
                    pass
                self.inst.close()
        except Exception:
            pass
        self.inst = None
        self.resource = None
        self.connected = False
        self._send({"type": MSG_DISCONNECTED})
        self._status(True, "Disconnected")

    def _sel(self, ch: int):
        self.inst.write(f"INSTrument:NSELect {ch}")

    def _set_vi(self, ch: int, v: float, i: float):
        # shadow setpoints
        self.v_set[ch] = float(v)
        self.i_set[ch] = float(i)
        try:
            self._sel(ch)
            self.inst.write(f"SOURce:VOLTage:LEVel:IMMediate:AMPLitude {v}")
            self.inst.write(f"SOURce:CURRent:LEVel:IMMediate:AMPLitude {i}")
            self.inst.query_opc()
            self._status(True, f"CH{ch} set VI {v},{i}")
        except Exception as ex:
            self._status(False, f"CH{ch} set VI failed: {ex}")

    def _toggle_ch(self, ch: int):
        try:
            self._sel(ch)
            try:
                state = int(self.inst.query_str("OUTPut:STATe?").strip())
            except Exception:
                state = 0
            new_state = 0 if state else 1
            self.inst.write(f"OUTPut:STATe {new_state}")
            self._status(True, f"CH{ch} toggled -> {'ON' if new_state else 'OFF'}")
        except Exception as ex:
            self._status(False, f"CH{ch} toggle failed: {ex}")

    def _master(self, on: bool):
        try:
            self.inst.write(f"OUTPut:GENeral {1 if on else 0}")
            self._status(True, f"Master {'ON' if on else 'OFF'}")
        except Exception as ex:
            self._status(False, f"Master failed: {ex}")

    def _read_vip(self, ch: int):
        try:
            self._sel(ch)
            v = float(self.inst.query_str("MEASure:SCALar:VOLTage:DC?").strip())
            i = float(self.inst.query_str("MEASure:SCALar:CURRent:DC?").strip())
            p = float(self.inst.query_str("MEASure:SCALar:POWer?").strip())
            return v, i, p
        except Exception:
            return float("nan"), float("nan"), float("nan")

    def _ch_on(self, ch: int) -> bool:
        try:
            self._sel(ch)
            return int(self.inst.query_str("OUTPut:STATe?").strip()) == 1
        except Exception:
            return False

    def _cut(self, ch: int):
        try:
            self._sel(ch)
            self.inst.write("OUTPut:STATe 0")
        except Exception:
            pass

    # ---------------- Limits & Events ----------------
    def _event(self, ch, v, i, p, label):
        self._send({
            "type":  MSG_EVENT,
            "iso":   now_iso(),
            "t":     time.time() - self.start_t,
            "event": label,
            "ch":    ch,
            "V":     v,
            "I":     i,
            "P":     p
        })

    def _set_limits(self, ch: int, soft, hard):
        self.lim_soft[ch] = ffloat(soft)
        self.lim_hard[ch] = ffloat(hard)
        self._status(True, f"CH{ch} limits updated (soft={self.lim_soft[ch]}, hard={self.lim_hard[ch]})")

    def _check_limits(self, ch: int, v: float, i: float, p: float):
        soft = self.lim_soft[ch]
        hard = self.lim_hard[ch]

        # latch: require P <= soft (or soft inf) to clear
        if self.latched[ch]:
            if (not math.isfinite(soft)) or (p <= soft):
                self.latched[ch] = False
                self._event(ch, v, i, p, f"CH{ch}_LATCH_CLEARED")
            else:
                return

        soft_now = p > soft if math.isfinite(soft) else False
        hard_now = p > hard if math.isfinite(hard) else False

        if soft_now and not self.prev_soft[ch]:
            self._event(ch, v, i, p, f"CH{ch}_SOFT_CROSS_UP")
        elif (not soft_now) and self.prev_soft[ch]:
            self._event(ch, v, i, p, f"CH{ch}_SOFT_CROSS_DOWN")
        self.prev_soft[ch] = soft_now

        if hard_now and not self.prev_hard[ch]:
            self._event(ch, v, i, p, f"CH{ch}_HARD_CROSS_UP")
            if self._ch_on(ch):
                self._cut(ch)
                self._event(ch, v, i, p, f"CH{ch}_HARD_TRIP")
            self.latched[ch] = True
        elif (not hard_now) and self.prev_hard[ch]:
            self._event(ch, v, i, p, f"CH{ch}_HARD_CROSS_DOWN")
        self.prev_hard[ch] = hard_now

    # ---------------- Action execution (poll thread only) ----------------
    def _exec_action(self, act: dict):
        """Execute a single action dict; only called from poll/enforce thread."""
        tp = act.get("type")
        if tp == CMD_CONNECT:
            self._connect(act.get("resource", ""))
        elif tp == CMD_DISCONNECT:
            self._disconnect()
        elif tp == CMD_SET_VI and self.connected and self.inst:
            self._set_vi(int(act["ch"]), float(act["v"]), float(act["i"]))
        elif tp == CMD_TOGGLE_CH and self.connected and self.inst:
            self._toggle_ch(int(act["ch"]))
        elif tp == CMD_MASTER and self.connected and self.inst:
            self._master(bool(act["on"]))
        elif tp == CMD_SET_LIMITS:
            self._set_limits(int(act["ch"]), act.get("soft"), act.get("hard"))
        # CMD_SET_INTERVAL is handled by cmd thread (no VISA)

    # ---------------- Command thread ----------------
    def _cmd_loop(self):
        """Runs in its own thread; translates GUI commands into internal actions or state updates."""
        while not self.stop_evt.is_set():
            try:
                cmd = self.cmd_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if not isinstance(cmd, dict):
                continue
            tp = cmd.get("type")

            if tp == CMD_QUIT:
                # enqueue a disconnect so poll thread closes VISA cleanly
                try: self._actions.put_nowait({"type": CMD_DISCONNECT})
                except Exception: pass
                self.stop_evt.set()
                break

            elif tp == CMD_CONNECT:
                try: self._actions.put_nowait({"type": CMD_CONNECT, "resource": cmd.get("resource","")})
                except Exception: pass

            elif tp == CMD_DISCONNECT:
                try: self._actions.put_nowait({"type": CMD_DISCONNECT})
                except Exception: pass

            elif tp == CMD_SET_INTERVAL:
                try:
                    self.interval = max(0.05, float(cmd.get("interval", self.interval)))
                    self._status(True, f"Interval set to {self.interval}s")
                except Exception as ex:
                    self._status(False, f"Interval error: {ex}")

            elif tp == CMD_SET_VI:
                try: self._actions.put_nowait({"type": CMD_SET_VI, "ch": int(cmd["ch"]), "v": float(cmd["v"]), "i": float(cmd["i"])})
                except Exception: pass

            elif tp == CMD_TOGGLE_CH:
                try: self._actions.put_nowait({"type": CMD_TOGGLE_CH, "ch": int(cmd["ch"])})
                except Exception: pass

            elif tp == CMD_MASTER:
                try: self._actions.put_nowait({"type": CMD_MASTER, "on": bool(cmd["on"])})
                except Exception: pass

            elif tp == CMD_SET_LIMITS:
                try: self._actions.put_nowait({"type": CMD_SET_LIMITS, "ch": int(cmd["ch"]), "soft": cmd.get("soft"), "hard": cmd.get("hard")})
                except Exception: pass

    # ---------------- Main poll/enforce loop (process main thread) ----------------
    def run(self):
        self.stop_evt = mp.Event()
        # Internal action queue (threading queue; only poll thread executes actions)
        self._actions = queue.Queue()
        # Start command thread
        t = threading.Thread(target=self._cmd_loop, daemon=True)
        t.start()

        while not self.stop_evt.is_set():
            cycle_start = time.time()

            # 1) Execute a bounded number of pending actions to avoid starvation
            for _ in range(self.max_actions_per_cycle):
                try:
                    act = self._actions.get_nowait()
                except queue.Empty:
                    break
                self._exec_action(act)

            # 2) Poll + enforce
            if self.connected and self.inst:
                iso = now_iso()
                t_rel = time.time() - self.start_t
                data = {}
                for ch in (1, 2, 3):
                    v, i, p = self._read_vip(ch)
                    if any(map(math.isnan, (v, i, p))):
                        continue
                    data[f"CH{ch}"] = {"V": v, "I": i, "P": p}
                    self._check_limits(ch, v, i, p)
                if data:
                    self._send({"type": MSG_MEAS, "iso": iso, "t": t_rel, "data": data})

            # 3) Sleep remainder of interval
            dt = time.time() - cycle_start
            rest = self.interval - dt
            if rest > 0:
                time.sleep(rest)

        # Graceful shutdown
        try:
            if self.connected:
                self._disconnect()
        except Exception:
            pass
