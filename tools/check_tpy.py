"""Checks the command variables of the PLC program and produces documentation.

Usage (from the home-web folder, on the server or on any PC with Python 3):

  python3 -m tools.check_tpy check house.tpy
      Checks the .tpy against the rules file and the plan: duplicate addresses, mirror variables
      without a matching output, variables without an address, unknown names, toggles without a
      readable state. Exit code 1 if there is at least one ERROR.

  python3 -m tools.check_tpy table house.tpy > ADDRESSES.md
      Markdown table of every command and state variable: address, output, role in the page.

  python3 -m tools.check_tpy declarations
      VAR_GLOBAL declarations of the functions and states of the rules file, ready to paste
      into TwinCAT (addresses and comments taken from the rules file).

Options: --rules (default: the rules file the server would use), --plan (default: the plan of the
server in /var/lib/home-web, else the starting plan).

"Mirror" rule suggested to the PLC program: the command variable <prefix><output> sits at
%MX(n+98).b when the output sits at %QXn.b. The server does not rely on it (it links by NAME);
this tool only uses it to spot typos.
"""
import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import plan as planmod  # noqa: E402
from app import rules as rulesmod  # noqa: E402
from app.tpy import IG_M_BITS, IG_M_BYTES, load_io, load_used, symbols  # noqa: E402

MIRROR_OFFSET = 98            # %QXn.b  ↔  %MX(n+98).b
SERVER_PLAN = Path("/var/lib/home-web/plan.json")


def load_plan(path=None):
    f = Path(path) if path else SERVER_PLAN if SERVER_PLAN.exists() else planmod.default_plan_file()
    return json.loads(f.read_text(encoding="utf-8")), f


def read_variables(path, rules):
    """Command and state variables of the .tpy: [{"name", "byte", "bit", "comment"}] (byte None = no address)."""
    out, extra = [], {n.lower() for n in rules.extra_reads()}
    for d in symbols(path):
        n = d["Name"]
        if not (n.lower().startswith(rules.prefix.lower()) or n.lower() in extra):
            continue
        g, off = int(d["IGroup"]), int(d["IOffset"])
        byte, bit = divmod(off, 8) if g == IG_M_BITS else (off, 0) if g == IG_M_BYTES else (None, None)
        out.append({"name": n, "byte": byte, "bit": bit, "comment": d.get("Comment", "")})
    return out


def known_commands(plan, rules, readable, outputs):
    from_plan, problems = planmod.commands(plan, rules, readable, outputs)
    fixed, ignored = rules.fixed_commands(readable, planmod.lights_of(plan), planmod.shutter_outputs(plan))
    return {**from_plan, **fixed}, dict(problems + ignored)


def check(variables, io, plan, rules):
    outputs = {n for n, _, _ in io["out"]}
    inputs = {n for n, _, _ in io["in"]}
    q_bit = {n: b for n, b, _ in io["out"]}
    by_qbit = {b: n for n, b, _ in io["out"]}
    errors, warnings, infos = [], [], []
    adr = lambda v: f"%MX{v['byte']}.{v['bit']}"
    p = rules.prefix

    seen = {}
    for v in variables:
        if v["byte"] is None:
            continue
        k = (v["byte"], v["bit"])
        if k in seen:
            errors.append(f"{adr(v)} used twice: {seen[k]} and {v['name']}")
        seen[k] = v["name"]

    readable = [v["name"] for v in variables if v["byte"] is not None]
    cmds, problems = known_commands(plan, rules, readable, outputs)
    fixed = rules.reserved()
    version = re.compile(re.escape(rules.version_prefix) + r"\d+", re.IGNORECASE)
    for v in variables:
        name = v["name"]
        if version.fullmatch(name):
            infos.append(f"Version check active: {name} (byte %MB{v['byte']})")
            continue
        if v["byte"] is None:
            warnings.append(f"{name} has no address (AT %MX...): the server cannot reach it")
            continue
        out = next((o for o in outputs if p.lower() + o.lower() == name.lower()), None)
        if out:
            if q_bit[out] != (v["byte"] - MIRROR_OFFSET) * 8 + v["bit"]:
                infos.append(f"{adr(v)} {name}: address outside the mirror rule (fine: the server links by name)")
        elif not rules.is_state(name) and name.lower() not in fixed:
            guess = by_qbit.get((v["byte"] - MIRROR_OFFSET) * 8 + v["bit"])
            close = difflib.get_close_matches(name, [p + o for o in outputs] + [f.var for f in rules.functions], n=1, cutoff=0.7)
            hint = f" — the mirror rule gives {p}{guess} here" if guess else f" — did you mean {close[0]}?" if close else ""
            warnings.append(f"{adr(v)} {name}: unknown to the server{hint}")
        pr = problems.get(name)
        if pr and name.lower() in fixed:
            warnings.append(f"{adr(v)} {name}: {pr}")

    exact = {n.lower() for n in readable}
    for e in plan["elements"]:
        if e["sig"] not in outputs | inputs:
            warnings.append(f"Plan: \"{e['name']}\" reads {e['sig']}, missing in the program")
        if e.get("cmd") and e["cmd"].lower() not in exact:
            infos.append(f"Plan: \"{e['name']}\" is commanded by {e['cmd']}, not in the program yet (greyed out)")
    used = {n.lower() for n in cmds}
    free = [n for n in readable if n.lower() not in used and not rules.is_state(n) and not rules.refusal(n, outputs)]
    if free:
        infos.append("Command variables the plan does not use (link them in edit mode if needed): " + ", ".join(sorted(free)))
    missing = [f.var for f in rules.functions if f.var.lower() not in exact]
    if missing:
        infos.append("Functions not in the program yet (greyed out): " + ", ".join(missing))
    return errors, warnings, infos


