#!/usr/bin/env python3
# rs_psu_worker.py
# Watchdog process for R&S NGE series. Owns VISA. Enforces power limits, streams measurements.

import math
import time
import queue
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

# ---- Defaults / limits ----
MAX_VOLT, MIN_VOLT = 32.0, 0.0
MAX_CURR, MIN_CURR = 3.0, 0.0
ABSOLUTE_MAX_PWR   = MAX_VOLT * MAX_CURR

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
    Sole VISA owner. Polls CH1..CH3 at interval, enforces soft/hard power limits,
    streams measurements and events to GUI via meas_q, and consumes commands via cmd_q.
    """
    def __init__(self, cmd_q: mp.Queue, meas_q: mp.Queue, init_interval: float = 0.5):
        super().__init__(daemon=True)
        self.cmd_q   = cmd_q
        self.meas_q  = meas_q
        self.interval = max(0.05, float(init_interval))
        self.inst     = None
        self.resource = None
        self.connected = False

        # setpoints and limits
        self.v_set = {1: 0.0, 2: 0.0, 3: 0.0}
        self.i_set = {1: 0.0, 2: 0.0, 3: 0.0}
        self.lim_soft = {1: float("inf"), 2: float("inf"), 3: float("inf")}
        self.lim_hard = {1: float("inf"), 2: float("inf"), 3: float("inf")}

        # crossings / latching
        self.prev_soft = {1: False, 2: False, 3: False}
        self.prev_hard = {1: False, 2: False, 3: False}
        self.latched   = {1: False, 2: False, 3: False}  # require P <= soft to clear

        self.start_t = time.time()

    # ---------- helpers ----------
    def _send(self, obj: dict):
        try:
            self.meas_q.put_nowait(obj)
        except Exception:
            pass

    def _status(self, ok: bool, msg: str):
        self._send({"type": MSG_STATUS, "ok": ok, "msg": msg})

    # ---------- VISA ----------
    def _connect(self, resource: str):
        if not RSINSTR_AVAILABLE:
            self._status(False, "RsInstrument not available in worker")
            return
        try:
            self.inst = RsInstrument(resource, id_query=True, reset=True, options="SelectVisa='rs',")
            self.inst.assert_minimum_version("1.53.0")
            # Shorter timeouts reduce stalls
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

    def _set_vi(self, ch: int, v: float, i: float) -> bool:
        self.v_set[ch] = float(v)
        self.i_set[ch] = float(i)
        try:
            self._sel(ch)
            self.inst.write(f"SOURce:VOLTage:LEVel:IMMediate:AMPLitude {v}")
            self.inst.write(f"SOURce:CURRent:LEVel:IMMediate:AMPLitude {i}")
            self.inst.query_opc()
            return True
        except Exception:
            return False

    def _toggle_ch(self, ch: int):
        try:
            self._sel(ch)
            try:
                state = int(self.inst.query_str("OUTPut:STATe?").strip())
            except Exception:
                state = 0
            new_state = 0 if state else 1
            self.inst.write(f"OUTPut:STATe {new_state}")
            return new_state
        except Exception:
            return None

    def _master(self, on: bool) -> bool:
        try:
            self.inst.write(f"OUTPut:GENeral {1 if on else 0}")
            return True
        except Exception:
            return False

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

    # ---------- limits ----------
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
        return True

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

    # ---------- main loop ----------
    def run(self):
        while True:
            t0 = time.time()

            # drain commands
            while True:
                try:
                    cmd = self.cmd_q.get_nowait()
                except queue.Empty:
                    break
                if not isinstance(cmd, dict):
                    continue
                tp = cmd.get("type")
                if tp == CMD_CONNECT:
                    self._connect(cmd.get("resource", ""))
                elif tp == CMD_DISCONNECT:
                    self._disconnect()
                elif tp == CMD_QUIT:
                    self._disconnect()
                    return
                elif not self.connected or self.inst is None:
                    # ignore mutating commands when not connected
                    continue
                elif tp == CMD_SET_INTERVAL:
                    try:
                        self.interval = max(0.05, float(cmd.get("interval", self.interval)))
                        self._status(True, f"Interval set to {self.interval}s")
                    except Exception as ex:
                        self._status(False, f"Interval error: {ex}")
                elif tp == CMD_SET_VI:
                    ok = self._set_vi(int(cmd["ch"]), float(cmd["v"]), float(cmd["i"]))
                    self._status(ok, f"CH{cmd['ch']} set VI {cmd['v']},{cmd['i']}")
                elif tp == CMD_TOGGLE_CH:
                    new = self._toggle_ch(int(cmd["ch"]))
                    self._status(new is not None, f"CH{cmd['ch']} toggled")
                elif tp == CMD_MASTER:
                    ok = self._master(bool(cmd["on"]))
                    self._status(ok, f"Master {'ON' if cmd['on'] else 'OFF'}")
                elif tp == CMD_SET_LIMITS:
                    ch = int(cmd["ch"])
                    ok = self._set_limits(ch, cmd.get("soft"), cmd.get("hard"))
                    self._status(ok, f"CH{ch} limits updated")

            # Poll + enforce
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

            # sleep remainder
            dt = time.time() - t0
            rest = self.interval - dt
            if rest > 0:
                time.sleep(rest)
