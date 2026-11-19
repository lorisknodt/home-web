"""home-web — FastAPI server.

Role of each file
-----------------
  app/main.py       this file: page, login, roles, commands, plan, log
  app/rules.py      fixed safety rules of the house, loaded from rules.json
  app/plan.py       the plan admins edit (lamps, variables...) and its checks
  app/accounts.py   login accounts (created in the container console)
  app/eventlog.py   event log (Logs tab, admins only)
  app/plc.py        talking to the controller: reading the state, pulses; simulated controller
  app/tpy.py        reading the .tpy file (names and addresses of the variables)
  app/i18n.py       texts of the server in English and French
  app/static/index.html  the page (drawing, display, edit mode)

Two groups of accounts
----------------------
  user   sees everything and sends every command
  admin  also edits the plan (edit mode) and reads the log (Logs tab)
The group is checked by the server at each request (require_admin), not only hidden in the page.

Path of a command (example: switch on the kitchen spots)
--------------------------------------------------------
  1. The page sends POST /api/cmd {"id": "HMI_DO_Kitchen_Spots", "on": true}
  2. command() checks: valid session, variable in the whitelist (built from the plan and the
     rules), controller ready, confirmation if needed, one command at a time, 40 per minute
     at most; for a toggle, the state is read again and nothing is sent if it is already right.
  3. plc.PlcReader.pulse() sets the %MX bit to 1 for 100 ms, then to 0.
  4. The PLC program sees the rising edge and toggles the output DO_Kitchen_Spots.
  5. command() reads the controller for up to 1.5 s: if the output has not changed, the page
     shows "check the wiring in the program". The command is written to the log.

Environment variables (file /etc/home-web.env, see home-web.env.example)
  HOMEWEB_TPY          path of the .tpy file (required)
  HOMEWEB_RULES        rules file (default: rules.json next to the app, else examples/rules.json)
  HOMEWEB_HOUSE        walls and rooms (default: house.json next to the app, else the example)
  HOMEWEB_PLAN_DEFAULT starting plan (default: plan_default.json next to the app, else the example)
  HOMEWEB_ACCOUNTS     accounts file (default /etc/home-web-accounts.json)
  HOMEWEB_LANG         language of the page and of the log: en (default) or fr
  HOMEWEB_TZ           time zone of the log (default UTC), e.g. Europe/Brussels
  HOMEWEB_DEMO=1       simulated controller, to test without hardware (any login works, as admin)
  HOMEWEB_POLL         read interval in seconds (default 1)
  HOMEWEB_PULSE_MS     pulse length in milliseconds (default 100, between 50 and 1000)
  HOMEWEB_TS_LOGINS    Tailscale accounts allowed to open the page, comma separated (optional)
  PLC_NETID, PLC_IP, PLC_PORT, LOCAL_NETID   ADS addresses (required unless HOMEWEB_DEMO=1)
  STATE_DIRECTORY      folder of the plan and the log (set by systemd: /var/lib/home-web)

Intended access: only through "tailscale serve" (HTTPS); the service listens on 127.0.0.1 only.
"""
import base64
import hashlib
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import plan as planmod
from . import rules as rulesmod
from .accounts import Accounts
from .eventlog import TYPES as LOG_TYPES, TZ, UNKNOWN_PREFIX, EventLog
from .i18n import LANG, t
from .passwd import hash_password, verify
from .plc import VERSION_SIGNAL, DemoReader, PlcReader, StuckBit
from .tpy import load_flags, load_io, load_used, load_version

DEMO = os.environ.get("HOMEWEB_DEMO") == "1"
TPY = os.environ.get("HOMEWEB_TPY", "")
POLL = float(os.environ.get("HOMEWEB_POLL", "1"))
PULSE_S = min(max(int(os.environ.get("HOMEWEB_PULSE_MS", "100")), 50), 1000) / 1000
TS_LOGINS = {x.strip().lower() for x in os.environ.get("HOMEWEB_TS_LOGINS", "").split(",") if x.strip()}
STATE_DIR = os.environ.get("STATE_DIRECTORY", "")
STATIC = Path(__file__).parent / "static"

TOKEN_TTL = 12 * 3600        # longest session (the page locks itself well before)
MAX_CMDS_PER_MIN = 40        # beyond, commands are refused for a minute
MAX_DATA_AGE = 5             # seconds: beyond, the state read is too old to send commands
TOGGLE_WAIT = 1.5            # seconds: time for a lamp to change after the pulse
EFFECT_WAIT = 2.0            # seconds: time to see the effect of the other commands

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("home-web")

