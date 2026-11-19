# How it works

How home-web works, from the page down to the relay of the controller. It is written for the admin of the server as well as for the PLC programmer who may one day read the code.

## 1. Overview

```
Phone / PC ──HTTPS (Tailscale)──▶ tailscale serve ──▶ home-web (127.0.0.1:8080) ──ADS──▶ BX9000
```

- **Reading**, every second, by a background thread: input image, output image, `%M` area of the command and state variables, RUN/STOP state. The last state is kept in memory and the page reads it every 2 seconds (`/api/state`).
- **Command**: the page sends the name of a command variable (`/api/cmd`). After several checks, the server sends **one pulse**: the bit goes to 1 for 100 ms, then the server resets it.
- The PLC program adds each command variable (with OR) to the condition of the matching button. The pulse does exactly what a button press does.

Why `%MX` variables: the BX9000 only opens the inputs, the outputs and the flags `%M` to the network. Other variables of the program are refused (ADS error 0x701).

## 2. Kinds of variables

All names below come from the example house; a real installation sets its own prefix and names in its rules file.

| Kind | Example | Role | The server… |
|---|---|---|---|
| Output command | `HMI_DO_Kitchen_Spots` | pulse = press on the button of output `DO_Kitchen_Spots` | writes (pulse) |
| Function | `HMI_AllLightsOff`, `HMI_ShuttersUp` | pulse = a scene of the program | writes (pulse) |
| Toggle | `HMI_Alarm_Toggle` | pulse = the block inverts the state, like a lamp | writes (pulse), after reading the state |
| Garage door | `HMI_Garage_Open`, `HMI_Garage_Close` | open / close | writes (pulse), after reading the door contact |
| State | `HMI_Alarm_Active`, `HMI_FireAlarm` | shown by the page | reads only |

The link is made **by name**: `HMI_DO_Kitchen_Spots` commands the output `DO_Kitchen_Spots`, whatever its address. Addresses come from the `.tpy` file.

**Toggles and state read before sending.** For a lamp, a device or a toggle, the block of the program looks at the state and decides to switch on or off. The page sends the *wanted* state ("turn on"). The server reads the controller just before and only sends the pulse if the state is not reached already; otherwise it answers "already in this state". The server never decides instead of the block: it only prevents an outdated screen (alarm turned on meanwhile from a keypad) from doing the opposite of the request. A toggle whose state is not readable is refused.

## 3. Path of a command

Example: switch on the kitchen spots from the Lights tab.

1. **Page** (`app/static/index.html`, `toggleLight`): sends `{"id": "HMI_DO_Kitchen_Spots", "on": true}` with the session token.
2. **Server** (`app/main.py`, `command`) checks, in this order:
   - the session (valid token);
   - the variable is in the **whitelist**, built from the `.tpy`, the plan (`app/plan.py`) and the rules (`app/rules.py`), and rebuilt at each plan edit;
   - the controller is **ready** (`ready`): reachable, data younger than 5 s, in RUN, right program version;
   - the command is not **blocked**;
   - the **confirmation** is there if the command requires one;
   - one command at a time, 40 per minute at most;
   - for a toggle (lamp, device, alarm, holiday): the controller is read again, and if the wanted state is already reached, nothing is sent.
3. **Pulse** (`app/plc.py`, `PlcReader.pulse`): the bit must be 0; set it to 1; wait 100 ms; reset to 0 (3 tries, reconnecting if needed); read it again, it must be 0.
4. **Controller**: the program sees the rising edge and toggles `DO_Kitchen_Spots`.
5. **Check** (`command`): the server reads the controller for 1.5 s at most. If the output has not changed, the page says "Pulse sent, but the output did not change". For other commands it watches 2 s for any change (shutter motor, alarm state…), without making it an error.
6. **Log**: one line per command in the log (Logs tab, `/var/lib/home-web/log-YYYY-MM.jsonl`) and in `journalctl -u home-web`: date, account, Tailscale account, text ("Switch on: Kitchen spots"), result.

## 4. Safeguards

| Risk | Protection |
|---|---|
| Writing anything but a command | Whitelist: only the variables used by a plan element or by a function of the rules can be written. Never outputs, never states, never forbidden outputs. A command variable no element uses is never written. |
| Admin mistake or stolen admin account | The fixed rules stay in the rules file (`rules.json`), which the page cannot change: forbidden outputs, mandatory confirmations, functions, garage and states. The server checks every plan edit (`plan.validate`), refuses a variable used twice, keeps the history (roll back possible) and logs everything. Only lamps and devices can be added or removed. |
| User trying an admin action | The role is checked by the server at each request (`require_admin`), not only hidden in the page; the attempt is logged. Admin-only functions are refused to users. |
| Dangerous command | Confirmation asked by the page **and** required by the server: toggles marked `confirm`, garage door, devices of `confirm_outputs` (heater, pumps…). |
| Toggle doing the opposite (outdated screen, two people at once) | State read just before sending; pulse only if the wanted state is not reached. Toggle refused if its state is not readable. |
| Opening the garage when it is already open (or closing it when closed) | "Open" is only sent when the contact says closed, "Close" only when it says open; contact read just before; unknown → nothing sent. Confirmation required. |
| Button that does nothing (variable declared but not wired yet) | The `.tpy` marks the variables the program uses (`TaskPrio` attribute); the others are removed from the whitelist and listed in Info. If the file has no such mark, nothing is filtered. |
| Bit stuck at 1 (server stopped in the middle of a pulse, network cut) | At each (re)connection, every command bit is reset to 0. At each read, a command bit found at 1 is reset and logged. If it is still 1 at the next read, the command is **blocked** (something else writes into it). |
| Controller stopped | ADS state read at each read: no command if the controller is not in RUN. If the controller cannot report it, the check is skipped and logged. |
| `.tpy` no longer matching the program (addresses moved) | Optional: `HMI_Version_<n> AT %MB103 : BYTE := <n>;`. The server reads the byte and compares it with the `<n>` of the `.tpy`. If they differ, no command. Without this variable, the rule is to **never move an existing address**. |
| Too many commands | One at a time, 40 per minute at most. |
| Access to the page | HTTPS through Tailscale only, password at each opening, block after 5 failed attempts per account (20 in total), security headers. See `INSTALL.md`. |

