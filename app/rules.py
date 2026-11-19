"""House rules: the fixed safety rules of an installation, loaded from a JSON file.

Nothing about a particular house is written in the code. The rules file (HOMEWEB_RULES,
example: examples/rules.json) says:

  command_prefix     prefix of the PLC variables the server may write (default "HMI_"). The command
                     variable of an output X is <prefix>X ("mirror" variable): a pulse on it does the
                     same as pressing the button of X.
  version_prefix     optional version check variable, "<prefix><n> AT %MB.. : BYTE := <n>;"
  forbidden_outputs  {output: reason}: outputs that are never controlled, even if an admin links
                     them to a lamp (network power, siren...)
  confirm_outputs    {output: description}: outputs that always ask for a confirmation
  functions          fixed commands shown in the Actions tab (scenes, toggles, maintenance)
  garage             optional garage door: open / close commands and the contact that says "open"
  siren              optional siren output: "the alarm is ringing" banner and log lines
  alerts             {state variable: text}: alert banner when the variable is 1
  states             read-only variables (copies of internal states made by the PLC program)
  state_prefixes     optional: every variable starting with one of these is read-only as well
  front_door         optional: input listed first in the log filter, and opened now and then in demo mode

An admin who edits the plan from the page cannot change any of this: a stolen admin account
cannot remove a confirmation, write into a state, or drive a forbidden output.

Texts (group, label, tile, alert...) are either a string or {"en": "...", "fr": "..."}.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import LANG, SEP, t

ROOT = Path(__file__).resolve().parent.parent


class RulesError(ValueError):
    """The rules file is invalid: the server refuses to start."""


def text(v, fallback=""):
    """A text of the rules file in the page language (string or {"en": ..., "fr": ...})."""
    if isinstance(v, dict):
        return v.get(LANG) or v.get("en") or next(iter(v.values()), fallback)
    return v if isinstance(v, str) and v else fallback


@dataclass(frozen=True)
class Function:
    """A fixed command of the Actions tab (from the rules file)."""
    var: str
    group: str
    label: str
    confirm: bool = False
    toggle: bool = False          # pulse = the PLC block inverts the state (like a lamp)
    state: tuple = ()             # toggle: possible state variables, the first readable one is used
    on: str = ""                  # toggle: text of the state when 1 / 0
    off: str = ""
    tile: str = ""                # toggle: shown in the "Alarm" tile when active
    effect: object = None         # "lights_off", {"lights_on": [...]}, {"shutters": "up", "only": [...]}
    admin: bool = False           # admins only (checked by the server)
    address: str = ""             # address suggested to the PLC program (tools/check_tpy.py declarations)
    plc_comment: str = ""


@dataclass(frozen=True)
class Command:
    """An allowed command: a PLC variable the server may set to 1 for a moment."""
    var: str                     # exact name of the variable in the PLC program (= command id)
    kind: str                    # light | device | shutter | toggle | function | garage_open | garage_close
    confirm: bool                # confirmation required
    watch: tuple                 # signals read again after the pulse to see the effect
    toggle_sig: str | None       # light / device / toggle: signal read before sending
    strict: bool = False         # True: error if toggle_sig has not changed 1.5 s after the pulse
    label: str = ""              # text for the log ("Kitchen spots", "Alarm"...)
    condition: tuple = ()        # (signal, required value, message): read just before sending
    admin: bool = False          # admins only
    shutter: tuple = ()          # shutter: (number, "up" | "down") (demo mode)
    effect: object = None        # function: its effect (demo mode)


@dataclass
class Rules:
    prefix: str = "HMI_"
    version_prefix: str = "HMI_Version_"
    forbidden: dict = field(default_factory=dict)
    confirm_outputs: dict = field(default_factory=dict)
    functions: list = field(default_factory=list)
    garage: dict | None = None
    siren: dict | None = None
    alerts: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)
    state_prefixes: tuple = ()
    front_door: str | None = None

    # ------------------------------------------------------------ helpers
    def is_state(self, var):
        """Known read-only variable?"""
        v = var.lower()
        return v in {s.lower() for s in self.states} or v in {a.lower() for a in self.alerts} \
            or any(v.startswith(p.lower()) for p in self.state_prefixes)

    def extra_reads(self):
        """Variables to read in %M besides the prefixed ones (states without the prefix)."""
        names = set(self.states) | set(self.alerts)
        for f in self.functions:
            names |= set(f.state)
        return names

    def reserved(self):
        """Variables used by the fixed commands (lowercase): never linked to a plan element."""
        r = {f.var.lower() for f in self.functions}
        if self.garage:
            r |= {self.garage["open"].lower(), self.garage["close"].lower()}
        return r

    def refusal(self, var, outputs):
        """Why `var` can NOT command a plan element (lamp, device, shutter). None if it can.

        `outputs`: names of the outputs of the PLC program.
        """
        v = var.lower()
        if not v.startswith(self.prefix.lower()) or v.startswith(self.version_prefix.lower()):
            return t("refuse.not_command", prefix=self.prefix)
        if self.is_state(var):
            return t("refuse.state")
        if v in self.reserved():
            return t("refuse.reserved")
        out = next((o for o in outputs if o.lower() == v[len(self.prefix):]), None)
        if out in self.forbidden:
            return t("refuse.forbidden", reason=self.forbidden[out])
        return None

    def forbidden_output(self, output):
        return self.forbidden.get(output)

    def mirror(self, output):
        """Command variable of an output: <prefix><output>."""
        return self.prefix + output

    # ------------------------------------------------------------ fixed commands
    def fixed_commands(self, readable, lights, shutters):
        """Functions and garage door: ({exact variable name: Command}, [(name, problem)]).

        readable : variables present in %M in the .tpy (exact names)
        lights   : outputs of the lamps of the plan (effect of "all off"...)
        shutters : {number: (output up, output down)} from the plan
        """
        exact = {n.lower(): n for n in readable}
        cmds, ignored = {}, []
        for f in self.functions:
            var = exact.get(f.var.lower())
            if var is None:
                continue
            watch = self.effect_signals(f.effect, lights, shutters)
            if f.toggle:
                st = next((exact[s.lower()] for s in f.state if s.lower() in exact), None)
                if st is None:
                    ignored.append((var, t("refuse.toggle_no_state", state=f.state[0] if f.state else "?")))
                    continue
                cmds[var] = Command(var, "toggle", f.confirm, (st,) + watch, st, label=f.group, admin=f.admin)
            else:
                cmds[var] = Command(var, "function", f.confirm, watch, None, label=f"{f.group}{SEP}{f.label}",
                                    admin=f.admin, effect=f.effect)
        g = self.garage
        if g:
            name = t("garage.name")
            sensor, is_open = g["sensor"], bool(g.get("sensor_open_value", True))
            up = exact.get(g["open"].lower())
            if up:
                cmds[up] = Command(up, "garage_open", True, (sensor,), None, label=f"{name}{SEP}{t('garage.open')}",
                                   condition=(sensor, not is_open, t("garage.not_closed")))
            dn = exact.get(g["close"].lower())
            if dn:
                cmds[dn] = Command(dn, "garage_close", True, (sensor,), None, label=f"{name}{SEP}{t('garage.close')}",
                                   condition=(sensor, is_open, t("garage.already_closed")))
        return cmds, ignored

    @staticmethod
    def effect_signals(effect, lights, shutters):
        """Signals to read after a function, to see its effect at once."""
        if effect == "lights_off":
            return tuple(lights)
        if isinstance(effect, dict) and "lights_on" in effect:
            return tuple(effect["lights_on"])
        if isinstance(effect, dict) and "shutters" in effect:
            only = effect.get("only") or list(shutters)
            return tuple(o for n in only for o in shutters.get(str(n), ()) if o)
        return ()

    # ------------------------------------------------------------ for the page
    def ui(self):
        """What the page needs to draw the Actions tab, the tiles and the alert banner."""
        return {
            "prefix": self.prefix,
            "functions": [{"var": f.var, "group": f.group, "label": f.label, "toggle": f.toggle,
                           "state": list(f.state), "on": f.on, "off": f.off, "tile": f.tile,
                           "effect": f.effect, "admin": f.admin} for f in self.functions],
            "garage": {k: self.garage.get(k) for k in ("open", "close", "sensor", "motor")}
                      | {"open_value": bool(self.garage.get("sensor_open_value", True))} if self.garage else None,
            "siren": {"output": self.siren["output"], "pause_s": self.siren.get("pause_s", 6)} if self.siren else None,
            "alerts": [[k, v] for k, v in self.alerts.items()],
            "confirm_outputs": sorted(self.confirm_outputs),
        }


# ---------------------------------------------------------------- loading
def rules_file():
    if os.environ.get("HOMEWEB_RULES"):
        return Path(os.environ["HOMEWEB_RULES"])
    return ROOT / "rules.json" if (ROOT / "rules.json").exists() else ROOT / "examples" / "rules.json"


def load(path=None):
    path = Path(path) if path else rules_file()
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RulesError(f"{path}: {e}")

    def names(key):
        v = d.get(key) or {}
        if not isinstance(v, dict):
            raise RulesError(f"{path}: '{key}' must be an object")
        return v

    funcs = []
    for i, f in enumerate(d.get("functions") or []):
        if not isinstance(f, dict) or not isinstance(f.get("var"), str):
            raise RulesError(f"{path}: functions[{i}] needs a 'var'")
        effect = f.get("effect")
        default_group = t("group.shutters") if isinstance(effect, dict) and "shutters" in effect else t("group.functions")
        group = text(f.get("group"), default_group)
        funcs.append(Function(
            var=f["var"], group=group, label=text(f.get("label"), group),
            confirm=bool(f.get("confirm")), toggle=bool(f.get("toggle")),
            state=tuple(f.get("state") or ()), on=text(f.get("on"), t("state.on")),
            off=text(f.get("off"), t("state.off")), tile=text(f.get("tile")), effect=effect,
            admin=bool(f.get("admin")), address=f.get("address", ""), plc_comment=f.get("plc_comment", "")))
        if funcs[-1].toggle and not funcs[-1].state:
            raise RulesError(f"{path}: toggle {f['var']} needs a 'state'")
    garage = d.get("garage")
    if garage is not None and not all(isinstance(garage.get(k), str) for k in ("open", "close", "sensor")):
        raise RulesError(f"{path}: 'garage' needs 'open', 'close' and 'sensor'")
    siren = d.get("siren")
    if siren is not None and not isinstance(siren.get("output"), str):
        raise RulesError(f"{path}: 'siren' needs an 'output'")
    return Rules(
        prefix=d.get("command_prefix", "HMI_"),
        version_prefix=d.get("version_prefix", "HMI_Version_"),
        forbidden={k: text(v) for k, v in names("forbidden_outputs").items()},
        confirm_outputs={k: text(v) for k, v in names("confirm_outputs").items()},
        functions=funcs,
        garage=garage,
        siren=dict(siren, label=text(siren.get("label"), t("siren.name"))) if siren else None,
        alerts={k: text(v, k) for k, v in names("alerts").items()},
        states=names("states"),
        state_prefixes=tuple(d.get("state_prefixes") or ()),
        front_door=d.get("front_door"),
    )