if not TPY:
    raise SystemExit("HOMEWEB_TPY missing: path of the .tpy file of the controller")

RULES = rulesmod.load()
ACCOUNTS = Accounts()
if not DEMO and not ACCOUNTS.names():
    raise SystemExit("No account: python -m app.accounts add <name> --admin")
LOG = EventLog(STATE_DIR or None)
HOUSE = planmod.load_house()                       # walls and rooms; sets the floors
PLAN = planmod.Plan(STATE_DIR or None)
log.info("Rules: %s ; house: %s ; starting plan: %s", rulesmod.rules_file(), planmod.house_file(),
         planmod.default_plan_file())


# ---------------------------------------------------------------- PLC program (.tpy)
IO = load_io(TPY)                                  # inputs and outputs
FLAGS = load_flags(TPY, RULES.prefix, RULES.extra_reads())   # command and state variables in %M
INPUTS = {name for name, _, _ in IO["in"]}
OUTPUTS = {name for name, _, _ in IO["out"]}
COMMENTS = {name: c for k in ("in", "out") for name, _, c in IO[k]}
VERSION = load_version(TPY, RULES.version_prefix)  # (name, byte, expected value) or None
WIRED = load_used(TPY)                             # variables the program uses (None: no information)
if VERSION:
    log.info("Version check: %s at %%MB%d must be %d", *VERSION)


# ---------------------------------------------------------------- whitelist of commands
COMMANDS = {}             # {exact variable name: Command}: the ONLY variables ever written
TO_CHECK = []             # [(variable, problem)]: shown to admins in the Info tab


def _whitelist(plan):
    """Allowed commands = functions and garage (rules) + lamps, devices, shutters (plan)."""
    from_plan, problems = planmod.commands(plan, RULES, FLAGS, OUTPUTS)
    fixed, ignored = RULES.fixed_commands(FLAGS, planmod.lights_of(plan), planmod.shutter_outputs(plan))
    cmds = {**from_plan, **fixed}
    used = {n.lower() for n in cmds}
    not_wired = []
    if WIRED is not None:                # declared but not wired in the program yet: greyed out
        not_wired = [(n, t("problem.not_wired")) for n in sorted(cmds) if n not in WIRED]
        for n, _ in not_wired:
            del cmds[n]
    unused = [(n, t("problem.unused")) for n in sorted(FLAGS)
              if n.lower() not in used and not RULES.is_state(n) and not RULES.refusal(n, OUTPUTS)]
    return cmds, problems + ignored + not_wired + unused


def _apply_plan(plan):
    global COMMANDS, TO_CHECK
    COMMANDS, TO_CHECK = _whitelist(plan)
    _reader.set_commands({n: FLAGS[n] for n in COMMANDS}, COMMANDS, planmod.lights_of(plan),
                         planmod.shutter_outputs(plan))
    log.info("Plan rev. %s: %d allowed commands", plan["rev"], len(COMMANDS))


# ---------------------------------------------------------------- reading the controller
_state = {"signals": {}, "connected": False, "ts": None, "error": t("state.starting")}
_last_ok = 0.0                                     # monotonic time of the last successful read
_lock = threading.Lock()
_stop = threading.Event()
if DEMO:
    _contacts = {e["sig"] for e in PLAN.data["elements"] if e["t"] == "contact"}
    _reader = DemoReader(IO, FLAGS, RULES, _contacts, VERSION[2] if VERSION else None)
else:
    try:
        _ads = {k: os.environ[k] for k in ("PLC_NETID", "PLC_IP", "LOCAL_NETID")}
    except KeyError as e:
        raise SystemExit(f"{e.args[0]} missing (ADS address of the controller, see home-web.env.example)")
    _reader = PlcReader(IO, FLAGS, {}, netid=_ads["PLC_NETID"], ip=_ads["PLC_IP"],
                        port=int(os.environ.get("PLC_PORT", "800")), local_netid=_ads["LOCAL_NETID"],
                        pulse_s=PULSE_S, version=VERSION[1] if VERSION else None)
_apply_plan(PLAN.copy())
log.info(".tpy file: %d variables in %%M, %d allowed commands", len(FLAGS), len(COMMANDS))


