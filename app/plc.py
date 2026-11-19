"""Talking to the BX9000 controller (ADS protocol, pyads library).

Reading (every second, by the background thread of main.py):
  - input image   (contacts, buttons, detectors)
  - output image  (lamps, shutters, devices)
  - %M area of the command and state variables
  - controller state (RUN / STOP), if the controller can tell

Writing: ONLY pulses on the command variables main.py allowed (see app/rules.py). A pulse:
  1. checks the bit is 0 (otherwise resets it; if it stays 1 → error, nothing is sent)
  2. sets the bit to 1
  3. waits HOMEWEB_PULSE_MS (100 ms by default)
  4. sets the bit back to 0 (up to 3 tries, reconnecting if needed)
  5. reads the bit again: it must be 0
The PLC program reacts to the rising edge, like to a button press.
The server never writes into outputs or state variables.

Extra safety:
  - at each (re)connection, every command bit is reset to 0;
  - at each read, a command bit found at 1 (while no pulse is running) is reset and logged;
    if it is still 1 at the next read, the command is blocked (something else writes into it).

DemoReader simulates the controller to test without hardware (HOMEWEB_DEMO=1).
"""
import logging
import random
import threading
import time

log = logging.getLogger("home-web")

IG_IN_BYTES = 0xF020
IG_OUT_BYTES = 0xF030
IG_M_BYTES = 0x4020
IG_M_BITS = 0x4021
ADS_ERR_UNSUPPORTED = {0x701, 0x702}   # service or index not supported by the controller
VERSION_SIGNAL = "__version__"          # name under which the version byte is reported


class StuckBit(Exception):
    """A command bit does not go back to 0: nothing more is sent on this variable."""


