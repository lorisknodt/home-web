# Guide for the PLC program

What the web page needs from the TwinCAT program of the BX9000. Names are those of the example house (prefix `HMI_`); a real installation chooses its own in its rules file.

## 1. The principle

```
Phone / PC ──HTTPS──▶ server ──ADS──▶ BX9000
                      reads every second       program reacts
                      sends pulses on %MX      like to a button
```

- Over the network, the BX9000 only allows three areas: **read the inputs**, **read the outputs**, **read and write the flags %M**. Other variables are out of reach (ADS error 0x701 "service not supported").
- So each command gets its own flag variable with a fixed address (`AT %MX…`).
- The server **only sends pulses**: the bit goes **TRUE for 100 ms**, then the server sets it back to FALSE itself. Nothing to reset in the program.
- The program reacts to the **rising edge**, like to a button press.
- The server **never** writes into an output or any other variable.

## 2. Variables to declare

| Kind | Example | What to do in the program |
|---|---|---|
| Output command | `HMI_DO_Kitchen_Spots AT %MX106.2 : BOOL;` | add it with OR to the button of `DO_Kitchen_Spots` |
| Function | `HMI_AllLightsOff AT %MX115.0 : BOOL;` | add it with OR to the condition of the scene |
| Toggle | `HMI_Alarm_Toggle AT %MX114.0 : BOOL;` | add it with OR to the on/off block: the block looks at the state and inverts it |
| State | `HMI_Alarm_Active AT %MX118.0 : BOOL;` | one copy line in a part that runs every cycle: `HMI_Alarm_Active := AlarmBlock.Active;` |

Suggested "mirror" address for output commands: `%QXn.b` → `%MX(n+98).b`. It is only a convention; the server links by **name** and reads the real addresses in the `.tpy`.

`python -m tools.check_tpy declarations` prints the declarations of the functions and states of the rules file, ready to paste.

## 3. Toggles and states

A toggle (alarm, holiday mode…) works like a lamp: one variable, the pulse inverts the state. The server needs to **read** the state to know whether to send: without a readable state variable, the button stays disabled. That is why the program copies the internal state into a `%MX` variable each cycle.

## 4. Version check (optional, advised)

```
HMI_Version_1 AT %MB103 : BYTE := 1;
```

No program line needed: the initial value is loaded at start. At **each change of existing addresses**, increase the number in the name AND the value (`HMI_Version_2 … := 2`). If the program in the controller and the `.tpy` on the server do not match, the server sends nothing.

## 5. After a change

1. Build (Project → Rebuild all) and download the program.
2. Send the new `.tpy` (next to the project) to the server admin.
3. On the server: `python -m tools.check_tpy check house.tpy`, then `home-web-update`.

A variable that is declared but not used yet by the program (no `TaskPrio` attribute in the `.tpy`) stays greyed out in the page and is listed in the Info tab; it becomes active with the next `.tpy` once it is wired.

## 6. Where to look in the code

| Topic | File |
|---|---|
| Pulse, reset to 0, stuck bits | `app/plc.py` (`PlcReader.pulse`, `_watch`) |
| Which variables may be written | `app/rules.py` (`refusal`, `fixed_commands`), `app/plan.py` (`commands`) |
| Checks before a command | `app/main.py` (`command`, `ready`) |
| Reading the `.tpy` | `app/tpy.py` |