def _refresh():
    """Reads the controller and updates the state in memory."""
    global _last_ok
    signals = _reader.read()
    with _lock:
        _state.update(signals=signals, connected=True, error=None, ts=datetime.now(timezone.utc).isoformat())
        _last_ok = time.monotonic()


def _duration(s):
    s = int(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


# ---------------------------------------------------------------- doors (log)
# Every door and window of the plan, and the garage door: at each read, compared with the
# previous read, "Opened" / "Closed" is written under the name of the element. Limits: an
# opening shorter than the read interval (1 s) may be missed; nothing is written while the
# service is stopped or the controller unreachable (the first read after is the new start).
_doors = {}               # signal → {"open": bool, "since": monotonic time of the opening or None}


def _is_open(e, v):
    if e["t"] == "garage" and RULES.garage and e["sig"] == RULES.garage["sensor"]:
        return v == bool(RULES.garage.get("sensor_open_value", True))
    return v if e["t"] == "garage" else not v          # contacts: 1 = closed


def _track_doors():
    with _lock:
        signals = dict(_state["signals"])
    seen = set()
    for e in [e for e in PLAN.data["elements"] if e["t"] in planmod.TRACKED]:
        v = signals.get(e["sig"])
        if not isinstance(v, bool):
            continue
        seen.add(e["sig"])
        is_open, before, now = _is_open(e, v), _doors.get(e["sig"]), time.monotonic()
        if before is None:                                      # first read: starting point
            _doors[e["sig"]] = {"open": is_open, "since": None}
            continue
        if is_open == before["open"]:
            continue
        if is_open:
            LOG.write("house", t("log.opened"), user=e["name"], sig=e["sig"])
            _doors[e["sig"]] = {"open": True, "since": now}
        else:
            d = before["since"]
            LOG.write("house", t("log.closed"), user=e["name"],
                      res=t("log.open_for", d=_duration(now - d)) if d else "", sig=e["sig"])
            _doors[e["sig"]] = {"open": False, "since": None}
    for s in set(_doors) - seen:                                # no longer tracked (plan edited): forgotten
        del _doors[s]


# ---------------------------------------------------------------- siren (log)
_siren = {"on": None, "since": None, "last": None}


def _track_siren():
    """Writes "Siren triggered" then "Siren stopped" from the siren output (rules: siren).

    Short pauses of the siren do not count: it is only called stopped after pause_s at 0.
    First read (start, reconnection): starting point, no log line.
    """
    if not RULES.siren:
        return
    out, pause, name = RULES.siren["output"], RULES.siren.get("pause_s", 6), RULES.siren["label"]
    with _lock:
        v = _state["signals"].get(out)
    if not isinstance(v, bool):
        return
    now = time.monotonic()
    if _siren["on"] is None:
        _siren.update(on=v, since=None, last=now if v else None)
        return
    if v:
        _siren["last"] = now
        if not _siren["on"]:
            LOG.write("house", t("log.siren_on"), user=name, sig=out)
            _siren.update(on=True, since=now)
    elif _siren["on"] and now - _siren["last"] > pause:
        d = _siren["since"]
        LOG.write("house", t("log.siren_off"), user=name,
                  res=t("log.after", d=_duration(_siren["last"] - d)) if d else "", sig=out)
        _siren.update(on=False, since=None)


def _poller():
    backoff = POLL
    while not _stop.is_set():
        try:
            _refresh()
            backoff = POLL
        except Exception as e:  # controller unreachable: keep the last state, marked offline
            with _lock:
                _state.update(connected=False, error=t("plc.unreachable_type", type=type(e).__name__))
            _doors.clear()                                      # start again from the next read
            _siren["on"] = None
            backoff = min(backoff * 2, 30)
        else:
            try:
                _track_doors()
                _track_siren()
            except Exception:                                   # tracking must never stop the reads
                log.exception("Door tracking")
        for text in ACCOUNTS.take_changes():                   # accounts changed in the console
            LOG.write("account", text, user="console", res="ok")
        _stop.wait(backoff)
    _reader.close()


def ready():
    """Can commands be sent now? Returns (yes/no, explanation)."""
    if not FLAGS:
        return False, t("ready.no_flags", prefix=RULES.prefix)
    if not COMMANDS:
        return False, t("ready.no_commands")
    with _lock:
        connected, age = _state["connected"], time.monotonic() - _last_ok
        version_read = _state["signals"].get(VERSION_SIGNAL)
    if not connected:
        return False, t("plc.unreachable")
    if age > MAX_DATA_AGE:
        return False, t("ready.old_data")
    if _reader.run is False:
        return False, t("ready.not_running")
    if VERSION and version_read != VERSION[2]:
        return False, t("ready.version", name=VERSION[0], value=version_read)
    return True, ""


@asynccontextmanager
async def lifespan(app):
    th = threading.Thread(target=_poller, name="plc-poller", daemon=True)
    th.start()
    yield
    _stop.set()
    th.join(timeout=5)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- authentication
# Against password guessing: 5 failures in 15 min on one account block THAT account for 15 min;
# 20 failures in 15 min, all accounts together, block every login for 15 min.
WINDOW, MAX_PER_ACCOUNT, MAX_TOTAL = 15 * 60, 5, 20
_failures = {}                     # name → deque of times; "*" = all accounts
_blocked_until = {}                # name → end of the block; "*" = everything
_auth_lock = threading.Lock()
_tokens = {}                       # token hash → {"user", "exp", "ts"}
_FAKE_HASH = hash_password(secrets.token_hex(16))   # unknown account: same computing time as a real one
_tok_lock = threading.Lock()


def check_tailscale(request: Request) -> str:
    """If HOMEWEB_TS_LOGINS is set, only these Tailscale accounts open the page (header of tailscale serve)."""
    ts_login = request.headers.get("Tailscale-User-Login", "").lower()
    if TS_LOGINS and ts_login not in TS_LOGINS:
        log.warning("Refused: Tailscale account not allowed (%s)", ts_login or "none")
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    return ts_login


def _demo_account():
    return {"group": "admin", "hash": "", "tailscale": []} if DEMO and not ACCOUNTS.names() else None


def require_session(request: Request) -> dict:
    """Every /api/... route (except login) requires the token received at login.

    The account is read again at each request: an account removed or moved to another group in
    the console takes effect at once. A session linked to a Tailscale account only works from it.
    """
    ts = check_tailscale(request)
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    key = hashlib.sha256(token.encode()).hexdigest()
    with _tok_lock:
        sess = _tokens.get(key)
        if not token or not sess or sess["exp"] < time.time():
            _tokens.pop(key, None)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("auth.required"))
    account = ACCOUNTS.get(sess["user"]) or _demo_account()
    if account is None or (sess["ts"] and sess["ts"] != ts):
        with _tok_lock:
            _tokens.pop(key, None)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("auth.required"))
    return {"user": sess["user"], "group": account["group"], "ts": ts, "key": key}