def _bit(image, n):
    """Value of bit n in a byte image."""
    return bool(image[n // 8] >> (n % 8) & 1)


class PlcReader:
    """Real access to the controller.

    io        : inputs / outputs read from the .tpy (app.tpy.load_io)
    flags     : {name: bit number} of all %M variables (commands + states)
    cmd_bits  : {name: bit number} of the ONLY variables the server may write
    pulse_s   : pulse length in seconds
    version   : byte number of the version variable (or None)
    """

    def __init__(self, io, flags, cmd_bits, netid, ip, port, local_netid, pulse_s, version=None, timeout_ms=2000):
        self.io, self.flags, self.cmd_bits, self.version = io, flags, dict(cmd_bits), version
        self.netid, self.ip, self.port, self.local_netid = netid, ip, port, local_netid
        self.pulse_s, self.timeout_ms = pulse_s, timeout_ms
        self.n_in = max(b for _, b, _ in io["in"]) // 8 + 1
        self.n_out = max(b for _, b, _ in io["out"]) // 8 + 1
        # %M area read in one request: from the first to the last byte used
        octets = [b // 8 for b in flags.values()] + ([version] if version is not None else [])
        self.m_start = min(octets) if octets else None
        self.m_len = max(octets) - self.m_start + 1 if octets else 0
        self.conn = None
        self._local_set = False
        self._ads = threading.Lock()      # one ADS request at a time (background read + commands)
        self.run = None                   # True = RUN, False = stopped, None = unknown / not supported
        self._state_ok = True             # can the controller report its state? (disabled if not)
        self._seen_high = set()           # command bits found at 1 at the previous read
        self.blocked = set()              # blocked commands (bit that does not go back to 0)

    # -------------------------------------------------------------- connection
    def _connect(self):
        import pyads  # imported here: pyads is not needed in demo mode
        if not self._local_set:
            pyads.set_local_address(self.local_netid)
            self._local_set = True
        conn = pyads.Connection(self.netid, self.port, self.ip)
        conn.open()
        conn.set_timeout(self.timeout_ms)
        self.conn = conn
        # After an outage a bit may have stayed at 1 (server stopped in the middle of a pulse):
        # every command is reset to 0 before anything else.
        for bit in self.cmd_bits.values():
            self._write(bit, False)
        log.info("Controller connected, %d command bits reset to 0", len(self.cmd_bits))

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def set_commands(self, cmd_bits, commands=None, lights=(), shutters=None):
        """New list of command bits (after an admin edited the plan).

        Bits ENTERING the list are reset to 0 at once (or at the next connection).
        """
        with self._ads:
            new = {n: b for n, b in cmd_bits.items() if n not in self.cmd_bits}
            self.cmd_bits = dict(cmd_bits)
            self.blocked &= set(cmd_bits)
            self._seen_high &= set(cmd_bits)
            if self.conn is not None:
                try:
                    for b in new.values():
                        self._write(b, False)
                except Exception:
                    self.close()

    # -------------------------------------------------------------- low level
    def _write(self, bit, value):
        import pyads
        self.conn.write(IG_M_BITS, bit, value, pyads.PLCTYPE_BOOL)

    def _read_bit(self, bit):
        """Reads one %M bit (the whole byte is read: the access already validated on the BX9000)."""
        import pyads
        octet = self.conn.read(IG_M_BYTES, bit // 8, pyads.PLCTYPE_BYTE)
        return bool(octet >> (bit % 8) & 1)

    def _read_state(self):
        """RUN or not. If the controller does not know this request, it is not asked again."""
        import pyads
        if not self._state_ok:
            return None
        try:
            return self.conn.read_state()[0] == pyads.ADSSTATE_RUN
        except pyads.ADSError as e:
            if getattr(e, "err_code", None) in ADS_ERR_UNSUPPORTED:
                self._state_ok = False
                log.info("The controller does not report RUN/STOP: check skipped")
            return None          # other error: unknown for this round (the reads themselves worked)

    # -------------------------------------------------------------- background read
    def read(self):
        """Returns {name: bool}: inputs, outputs and %M variables."""
        import pyads
        with self._ads:
            if self.conn is None:
                self._connect()
            try:
                in_img = self.conn.read(IG_IN_BYTES, 0, pyads.PLCTYPE_BYTE * self.n_in)
                out_img = self.conn.read(IG_OUT_BYTES, 0, pyads.PLCTYPE_BYTE * self.n_out)
                m_img = None
                if self.m_len:
                    m_img = list(self.conn.read(IG_M_BYTES, self.m_start, pyads.PLCTYPE_BYTE * self.m_len))
                self.run = self._read_state()
                if m_img is not None:
                    self._watch(m_img)
            except Exception:
                self.close()  # reconnect at the next round
                raise
        signals = {name: _bit(in_img, b) for name, b, _ in self.io["in"]}
        signals.update({name: _bit(out_img, b) for name, b, _ in self.io["out"]})
        if m_img is not None:
            signals.update({name: _bit(m_img, b - self.m_start * 8) for name, b in self.flags.items()})
            if self.version is not None:
                signals[VERSION_SIGNAL] = m_img[self.version - self.m_start]
        return signals

    def _watch(self, m_img):
        """A command bit may only be 1 during a pulse (never during a read)."""
        high = {n for n, b in self.cmd_bits.items() if _bit(m_img, b - self.m_start * 8)}
        for name in high:
            if name in self._seen_high:
                if name not in self.blocked:
                    log.error("%s stays at 1 despite the reset: command blocked. "
                              "Does the program write into this variable?", name)
                self.blocked.add(name)
            else:
                log.warning("%s found at 1 outside a pulse: reset to 0", name)
            self._write(self.cmd_bits[name], False)
        for name in self.blocked - high:
            log.info("%s is back to 0: command unblocked", name)
        self.blocked &= high
        self._seen_high = high

    # -------------------------------------------------------------- pulse
    def pulse(self, name):
        """Sends a pulse on variable `name` (already checked by main.py)."""
        bit = self.cmd_bits[name]                       # KeyError if the variable is not allowed
        with self._ads:
            if self.conn is None:
                self._connect()
            # 1. the bit must be 0 to start with
            if self._read_bit(bit):
                self._write(bit, False)
                time.sleep(0.05)
                if self._read_bit(bit):
                    self.blocked.add(name)
                    raise StuckBit(name)
            # 2-4. 1 for pulse_s, then 0 (going back to 0 is retried whatever happens)
            self._write(bit, True)
            try:
                time.sleep(self.pulse_s)
            finally:
                for attempt in range(3):
                    try:
                        if self.conn is None:
                            self._connect()               # also resets every command to 0
                        self._write(bit, False)
                        break
                    except Exception:
                        self.close()
                        if attempt == 2:
                            raise
                        time.sleep(0.2)
            # 5. final check
            if self._read_bit(bit):
                self.blocked.add(name)
                raise StuckBit(name)


class DemoReader:
    """Simulated controller (HOMEWEB_DEMO=1): same interface as PlcReader.

    Reacts to pulses like the PLC program would: a lamp toggles, a shutter motor runs for a few
    seconds, the garage door opens and closes, "all off" switches every lamp off, etc.
    Contacts of the plan start closed; the front door (rules: front_door) opens now and then.
    """

    def __init__(self, io, flags, rules, closed_inputs=(), version=None):
        self.flags, self.rules = flags, rules
        self.run, self.blocked = True, set()
        s = {name: name in closed_inputs for name, _, _ in io["in"]}
        s.update({name: False for name, _, _ in io["out"]})
        s.update({name: False for name in flags})
        if version is not None:
            s[VERSION_SIGNAL] = version                     # the simulated program is the right one
        self.signals = s
        self.commands, self.lights, self.shutters = {}, [], {}
        self.motors = {}                     # shutter output → reads left
        g = rules.garage or {}
        self.garage = {"pos": "closed", "dir": None, "left": 0}
        self.garage_sensor, self.garage_open_value = g.get("sensor"), bool(g.get("sensor_open_value", True))
        self.garage_motor = g.get("motor")
        self.touched = False
        self.tick = 0

    def set_commands(self, cmd_bits, commands, lights=(), shutters=None):
        self.commands, self.lights, self.shutters = commands, list(lights), dict(shutters or {})

    def read(self):
        self.tick += 1
        s = self.signals
        if not self.touched and self.lights and self.tick % 3 == 0:   # a bit of life until someone acts
            name = random.choice(self.lights)
            s[name] = not s[name]
        for out in list(self.motors):                         # shutters: the motor stops by itself
            self.motors[out] -= 1
            if self.motors[out] <= 0:
                s[out] = False
                del self.motors[out]
        g = self.garage
        if g["dir"]:
            g["left"] -= 1
            if g["left"] <= 0:
                g["pos"], g["dir"] = ("open" if g["dir"] == "up" else "closed"), None
        if self.garage_sensor:
            s[self.garage_sensor] = (g["pos"] != "closed") == self.garage_open_value
        if self.garage_motor:
            s[self.garage_motor] = g["dir"] is not None
        door = self.rules.front_door
        if door in s:                                          # front door: open 4 s every 30 s
            s[door] = self.tick % 30 not in range(10, 14)
        return dict(s)

    # -- effects of the pulses
    def _shutter(self, n, direction):
        up, dn = self.shutters.get(str(n), (None, None))
        if not up or not dn:
            return
        s = self.signals
        other = dn if direction == "up" else up
        if s.get(other):                                       # opposite press while moving: stop
            s[other] = False
            self.motors.pop(other, None)
            return
        out = up if direction == "up" else dn
        s[out] = True
        self.motors[out] = 4

    def _garage(self, want):
        g = self.garage
        if g["dir"]:                                           # moving → stop
            g["dir"], g["pos"] = None, "stopped"
            return
        if (want == "up" and g["pos"] == "open") or (want == "down" and g["pos"] == "closed"):
            return
        g["dir"], g["left"] = want, 5

    def pulse(self, name):
        self.touched = True
        s, c = self.signals, self.commands[name]
        if c.kind in ("light", "device", "toggle"):             # the block inverts the state
            s[c.toggle_sig] = not s[c.toggle_sig]
        elif c.kind == "shutter":
            self._shutter(*c.shutter)
        elif c.kind in ("garage_open", "garage_close"):
            self._garage("up" if c.kind == "garage_open" else "down")
        elif c.effect == "lights_off":
            for n in self.lights:
                s[n] = False
        elif isinstance(c.effect, dict) and "lights_on" in c.effect:
            for n in c.effect["lights_on"]:
                if n in s:
                    s[n] = True
        elif isinstance(c.effect, dict) and "shutters" in c.effect:
            for n in c.effect.get("only") or list(self.shutters):
                self._shutter(n, c.effect["shutters"])

    def close(self):
        pass
