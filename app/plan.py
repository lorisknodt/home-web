"""The house plan: lamps, devices, doors and windows, detectors, shutters.

Walls and rooms come from house.json (fixed, sent to the page). Everything placed on them is
here, in a file admins edit from the page (edit mode):

    /var/lib/home-web/plan.json               plan in use
    /var/lib/home-web/plan-history/r12.json   previous versions (the last 100)
    plan_default.json                         starting plan, used until plan.json exists

Format
------
{"rev": 12, "format": 1,
 "elements": [
   {"id": "e8", "floor": "ground", "t": "light", "name": "Living room ceiling",
    "sig": "DO_Living_Ceiling", "cmd": "HMI_DO_Living_Ceiling", "x": 325, "y": 250},
   {"id": "e3", "floor": "ground", "t": "contact", "name": "Living room window", "sig": "DI_Living_Window",
    "x1": 400, "y1": 0, "x2": 540, "y2": 0, "out": [0, -1], "shutter": 2},
   {"id": "e20", "floor": "ground", "t": "dev", "kind": "socket", "letter": "S", "name": "Terrace socket",
    "sig": "DO_Terrace_Socket", "cmd": "HMI_DO_Terrace_Socket", "x": 45, "y": 160}, ...],
 "shutters": {"2": {"name": "Living room window", "up": "HMI_DO_Shutter2_Up", "down": "HMI_DO_Shutter2_Down",
                    "out_up": "DO_Shutter2_Up", "out_down": "DO_Shutter2_Down"}, ...}}

  t        light, dev (device), contact (door / window), garage (garage door), fire (smoke detector),
           water (water detector), tech (no position, Info tab)
  sig      signal read for the state: an output (lamp, device) or an input (contacts, detectors)
  cmd      lamp / device / tech: command variable (null = read only)
  floor    a floor id of house.json, or null (element without a position)
  position x, y (one point), at [[x, y], ...] (several points), segs [[x1, y1, x2, y2], ...] (bars),
           x1 y1 x2 y2 + out (contacts: opening and outer side). Coordinates in cm.
  kind     device: kind of device (KINDS); `letter` follows from it
  icon     device: icon drawn on the plan (ICONS); missing = the one of its kind
  shutter  contact: number of the shutter of this window (1 to 8)
  confirm  lamp / device: ask for a confirmation. Always asked, whatever is ticked, for the
           outputs of rules.confirm_outputs and for "tech" elements
  on, off  tech: texts shown for 1 / 0

Rules applied at each save (validate): admins can rename, move and change the variables of any
element, turn a lamp into a device (and back), and add or remove LAMPS and DEVICES only. A
command variable must pass rules.refusal (not forbidden, not a state, not reserved) and be
used only once.
"""
import copy
import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from .eventlog import TZ
from .i18n import SEP, quote, t
from .rules import Command

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def _file(env, name, example):
    """House file: environment variable, else next to the app, else the example of the repository."""
    if os.environ.get(env):
        return Path(os.environ[env])
    return ROOT / name if (ROOT / name).exists() else EXAMPLES / example


def house_file():
    return _file("HOMEWEB_HOUSE", "house.json", "house.json")


def default_plan_file():
    return _file("HOMEWEB_PLAN_DEFAULT", "plan_default.json", "plan.json")


FLOORS = ()                                          # floor ids, set by load_house()


def load_house():
    """Walls and rooms (house.json); also sets the floors allowed in the plan."""
    global FLOORS
    house = json.loads(house_file().read_text(encoding="utf-8"))
    FLOORS = tuple(f["id"] for f in house["floors"])
    return house