def require_admin(request: Request, sess: dict = Depends(require_session)) -> dict:
    if sess["group"] != "admin":
        LOG.write("denied", t("log.admin_denied", path=request.url.path), sess["user"], sess["ts"], t("res.refused"))
        raise HTTPException(status.HTTP_403_FORBIDDEN, t("auth.admins_only"))
    return sess


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


def _is_blocked(name, now):
    return now < _blocked_until.get(name, 0) or now < _blocked_until.get("*", 0)


def _failure(name, now):
    for k, maxi in ((name, MAX_PER_ACCOUNT), ("*", MAX_TOTAL)):
        q = _failures.setdefault(k, deque())
        q.append(now)
        while q and now - q[0] > WINDOW:
            q.popleft()
        if len(q) >= maxi:
            _blocked_until[k] = now + WINDOW
            q.clear()
            return True
    return False


@app.post("/api/login")
def login(body: Login, request: Request):
    ts = check_tailscale(request)
    name = body.username.strip().lower()[:40]
    now = time.monotonic()
    with _auth_lock:
        if _is_blocked(name, now):
            LOG.write("login", t("log.login_refused"), name, ts, t("res.blocked"))
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, t("auth.too_many"))
    account = ACCOUNTS.get(name) or _demo_account()
    if _demo_account() is not None:
        ok = True
    else:
        ok = verify(body.password, account["hash"] if account else _FAKE_HASH) and account is not None
    if ok and account.get("tailscale") and ts not in account["tailscale"]:
        LOG.write("login", t("log.login_refused"), name, ts, t("res.wrong_tailscale"))
        time.sleep(1)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("auth.bad_login"))
    if not ok:
        with _auth_lock:
            blocked = _failure(name, now)
        LOG.write("login", t("log.login_refused"), name if account else UNKNOWN_PREFIX + name, ts,
                  t("res.wrong_password") if account else t("res.unknown_account"))
        if blocked:
            LOG.write("login", t("log.logins_blocked"), name, ts, t("res.too_many"))
        time.sleep(1)  # slows down password guessing
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("auth.bad_login"))
    token = secrets.token_urlsafe(32)
    with _tok_lock:
        now_t = time.time()
        for k in [k for k, s in _tokens.items() if s["exp"] < now_t]:
            del _tokens[k]
        _tokens[hashlib.sha256(token.encode()).hexdigest()] = {"user": name, "exp": now_t + TOKEN_TTL, "ts": ts}
    LOG.write("login", t("log.login"), name, ts, "ok")
    return {"token": token, "user": name, "group": account["group"]}