## 5. Rules file

`rules.json` (example: `examples/rules.json`) holds what an admin must not be able to change from the page.

| Key | Content |
|---|---|
| `command_prefix` | prefix of the variables the server may write (`HMI_`). The command of output `X` is `<prefix>X`. |
| `version_prefix` | optional version check variable (`HMI_Version_`). |
| `forbidden_outputs` | `{output: reason}`: never controlled (network power, siren, doorbell…). |
| `confirm_outputs` | `{output: description}`: always ask for a confirmation. |
| `functions` | fixed commands of the Actions tab, see below. |
| `garage` | optional: `open`, `close` (commands), `sensor` (contact), `sensor_open_value` (value when open), `motor` (output shown). |
| `siren` | optional: `output`, `pause_s` (pauses shorter than this do not end the alarm), `label` (name in the log). |
| `alerts` | `{state variable: text}`: red banner when the variable is 1. |
| `states` | read-only variables (copies of internal states made by the program). |
| `state_prefixes` | optional: every variable starting with one of these is read-only. |
| `front_door` | optional: input listed first in the log filter (and opened now and then in demo mode). |

A function has a `var`, a `group` and a `label`, and optionally:

- `toggle: true` with `state: [...]` (possible state variables, the first readable one is used), `on` / `off` texts and `tile` (text in the Alarm tile when active);
- `confirm: true`, `admin: true` (admins only);
- `effect`: `"lights_off"`, `{"lights_on": [outputs]}` or `{"shutters": "up" | "down", "only": ["1", "2"]}`. The server reads these signals after the pulse to show the effect at once; shutter functions are shown with the shutters; the demo mode simulates them;
- `address`, `plc_comment`: used by `tools/check_tpy.py declarations`.

Texts are a string, or `{"en": "...", "fr": "..."}`.

## 6. Accounts, roles and log

- **Accounts**: file `/etc/home-web-accounts.json`, managed in the console with `python -m app.accounts`. Two groups: `user` (see and control) and `admin` (also edit mode and Logs tab). The server reloads the file when it changes; each request checks again that the account still exists and its group.
- **Tailscale link** (per account, optional): `tailscale serve` adds the `Tailscale-User-Login` header with the person's Tailscale account, even for a shared machine. If an account is linked, the login is only accepted from that Tailscale account, and the session is tied to it.
- **Login**: 5 failures in 15 min block the account 15 min; 20 failures across accounts block everything 15 min. Same response time for an unknown account and a wrong password.
- **Log** (`app/eventlog.py`): one JSON file per month, deleted after 12 months. Types: login, command, edit (plan), account (change made in the console), denied (forbidden attempt), house (each door, window and the garage door opened or closed, under the name of the element; siren triggered or stopped).

## 7. The plan and edit mode

- Walls and rooms are in `house.json` (fixed), sent to the page with the plan. The elements placed on them are in the plan, `/var/lib/home-web/plan.json` (format described in `app/plan.py`). At first start, it is `plan_default.json`.
- Each element has: a type, a name, a floor, a position, a **state read** (`sig`: output or input) and, for lamps and devices, a **command** (`cmd`). A device also has its kind (`kind`: socket, fan, heater, pump, other) and, if chosen, its icon (`icon`). Each shutter has a name, its ▲ ▼ commands and its motor outputs.
- The link between a lamp and its variable is data, not a naming rule: if a variable is misnamed in the program, an admin can link it by hand.
- Each save: full validation (`plan.validate`), previous version kept in `plan-history/` (100 versions), whitelist rebuilt, one log line per change. If two admins edit at the same time, the second gets "The plan was changed meanwhile" (version number `rev`).

## 8. Adding a command

1. **In the program**: declare `<prefix>… AT %MX… : BOOL;` at a free address, add it with OR to the condition, build, download, send the `.tpy`.
2. **On the server**: replace the `.tpy`, run `python -m tools.check_tpy check house.tpy`, then `home-web-update`.
3. **A new lamp or device**: edit mode → "Add a device". For an existing lamp linked to the wrong variable: touch it in edit mode and choose the right one. An output that must never be controlled goes to `forbidden_outputs`; one that always needs a confirmation to `confirm_outputs`.
4. **A new function**: add it to `functions` in the rules file. For a toggle, give its state and add the state to `states`.

## 9. Testing without a controller

```bash
HOMEWEB_DEMO=1 HOMEWEB_TPY=examples/house.tpy uvicorn app.main:app --port 8081
```

The controller is then simulated by `DemoReader` (`app/plc.py`): lamps toggle, shutters run for a few seconds, the garage door opens and closes, functions do what their `effect` says. Every check of the server stays active.