TYPES = ("light", "dev", "contact", "garage", "fire", "water", "tech")
ADDABLE = ("light", "dev")                           # the only types an admin can add or remove
TRACKED = ("contact", "garage")                      # openings and closings written to the log
# Kinds of devices (t = "dev"): letter, default icon. Same list in the page (constant KINDS).
KINDS = {
    "socket": ("S", "socket"),
    "fan":    ("F", "fan"),
    "heater": ("H", "radiator"),
    "pump":   ("P", "pump"),
    "other":  ("D", "letter"),
}
# Icons an admin can choose for a device (drawings in the page: constant ICONS).
# "letter" = the letter of the kind.
ICONS = ("socket", "fan", "radiator", "flame", "drop", "pump", "snowflake", "bolt",
         "plant", "screen", "washer", "sun", "letter")
FORMAT = 1
KEEP_VERSIONS = 100
BOUNDS_X, BOUNDS_Y = (-400, 1800), (-400, 1500)
TEXT = re.compile(r"[^\x00-\x1f\x7f<>]{1,60}")


class InvalidPlan(ValueError):
    """Edit refused: the message is shown as is in the page."""


def what(e):
    """Name of the kind of element, for the log ("Socket", "Lamp"...)."""
    if e["t"] == "dev":
        return t("kind." + (e.get("kind") if e.get("kind") in KINDS else "other"))
    if e["t"] == "light" and e.get("segs"):         # lamp drawn as a bar (LED strip, tube)
        return "LED"
    return t("type." + e["t"])


def icon(e):
    """Icon of a device as drawn (chosen, else the one of its kind)."""
    return e.get("icon") or KINDS.get(e.get("kind"), KINDS["other"])[1]


# ---------------------------------------------------------------- validation
def _number(v, bounds, label):
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not bounds[0] <= v <= bounds[1]:
        raise InvalidPlan(t("plan.off_plan", what=label))
    return round(v)


def _text(v, label, maxlen=60):
    if not isinstance(v, str) or not TEXT.fullmatch(v.strip()) or len(v.strip()) > maxlen:
        raise InvalidPlan(t("plan.bad_text", what=label, max=maxlen))
    return v.strip()


def _point(p, label):
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise InvalidPlan(t("plan.bad_point", what=label))
    return [_number(p[0], BOUNDS_X, label), _number(p[1], BOUNDS_Y, label)]