@app.post("/api/logout")
def logout(sess: dict = Depends(require_session)):
    with _tok_lock:
        _tokens.pop(sess.get("key", ""), None)
    LOG.write("login", t("log.logout"), sess["user"], sess["ts"], "ok")
    return {"ok": True}


# ---------------------------------------------------------------- page and state
@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    return resp


_TZ_NAME = getattr(TZ, "key", "UTC")


@app.get("/")
def index(request: Request):
    # The page itself holds no data: the state is only sent after login.
    check_tailscale(request)
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace('<html lang="en">', f'<html lang="{LANG}" data-tz="{_TZ_NAME}">', 1)
    # The page may only run its own script (hash computed at each request, so always right
    # even after index.html changed) and talk to this server.
    hashes = " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()}'"
        for s in re.findall(r"<script>(.*?)</script>", html, re.S))
    csp = (f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; connect-src 'self'; "
           "img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return HTMLResponse(html, headers={"Content-Security-Policy": csp})


@app.get("/api/state")
def state(sess: dict = Depends(require_session)):
    """State read in the controller + list of allowed commands + who is logged in.

    "iface.ok" says whether commands can be sent now; "blocked" lists the commands refused for
    the moment (bit stuck at 1). The page greys out its buttons accordingly.
    """
    ok, msg = ready()
    with _lock:
        data = dict(_state, demo=DEMO)
    data["signals"] = {k: v for k, v in data["signals"].items() if k != VERSION_SIGNAL}
    data["commands"] = {name: {"kind": c.kind, "confirm": c.confirm} for name, c in COMMANDS.items()
                        if not c.admin or sess["group"] == "admin"}
    data["iface"] = {"ok": ok, "msg": msg}
    data["plc_run"] = _reader.run
    data["blocked"] = sorted(_reader.blocked)
    data["me"] = {"user": sess["user"], "group": sess["group"]}
    data["plan_rev"] = PLAN.data["rev"]
    if sess["group"] == "admin":
        data["to_check"] = TO_CHECK
    return JSONResponse(data)


# ---------------------------------------------------------------- commands
class Command(BaseModel):
    id: str = Field(max_length=60)   # name of the command variable (e.g. "HMI_DO_Kitchen_Spots")
    on: bool | None = None           # toggle (lamp, device, alarm...): WANTED state, required
    confirm: bool = False            # the person confirmed (required for some commands)


_cmd_lock = threading.Lock()
_cmd_times = deque()


def _command_text(c, on):
    """Log text: "Switch on: Kitchen spots", "Alarm: turn on"..."""
    if c.kind == "light":
        return t("cmd.light_on" if on else "cmd.light_off", name=c.label)
    if c.kind == "device":
        return t("cmd.dev_on" if on else "cmd.dev_off", name=c.label)
    if c.kind == "toggle":
        return t("cmd.toggle_on" if on else "cmd.toggle_off", name=c.label)
    return c.label or c.var


def _signals(names):
    with _lock:
        sig = _state["signals"]
        return {n: sig.get(n) for n in names}


@app.post("/api/cmd")
def command(body: Command, sess: dict = Depends(require_session)):
    def note(res):
        LOG.write("command", _command_text(c, body.on), sess["user"], sess["ts"], res, variable=body.id)

    # 1. Checks before sending anything
    c = COMMANDS.get(body.id)
    if c is None:
        LOG.write("denied", t("log.not_whitelisted", var=body.id), sess["user"], sess["ts"], t("res.refused"))
        raise HTTPException(status.HTTP_403_FORBIDDEN, t("cmd.not_allowed"))
    ok, msg = ready()
    if not ok:
        note(t("res.refused_because", why=msg))
        raise HTTPException(status.HTTP_409_CONFLICT, msg)
    if body.id in _reader.blocked:
        note(t("res.stuck"))
        raise HTTPException(status.HTTP_409_CONFLICT, t("cmd.blocked"))
    if c.admin and sess["group"] != "admin":
        LOG.write("denied", t("log.admin_command", name=c.label), sess["user"], sess["ts"], t("res.refused"))
        raise HTTPException(status.HTTP_403_FORBIDDEN, t("cmd.admins_only"))
    if c.confirm and not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("cmd.confirm_required"))
    if c.toggle_sig and body.on is None:
        # a toggle inverts the state: without a wanted state, we could not know if the pulse does what is asked
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("cmd.wanted_missing"))

    with _cmd_lock:                                       # one command at a time
        now = time.monotonic()
        while _cmd_times and now - _cmd_times[0] > 60:
            _cmd_times.popleft()
        if len(_cmd_times) >= MAX_CMDS_PER_MIN:
            note(t("res.too_many_cmds"))
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, t("cmd.too_many"))
        # State read just before sending (not the one shown by the page, which may be old)
        try:
            _refresh()
        except Exception:
            note(t("res.unreachable"))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, t("plc.unreachable"))
        before = _signals(c.watch)
        if c.condition:
            # Garage door: "Open" only when closed, "Close" only when open
            sig, wanted, message = c.condition
            v = _signals((sig,))[sig]
            if v is None:
                note(t("res.unknown_state"))
                raise HTTPException(status.HTTP_409_CONFLICT, t("cmd.unknown_state"))
            if bool(v) != wanted:
                note(message)
                return {"ok": True, "note": message}
        if c.toggle_sig:
            # Toggle: the program inverts the state. The pulse is only sent if the current state
            # is the opposite of the wanted one; otherwise it would do the contrary of the request.
            if before.get(c.toggle_sig) is None:
                note(t("res.unknown_state"))
                raise HTTPException(status.HTTP_409_CONFLICT, t("cmd.unknown_state"))
            if bool(before[c.toggle_sig]) == body.on:
                note(t("res.already"))
                return {"ok": True, "note": t("cmd.already")}
        _cmd_times.append(now)

        # 2. Pulse
        try:
            _reader.pulse(body.id)
        except StuckBit:
            note(t("res.stuck"))
            raise HTTPException(status.HTTP_409_CONFLICT, t("cmd.blocked"))
        except Exception as e:
            note(t("res.error", type=type(e).__name__))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, t("plc.unreachable"))

        # 3. Effect: read the controller until something changes (the page sees it at once)
        end = time.monotonic() + (TOGGLE_WAIT if c.strict else EFFECT_WAIT)
        effect = None if not c.watch else False
        while c.watch:
            try:
                _refresh()
            except Exception:
                break
            after = _signals(c.watch)
            if c.toggle_sig:
                effect = after[c.toggle_sig] is not None and bool(after[c.toggle_sig]) != bool(before[c.toggle_sig])
            else:
                effect = any(after[n] is not None and after[n] != before[n] for n in c.watch)
            if effect or time.monotonic() > end:
                break
            time.sleep(0.1)
        note(t("res.sent") if effect is None else "ok" if effect else t("res.no_effect"))

    if c.strict and not effect:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, t("cmd.no_change"))
    return {"ok": True, "effect": effect}


