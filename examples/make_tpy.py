"""Builds examples/house.tpy: a fake TwinCAT symbol file for the example house.

The real .tpy comes from the TwinCAT project of the controller ("Project → Rebuild all"). This
one only serves the demo mode and the screenshots:

    python3 examples/make_tpy.py            # rewrites examples/house.tpy from plan.json and rules.json

It follows what a real one contains: inputs (0xF021) and outputs (0xF031) as bits, one command
variable HMI_<output> per output at the "mirror" address (%QXn.b → %MX(n+98).b), the functions
and states of the rules file at the addresses they suggest, the version check at %MB103, and
the TaskPrio attribute on the variables the program uses. Two variables are deliberately "not
wired yet" (no TaskPrio) to show how the page greys them out.
"""
import json
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
IG_IN, IG_OUT, IG_MB, IG_MX = 0xF021, 0xF031, 0x4020, 0x4021
NOT_WIRED = {"HMI_DO_Workshop", "HMI_ShuttersUp_Living"}         # declared, not in the blocks yet
EXTRA_OUTPUTS = {"DO_Siren": "Alarm siren", "DO_Doorbell": "Doorbell", "DO_Garage_Motor": "Garage door motor",
                 "DO_Garage_Button": "Garage door push button"}


def symbol(name, ig, offset, comment="", type_="BOOL", size=1, used=True):
    attr = ' TaskPrio="1"' if used else ""
    com = f"\n\t\t\t<Comment><![CDATA[{comment}]]></Comment>" if comment else ""
    return (f"\t\t<Symbol>\n\t\t\t<Name{attr}>.{escape(name)}</Name>\n\t\t\t<Type>{type_}</Type>{com}\n"
            f"\t\t\t<IGroup>{ig}</IGroup>\n\t\t\t<IOffset>{offset}</IOffset>\n\t\t\t<BitSize>{size}</BitSize>\n\t\t</Symbol>\n")


def bit_of(address):
    byte, bit = (int(x) for x in address.removeprefix("%MX").split("."))
    return byte * 8 + bit


def main():
    plan = json.loads((HERE / "plan.json").read_text(encoding="utf-8"))
    rules = json.loads((HERE / "rules.json").read_text(encoding="utf-8"))
    prefix = rules["command_prefix"]
    names = {e["sig"]: e["name"] for e in plan["elements"]}
    for n, s in plan["shutters"].items():
        names[s["out_up"]] = f"Shutter {n} up ({s['name']})"
        names[s["out_down"]] = f"Shutter {n} down ({s['name']})"
    names.update(EXTRA_OUTPUTS)
    inputs = sorted(s for s in names if s.startswith("DI_"))
    outputs = sorted(s for s in names if s.startswith("DO_"))
    out = []
    for i, s in enumerate(inputs):                              # inputs: %IX0.0, %IX0.1...
        out.append(symbol(s, IG_IN, i, names[s]))
    for i, s in enumerate(outputs):                             # outputs: %QX6.0, %QX6.1... (bit 48 +)
        out.append(symbol(s, IG_OUT, 48 + i, names[s]))
        cmd = prefix + s                                        # mirror: %MX(n+98).b = bit + 784
        out.append(symbol(cmd, IG_MX, 48 + i + 784, "HMI: " + names[s], used=cmd not in NOT_WIRED))
    for f in rules["functions"]:
        out.append(symbol(f["var"], IG_MX, bit_of(f["address"]), f.get("plc_comment", ""),
                          used=f["var"] not in NOT_WIRED))
    g = rules.get("garage")
    if g:
        out.append(symbol(g["open"], IG_MX, bit_of(g["addresses"]["open"]), "Open the garage door"))
        out.append(symbol(g["close"], IG_MX, bit_of(g["addresses"]["close"]), "Close the garage door"))
    for name, s in rules["states"].items():
        out.append(symbol(name, IG_MX, bit_of(s["address"]), s.get("plc_comment", "")))
    out.append(symbol(rules["version_prefix"] + "1", IG_MB, 103, "Version check", type_="BYTE", size=8, used=False))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<PlcProjectInfo>\n\t<Symbols>\n'
           + "".join(out) + "\t</Symbols>\n</PlcProjectInfo>\n")
    (HERE / "house.tpy").write_text(xml, encoding="utf-8")
    print(f"examples/house.tpy: {len(inputs)} inputs, {len(outputs)} outputs, {len(out)} symbols")


if __name__ == "__main__":
    main()