def validate(plan, rules, inputs, outputs, old=None):
    """Checks the whole plan and returns a clean copy. Raises InvalidPlan otherwise.

    inputs / outputs: names of the inputs and outputs of the program (.tpy).
    old: previous plan (to forbid adding or removing anything but lamps and devices).
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("elements"), list):
        raise InvalidPlan(t("plan.unreadable"))
    els = plan["elements"]
    if len(els) > 300:
        raise InvalidPlan(t("plan.too_many"))
    olds = {e["id"]: e for e in (old or {}).get("elements", [])}
    clean, ids, sigs, cmds = [], set(), set(), set()

    def cmd_ok(cmd, where):
        if cmd in (None, ""):
            return None
        cmd = _text(cmd, where, 60)
        r = rules.refusal(cmd, outputs)
        if r:
            raise InvalidPlan(t("plan.cmd_refused", where=where, cmd=cmd, reason=r))
        if cmd.lower() in cmds:
            raise InvalidPlan(t("plan.cmd_twice", where=where, cmd=cmd))
        cmds.add(cmd.lower())
        return cmd

    for e in els:
        if not isinstance(e, dict):
            raise InvalidPlan(t("plan.bad_element"))
        typ = e.get("t")
        if typ not in TYPES:
            raise InvalidPlan(t("plan.bad_type"))
        eid = e.get("id")
        if not isinstance(eid, str) or not re.fullmatch(r"e\d{1,5}", eid) or eid in ids:
            raise InvalidPlan(t("plan.bad_id"))
        ids.add(eid)
        if old is not None:
            if eid not in olds and typ not in ADDABLE:
                raise InvalidPlan(t("plan.only_add"))
            if eid in olds and olds[eid]["t"] != typ and {olds[eid]["t"], typ} != set(ADDABLE):
                raise InvalidPlan(t("plan.type_fixed"))
        name = _text(e.get("name"), t("field.name"))
        floor = e.get("floor")
        if floor is not None and floor not in FLOORS:
            raise InvalidPlan(t("plan.bad_floor"))
        n = {"id": eid, "floor": floor, "t": typ, "name": name}

        sig = e.get("sig")
        allowed = outputs if typ in ("light", "dev") else inputs if typ in ("contact", "garage", "fire", "water") \
            else inputs | outputs
        unchanged = eid in olds and olds[eid].get("sig") == sig
        if sig not in allowed and not unchanged:          # a signal already in place is not checked again
            raise InvalidPlan(t("plan.sig_missing", name=name, sig=sig))
        if sig in sigs:
            raise InvalidPlan(t("plan.sig_twice", name=name, sig=sig))
        sigs.add(sig)
        n["sig"] = sig

        if typ in ("light", "dev", "tech"):
            n["cmd"] = cmd_ok(e.get("cmd"), name)
            if n["cmd"] and rules.forbidden_output(sig):
                raise InvalidPlan(t("plan.sig_forbidden", name=name, sig=sig, reason=rules.forbidden_output(sig)))
        if typ in ("light", "dev") and e.get("confirm"):
            n["confirm"] = True

        # position
        if floor is not None:
            if typ in ("contact", "garage"):
                for k in ("x1", "x2"):
                    n[k] = _number(e.get(k), BOUNDS_X, name)
                for k in ("y1", "y2"):
                    n[k] = _number(e.get(k), BOUNDS_Y, name)
                out = e.get("out")
                if not (isinstance(out, list) and len(out) == 2 and all(v in (-1, 0, 1) for v in out)):
                    raise InvalidPlan(t("plan.bad_side", name=name))
                n["out"] = out
            elif "segs" in e:
                segs = e["segs"]
                if not isinstance(segs, list) or not 1 <= len(segs) <= 4:
                    raise InvalidPlan(t("plan.bad_bars", name=name))
                n["segs"] = [[_number(s[0], BOUNDS_X, name), _number(s[1], BOUNDS_Y, name),
                              _number(s[2], BOUNDS_X, name), _number(s[3], BOUNDS_Y, name)]
                             for s in segs if isinstance(s, (list, tuple)) and len(s) == 4]
                if len(n["segs"]) != len(segs):
                    raise InvalidPlan(t("plan.bad_bars", name=name))
            elif "at" in e:
                at = e["at"]
                if not isinstance(at, list) or not 1 <= len(at) <= 12:
                    raise InvalidPlan(t("plan.bad_points", name=name))
                n["at"] = [_point(p, name) for p in at]
            elif "x" in e or "y" in e:
                n["x"], n["y"] = _point([e.get("x"), e.get("y")], name)
            elif typ not in ("light", "tech"):          # a lamp may be listed under a floor without a position
                raise InvalidPlan(t("plan.no_position", name=name))
        elif typ not in ("light", "tech"):
            raise InvalidPlan(t("plan.must_stay", name=name))
        if floor is None and typ == "light" and e.get("room"):
            n["room"] = _text(e["room"], t("field.room"), 40)
        if typ == "dev":
            kind = e.get("kind") or "other"
            if kind not in KINDS:
                raise InvalidPlan(t("plan.bad_kind", name=name))
            n["kind"], n["letter"] = kind, KINDS[kind][0]
            ic = e.get("icon")
            if ic not in (None, "") and ic not in ICONS:
                raise InvalidPlan(t("plan.bad_icon", name=name))
            if ic and ic != KINDS[kind][1]:              # the default icon is not stored
                n["icon"] = ic
        if typ == "contact" and e.get("shutter") not in (None, 0):
            if e["shutter"] not in range(1, 9):
                raise InvalidPlan(t("plan.bad_shutter", name=name))
            n["shutter"] = e["shutter"]
        if typ == "tech":
            n["on"] = _text(e.get("on", t("state.yes")), t("field.on_text"), 20)
            n["off"] = _text(e.get("off", t("state.no")), t("field.off_text"), 20)
            if e.get("inv"):
                n["inv"] = True
        clean.append(n)

    if old is not None:
        for eid, e in olds.items():
            if eid not in ids and e["t"] not in ADDABLE:
                raise InvalidPlan(t("plan.only_remove"))

    shutters = {}
    olds_s = (old or {}).get("shutters")
    given = plan.get("shutters") or {}
    if olds_s is not None and set(given) != set(olds_s):
        raise InvalidPlan(t("plan.shutters_fixed"))
    for k, v in given.items():
        if k not in {str(i) for i in range(1, 9)} or not isinstance(v, dict):
            raise InvalidPlan(t("plan.bad_shutter_entry"))
        name = _text(v.get("name"), t("shutter.n", n=k), 40)
        sv = {"name": name}
        for d in ("up", "down"):
            sv[d] = cmd_ok(v.get(d), t("shutter.named", name=name))
        for d in ("out_up", "out_down"):
            o = v.get(d)
            if o not in (None, "") and o not in outputs and (olds_s or {}).get(k, {}).get(d) != o:
                raise InvalidPlan(t("plan.shutter_output_missing", name=name, out=o))
            sv[d] = o or None
        shutters[k] = sv
    return {"rev": plan.get("rev", 0), "format": FORMAT, "elements": clean, "shutters": shutters}


# ---------------------------------------------------------------- commands from the plan
def lights_of(plan):
    """Outputs of the lamps of the plan."""
    return [e["sig"] for e in plan["elements"] if e["t"] == "light"]


def shutter_outputs(plan):
    """{number: (output up, output down)}."""
    return {k: (v.get("out_up"), v.get("out_down")) for k, v in plan["shutters"].items()}


def commands(plan, rules, readable, outputs):
    """Commands of the lamps, devices and shutters: ({exact name: Command}, [(variable, problem)])."""
    exact = {n.lower(): n for n in readable}
    cmds, problems = {}, []

    def var(name, where):
        if not name:
            return None
        v = exact.get(name.lower())
        if v is None:
            problems.append((name, t("problem.missing", where=where)))
            return None
        r = rules.refusal(v, outputs)                     # checked twice (the plan was validated already)
        if r:
            problems.append((v, f"{r} ({where})"))
            return None
        return v

    for e in plan["elements"]:
        if e["t"] not in ("light", "dev", "tech") or not e.get("cmd"):
            continue
        v = var(e["cmd"], e["name"])
        if not v or rules.forbidden_output(e["sig"]):
            continue
        confirm = e["t"] == "tech" or bool(e.get("confirm")) or e["sig"] in rules.confirm_outputs
        cmds[v] = Command(v, "light" if e["t"] == "light" else "device", confirm, (e["sig"],), e["sig"],
                          strict=True, label=e["name"])
    for k, s in sorted(plan["shutters"].items()):
        watch = tuple(x for x in (s.get("out_up"), s.get("out_down")) if x)
        for d in ("up", "down"):
            v = var(s.get(d), t("shutter.named", name=s["name"]))
            if v:
                cmds[v] = Command(v, "shutter", False, watch, None, shutter=(k, d),
                                  label=f"{t('shutter.named', name=s['name'])}{SEP}{t('shutter.' + d)}")
    return cmds, problems


# ---------------------------------------------------------------- text of the changes
def _position(e):
    return {k: e.get(k) for k in ("floor", "x", "y", "at", "segs", "x1", "y1", "x2", "y2", "out")}


def differences(before, after):
    """Readable lines describing what changed (for the log)."""
    a = {e["id"]: e for e in before["elements"]}
    b = {e["id"]: e for e in after["elements"]}
    none = t("diff.none")
    out = []
    for eid in b.keys() - a.keys():
        e = b[eid]
        out.append(t("diff.added", what=what(e), name=e["name"], sig=e["sig"], cmd=e.get("cmd") or t("diff.no_cmd")))
    for eid in a.keys() - b.keys():
        out.append(t("diff.removed", what=what(a[eid]), name=a[eid]["name"]))
    for eid in a.keys() & b.keys():
        x, y = a[eid], b[eid]
        ch = []
        if what(x) != what(y):
            ch.append(t("diff.type", a=what(x), b=what(y)))
        if x["name"] != y["name"]:
            ch.append(t("diff.name", a=x["name"], b=y["name"]))
        if x["t"] == y["t"] == "dev" and icon(x) != icon(y):
            ch.append(t("diff.icon", a=icon(x), b=icon(y)))
        if x["sig"] != y["sig"]:
            ch.append(t("diff.state", a=x["sig"], b=y["sig"]))
        if x.get("cmd") != y.get("cmd"):
            ch.append(t("diff.cmd", a=x.get("cmd") or none, b=y.get("cmd") or none))
        if x.get("shutter") != y.get("shutter"):
            ch.append(t("diff.shutter", a=x.get("shutter") or none, b=y.get("shutter") or none))
        if bool(x.get("confirm")) != bool(y.get("confirm")):
            ch.append(t("diff.confirm_on") if y.get("confirm") else t("diff.confirm_off"))
        if _position(x) != _position(y):
            ch.append(t("diff.moved"))
        if ch:
            out.append(f"{what(y)} {quote(y['name'])}{SEP}" + "; ".join(ch))
    labels = {"up": t("diff.cmd_up"), "down": t("diff.cmd_down"), "out_up": t("diff.out_up"), "out_down": t("diff.out_down")}
    for k in sorted(after["shutters"]):
        x, y = before["shutters"].get(k, {}), after["shutters"][k]
        ch = [f"{labels[c]} {x.get(c) or none} → {y.get(c) or none}"
              for c in ("up", "down", "out_up", "out_down") if x.get(c) != y.get(c)]
        if x.get("name") != y.get("name"):
            ch.insert(0, t("diff.name", a=x.get("name"), b=y["name"]))
        if ch:
            out.append(f"{t('shutter.n', n=k)} {quote(y['name'])}{SEP}" + "; ".join(ch))
    return out


# ---------------------------------------------------------------- file and history
class Plan:
    """Plan in use, with history. `folder` = None: in memory only (demo mode)."""

    def __init__(self, folder):
        self.folder = Path(folder) if folder else None
        self._lock = threading.Lock()
        path = self.folder / "plan.json" if self.folder else None
        source = path if path and path.exists() else default_plan_file()
        self.data = json.loads(source.read_text(encoding="utf-8"))

    def copy(self):
        with self._lock:
            return copy.deepcopy(self.data)

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".plan-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def save(self, new, expected_rev, user, summary):
        """Replaces the plan if nobody changed it meanwhile. Keeps the old one in the history."""
        with self._lock:
            if expected_rev != self.data["rev"]:
                raise InvalidPlan(t("plan.changed_meanwhile"))
            new = dict(new, rev=self.data["rev"] + 1, format=FORMAT,
                       author=user, date=datetime.now(TZ).isoformat(timespec="seconds"), summary=summary)
            if self.folder:
                old = dict(self.data)
                self._write(self.folder / "plan-history" / f"r{old['rev']}.json", old)
                self._write(self.folder / "plan.json", new)
                versions = sorted((self.folder / "plan-history").glob("r*.json"), key=lambda f: int(f.stem[1:]))
                for f in versions[:-KEEP_VERSIONS]:
                    f.unlink(missing_ok=True)
            self.data = new
            return copy.deepcopy(new)

    def history(self):
        """Previous versions, newest first."""
        if not self.folder:
            return []
        out = []
        for f in sorted((self.folder / "plan-history").glob("r*.json"), key=lambda f: -int(f.stem[1:])):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"rev": d.get("rev"), "date": d.get("date", ""), "author": d.get("author", ""),
                        "summary": d.get("summary", "")})
        return out

    def version(self, rev):
        if not self.folder or not isinstance(rev, int):
            raise InvalidPlan(t("plan.no_version"))
        f = self.folder / "plan-history" / f"r{rev}.json"
        if not f.exists():
            raise InvalidPlan(t("plan.no_version"))
        return json.loads(f.read_text(encoding="utf-8"))