# ---------------------------------------------------------------- plan (read: everyone; edit: admins)
@app.get("/api/plan")
def read_plan(sess: dict = Depends(require_session)):
    return {**PLAN.copy(), "house": HOUSE, "rules": RULES.ui()}


@app.get("/api/variables")
def variables(sess: dict = Depends(require_admin)):
    """Lists offered in edit mode: nothing else can be chosen."""
    return {
        "outputs": [[n, COMMENTS.get(n, "")] for n in sorted(OUTPUTS)],
        "inputs": [[n, COMMENTS.get(n, "")] for n in sorted(INPUTS)],
        "commands": [n for n in sorted(FLAGS) if not RULES.refusal(n, OUTPUTS)],
    }


class ElementEdit(BaseModel):
    rev: int
    element: dict


class ElementRemove(BaseModel):
    rev: int
    id: str = Field(max_length=10)


class ShutterEdit(BaseModel):
    rev: int
    n: str = Field(max_length=1)
    shutter: dict


class Restore(BaseModel):
    rev: int                          # current version (check against simultaneous edits)
    version: int                      # version to put back


_plan_lock = threading.Lock()


def _edit_plan(sess, rev, new, summary=None):
    """Validates, saves (with history), logs and applies a new plan."""
    with _plan_lock:
        old = PLAN.copy()
        try:
            clean = planmod.validate(new, RULES, INPUTS, OUTPUTS, old)
            diff = planmod.differences(old, clean)
            if not diff and summary is None:
                return {"ok": True, "plan": old, "note": t("plan.no_change")}
            saved = PLAN.save(clean, rev, sess["user"], (summary or " / ".join(diff))[:300])
        except planmod.InvalidPlan as e:
            LOG.write("edit", t("log.edit_refused"), sess["user"], sess["ts"], str(e))
            code = status.HTTP_409_CONFLICT if str(e) == t("plan.changed_meanwhile") else status.HTTP_400_BAD_REQUEST
            raise HTTPException(code, str(e))
        for line in ([summary] if summary else []) + diff:
            LOG.write("edit", line, sess["user"], sess["ts"], "ok", rev=saved["rev"])
        with _cmd_lock:
            _apply_plan(saved)
        return {"ok": True, "plan": saved}