def table(variables, io, source, plan, rules):
    outputs = {n for n, _, _ in io["out"]}
    q = {n: b for n, b, _ in io["out"]}
    readable = [v["name"] for v in variables if v["byte"] is not None]
    cmds, problems = known_commands(plan, rules, readable, outputs)
    wired = load_used(source)
    lines = [f"# Addresses of the {rules.prefix} variables", "",
             f"Generated by `python3 -m tools.check_tpy table` from `{Path(source).name}`. Do not edit by hand.", "",
             "| Address | Variable | Output | Comment (program) | In the page | Confirmation |", "|---|---|---|---|---|---|"]
    for v in sorted(variables, key=lambda v: (v["byte"] if v["byte"] is not None else 9999, v["bit"] or 0)):
        a = f"%MX{v['byte']}.{v['bit']}" if v["byte"] is not None else "*(none)*"
        out = next((o for o in outputs if rules.prefix.lower() + o.lower() == v["name"].lower()), "")
        qo = f"`{out}` %QX{q[out] // 8}.{q[out] % 8}" if out else ""
        c = next((x for k, x in cmds.items() if k.lower() == v["name"].lower()), None)
        if c:
            role, conf = f"{c.kind}: {c.label}", "yes" if c.confirm else ""
        elif rules.is_state(v["name"]):
            role, conf = "shown (read only)", ""
        else:
            role, conf = f"*refused* ({rules.refusal(v['name'], outputs) or problems.get(v['name']) or 'not used by the plan'})", ""
        if wired is not None and c and v["name"] not in wired:
            role += " — *not wired in the program yet*"
        lines.append(f"| {a} | `{v['name']}` | {qo} | {v['comment'].replace('|', '/')} | {role} | {conf} |")
    return "\n".join(lines) + "\n"


def declarations(rules):
    """Declarations to paste into TwinCAT (ASCII only, as the TwinCAT 2 editor expects)."""
    out = ["(* Command variables for home-web: the server sends a pulse (100 ms TRUE, then FALSE).",
           "   Add each one with OR to the condition of the matching button (rising edge).",
           "   The addresses are a suggestion: only the NAMES matter. Each address only once. *)", "VAR_GLOBAL"]
    for f in rules.functions:
        out.append(f"    {f.var:<26} AT {f.address or '%MX?.?':<9}: BOOL; (* {f.plc_comment or f.label} *)")
    g = rules.garage
    if g:
        for k in ("open", "close"):
            out.append(f"    {g[k]:<26} AT {(g.get('addresses') or {}).get(k, '%MX?.?'):<9}: BOOL; (* garage door: {k} *)")
    out += [f"    {rules.version_prefix + '1':<26} AT %MB103  : BYTE := 1; (* version check: +1 at each address change *)",
            "END_VAR", "",
            "(* States: copies of internal variables, read only by the server, e.g. HMI_Alarm_Active := AlarmBlock.Active; *)",
            "VAR_GLOBAL"]
    for name, s in rules.states.items():
        out.append(f"    {name:<26} AT {s.get('address', '%MX?.?'):<9}: BOOL; (* {s.get('plc_comment', '')} *)")
    out.append("END_VAR")
    return "\n".join(out).encode("ascii", "replace").decode() + "\n"


def main():
    ap = argparse.ArgumentParser(description="Command variables of the PLC program")
    sub = ap.add_subparsers(dest="action", required=True)
    for name in ("check", "table"):
        s = sub.add_parser(name)
        s.add_argument("tpy")
        s.add_argument("--plan")
        s.add_argument("--rules")
    d = sub.add_parser("declarations")
    d.add_argument("--rules")
    a = ap.parse_args()
    rules = rulesmod.load(a.rules)
    if a.action == "declarations":
        sys.stdout.write(declarations(rules))
        return 0
    planmod.load_house()
    io, variables = load_io(a.tpy), read_variables(a.tpy, rules)
    plan, fplan = load_plan(a.plan)
    if a.action == "table":
        sys.stdout.write(table(variables, io, a.tpy, plan, rules))
        return 0
    errors, warnings, infos = check(variables, io, plan, rules)
    wired = load_used(a.tpy)
    if wired is not None:
        waiting = sorted(v["name"] for v in variables if v["byte"] is not None and v["name"] not in wired
                         and not v["name"].lower().startswith(rules.version_prefix.lower()))
        if waiting:
            infos.append("Not wired in the program yet (greyed out until the next .tpy): " + ", ".join(waiting))
    print(f"{len(variables)} variables read from {a.tpy}; plan: {fplan}\n")
    for title, lst in (("ERROR", errors), ("WARNING", warnings), ("INFO", infos)):
        for x in lst:
            print(f"{title:8} {x}")
    if not (errors or warnings):
        print("No problem found.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
