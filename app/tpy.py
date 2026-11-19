"""Reading the TwinCAT 2 symbol file (.tpy).

TwinCAT PLC Control writes this file at each build ("Project → Rebuild all"), next to the
project. It holds, for each variable, its name, type and address.

The BX9000 only opens three areas over ADS (a limit of the controller):
  - the input image   : index group 0xF021 (bits), bit number in IOffset
  - the output image  : index group 0xF031 (bits), bit number in IOffset
  - the flags %M      : index group 0x4021 (bits), bit number = byte × 8 + bit
                        (a variable "AT %MX104.1" has IOffset 104 × 8 + 1 = 833)
                        or 0x4020 (bytes) for a variable declared "AT %MB...".
The other variables of the program (area 0x4040) cannot be reached over the network.
"""
import re
import xml.etree.ElementTree as ET

IG_IN_BITS = 0xF021
IG_OUT_BITS = 0xF031
IG_M_BYTES = 0x4020
IG_M_BITS = 0x4021


def symbols(path):
    """Yields the symbols of the .tpy: {"Name", "Type", "IGroup", "IOffset", "Comment"...}.

    "_used": True if TwinCAT put the TaskPrio attribute on the name, i.e. a task of the program
    uses the variable (a variable that is only declared does not have it).
    """
    root = ET.parse(path).getroot()
    for sym in root.find("Symbols").findall("Symbol"):
        d = {c.tag: (c.text or "").strip() for c in sym}
        name = sym.find("Name")
        d["_used"] = name is not None and "TaskPrio" in name.attrib
        if d.get("IGroup") and d.get("IOffset"):
            d["Name"] = d["Name"].lstrip(".")          # global variables start with "."
            yield d


def load_io(path):
    """Inputs and outputs: {"in": [(name, bit, comment), ...], "out": [...]}, sorted by bit."""
    io = {"in": [], "out": []}
    for d in symbols(path):
        if d.get("Type") != "BOOL":
            continue
        group = int(d["IGroup"])
        entry = (d["Name"], int(d["IOffset"]), d.get("Comment", ""))
        if group == IG_IN_BITS:
            io["in"].append(entry)
        elif group == IG_OUT_BITS:
            io["out"].append(entry)
    for k in io:
        io[k].sort(key=lambda e: e[1])
    if not io["in"] or not io["out"]:
        raise ValueError(f"No BOOL input/output found in {path}")
    return io


def load_flags(path, prefix, extra=()):
    """BOOL variables placed in %M whose name starts with `prefix`, plus `extra`: {name: bit number}.

    `extra`: other names to read if they are in %M (states named without the prefix). Case does
    not matter (TwinCAT ignores it).

    The bit number is the one given to the controller to read or write (index group 0x4021).
    Example: HMI_DO_Shutter1_Up AT %MX104.0 → 832. A variable without an address
    ("HMI_X : BOOL;" without AT) is not listed: it stays in the internal area, out of reach.
    """
    flags = {}
    prefix, extra = prefix.lower(), {n.lower() for n in extra}
    for d in symbols(path):
        name = d["Name"]
        if not (name.lower().startswith(prefix) or name.lower() in extra) or d.get("Type") != "BOOL":
            continue
        group = int(d["IGroup"])
        if group == IG_M_BITS:
            flags[name] = int(d["IOffset"])
        elif group == IG_M_BYTES:
            flags[name] = int(d["IOffset"]) * 8           # BOOL declared on a whole byte (AT %MB...)
    return flags


def load_used(path):
    """Names of the variables the program uses (TaskPrio attribute), or None.

    Greys out commands that are declared in the program but not wired into its blocks yet: a pulse
    would do nothing. None if the file carries no such mark (other TwinCAT version): nothing is
    filtered then.
    """
    names = {d["Name"] for d in symbols(path) if d["_used"]}
    return names or None


def load_version(path, prefix):
    """Optional version check: variable "<prefix><n> AT %MB... : BYTE := <n>;".

    The program has nothing to do: the initial value <n> is loaded when the controller starts.
    The server reads this byte and compares it with the <n> of the name in the .tpy. If they
    differ, the program in the controller is not the one of the .tpy: addresses may have moved,
    so no command is sent.
    Returns (name, byte number, expected value) or None if the variable does not exist.
    """
    pattern = re.compile(re.escape(prefix) + r"(\d+)", re.IGNORECASE)
    for d in symbols(path):
        m = pattern.fullmatch(d["Name"])
        if m and int(d["IGroup"]) == IG_M_BYTES and d.get("Type") in ("BYTE", "USINT", "SINT"):
            return d["Name"], int(d["IOffset"]), int(m.group(1))
    return None