@app.post("/api/plan/element")
def edit_element(body: ElementEdit, sess: dict = Depends(require_admin)):
    """Creates (no id: lamp or device) or edits an element."""
    plan = PLAN.copy()
    e = dict(body.element)
    if not e.get("id"):
        nums = [int(x["id"][1:]) for x in plan["elements"]]
        e["id"] = f"e{max(nums, default=0) + 1}"
        plan["elements"].append(e)
    else:
        idx = next((i for i, x in enumerate(plan["elements"]) if x["id"] == e["id"]), None)
        if idx is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, t("plan.no_element"))
        plan["elements"][idx] = e
    return _edit_plan(sess, body.rev, plan)


@app.post("/api/plan/remove")
def remove_element(body: ElementRemove, sess: dict = Depends(require_admin)):
    plan = PLAN.copy()
    plan["elements"] = [x for x in plan["elements"] if x["id"] != body.id]
    return _edit_plan(sess, body.rev, plan)


@app.post("/api/plan/shutter")
def edit_shutter(body: ShutterEdit, sess: dict = Depends(require_admin)):
    plan = PLAN.copy()
    if body.n not in plan["shutters"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("plan.no_shutter"))
    plan["shutters"][body.n] = body.shutter
    return _edit_plan(sess, body.rev, plan)


@app.get("/api/plan/history")
def history(sess: dict = Depends(require_admin)):
    return {"rev": PLAN.data["rev"], "versions": PLAN.history()}


@app.post("/api/plan/restore")
def restore(body: Restore, sess: dict = Depends(require_admin)):
    try:
        old = PLAN.version(body.version)
    except planmod.InvalidPlan as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return _edit_plan(sess, body.rev, old, summary=t("log.restored", rev=body.version))


# ---------------------------------------------------------------- log (admins)
@app.get("/api/log")
def read_log(limit: int = 200, before: str | None = None, user: str | None = None, type: str | None = None,
             sess: dict = Depends(require_admin)):
    limit = min(max(limit, 1), 500)
    if type and type not in LOG_TYPES:
        type = None
    lines, more = LOG.read(limit, before[:40] if before else None, user[:60] if user else None, type)
    accounts, elements = LOG.names()
    return {"lines": lines, "more": more, "accounts": accounts, "elements": _house_order(elements),
            "types": LOG_TYPES}


def _house_order(from_log):
    """"House" list of the log filter: front door, garage door, then the rest in alphabetical order.

    Every door and window of the plan is there, even without an event yet, plus the siren and the
    old names no longer in the plan (element renamed or removed).
    """
    els = [e for e in PLAN.data["elements"] if e["t"] in planmod.TRACKED]
    first = [e["name"] for e in els if e["sig"] == RULES.front_door]
    garage = [e["name"] for e in els if e["t"] == "garage"]
    siren = {RULES.siren["label"]} if RULES.siren and RULES.siren["output"] in OUTPUTS else set()
    rest = ({e["name"] for e in els} | set(from_log) | siren) - set(first) - set(garage)
    key = lambda n: unicodedata.normalize("NFD", n).encode("ascii", "ignore").decode().casefold()
    return first + garage + sorted(rest, key=key)
