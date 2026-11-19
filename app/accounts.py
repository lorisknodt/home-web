"""Login accounts of the page: file + console tool.

Accounts are created ONLY here, in the container console (no signup form on the page).

    cd /opt/home-web
    /opt/home-web-venv/bin/python -m app.accounts list
    /opt/home-web-venv/bin/python -m app.accounts add sam --user
    /opt/home-web-venv/bin/python -m app.accounts add alex --admin --tailscale alex@example.com
    /opt/home-web-venv/bin/python -m app.accounts passwd sam
    /opt/home-web-venv/bin/python -m app.accounts group sam admin
    /opt/home-web-venv/bin/python -m app.accounts tailscale alex alex@example.com   (no address: removes the link)
    /opt/home-web-venv/bin/python -m app.accounts remove sam

The server reloads the file by itself when it changes: no restart needed.

File: /etc/home-web-accounts.json (HOMEWEB_ACCOUNTS), owner root, group homeweb, mode 640.
    {"alex": {"group": "admin", "hash": "scrypt:...", "tailscale": ["alex@example.com"]},
     "sam":  {"group": "user",  "hash": "scrypt:...", "tailscale": []}}

  group      "user": see the state and send every command
             "admin": also edit the plan (lamps, variables) and read the log
  hash       scrypt hash of the password (the password itself is never stored)
  tailscale  optional: Tailscale accounts allowed for this account. If set, the login is only
             accepted from these Tailscale accounts (header added by "tailscale serve").
"""
import argparse
import getpass
import grp
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

from .passwd import hash_password

PATH = Path(os.environ.get("HOMEWEB_ACCOUNTS", "/etc/home-web-accounts.json"))
GROUPS = ("user", "admin")
VALID_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
SERVICE_GROUP = "homeweb"


# ---------------------------------------------------------------- reading (server)
class Accounts:
    """Accounts read from the file, reloaded automatically when it changes."""

    def __init__(self, path=PATH):
        self.path = Path(path)
        self._mtime = None
        self._loaded = False           # first load: nothing to report to the log
        self._data = {}
        self._lock = threading.Lock()
        self.changes = []              # differences seen at the last reload

    def _reload(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if self._loaded and mtime == self._mtime:
            return
        before = self._data
        data = {} if mtime is None else json.loads(self.path.read_text(encoding="utf-8"))
        if self._loaded:
            self.changes += _differences(before, data)
        self._data, self._mtime, self._loaded = data, mtime, True

    def get(self, name):
        """Account {group, hash, tailscale} or None. Names are lowercase."""
        with self._lock:
            self._reload()
            c = self._data.get(name.strip().lower())
            return dict(c) if c else None

    def names(self):
        with self._lock:
            self._reload()
            return sorted(self._data)

    def take_changes(self):
        """Returns then forgets the differences seen at the last reload (for the log)."""
        with self._lock:
            self._reload()
            c, self.changes = self.changes, []
            return c


def _differences(before, after):
    from .i18n import t
    out = []
    for n in sorted(set(after) - set(before)):
        out.append(t("account.added", name=n, group=after[n].get("group")))
    for n in sorted(set(before) - set(after)):
        out.append(t("account.removed", name=n))
    for n in sorted(set(before) & set(after)):
        a, b = before[n], after[n]
        if a.get("group") != b.get("group"):
            out.append(t("account.group", name=n, a=a.get("group"), b=b.get("group")))
        if a.get("hash") != b.get("hash"):
            out.append(t("account.passwd", name=n))
        if a.get("tailscale") != b.get("tailscale"):
            out.append(t("account.tailscale", name=n))
    return out


# ---------------------------------------------------------------- console tool
def _read():
    if not PATH.exists():
        return {}
    return json.loads(PATH.read_text(encoding="utf-8"))


def _write(data):
    """Atomic write (temporary file, then rename), mode 640 root:homeweb."""
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=PATH.parent, prefix=".accounts-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o640)
    try:
        os.chown(tmp, 0, grp.getgrnam(SERVICE_GROUP).gr_gid)
    except (KeyError, PermissionError):
        print(f"Warning: group '{SERVICE_GROUP}' not found or not enough rights, fix the owner by hand.")
    os.replace(tmp, PATH)


def _password():
    pw = getpass.getpass("Password (at least 12 characters): ")
    if len(pw) < 12:
        raise SystemExit("Choose at least 12 characters.")
    if pw != getpass.getpass("Once more: "):
        raise SystemExit("The two entries do not match.")
    return hash_password(pw)


def _name(n):
    n = n.strip().lower()
    if not VALID_NAME.fullmatch(n):
        raise SystemExit("Name: lowercase letters, digits, '.', '_' or '-' (32 characters at most).")
    return n


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app.accounts", description="Accounts of the home-web page")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("name")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--admin", action="store_true")
    g.add_argument("--user", action="store_true")
    a.add_argument("--tailscale", nargs="*", default=[], help="allowed Tailscale accounts (optional)")
    m = sub.add_parser("passwd")
    m.add_argument("name")
    gr = sub.add_parser("group")
    gr.add_argument("name")
    gr.add_argument("group", choices=GROUPS)
    t = sub.add_parser("tailscale")
    t.add_argument("name")
    t.add_argument("logins", nargs="*")
    s = sub.add_parser("remove")
    s.add_argument("name")
    args = p.parse_args(argv)

    data = _read()
    if args.action == "list":
        if not data:
            print(f"No account in {PATH}.")
        for n in sorted(data):
            c = data[n]
            ts = ", ".join(c.get("tailscale") or []) or "-"
            print(f"{n:<20} {c['group']:<6} Tailscale: {ts}")
        return 0

    name = _name(args.name)
    if args.action == "add":
        if name in data:
            raise SystemExit(f"Account {name} already exists.")
        data[name] = {"group": "admin" if args.admin else "user", "hash": _password(),
                      "tailscale": [x.strip().lower() for x in args.tailscale if x.strip()]}
        msg = f"Account {name} created ({data[name]['group']})."
    else:
        if name not in data:
            raise SystemExit(f"Unknown account {name}.")
        if args.action == "passwd":
            data[name]["hash"] = _password()
            msg = f"Password of {name} changed."
        elif args.action == "group":
            data[name]["group"] = args.group
            msg = f"{name} is now {args.group}."
        elif args.action == "tailscale":
            data[name]["tailscale"] = [x.strip().lower() for x in args.logins if x.strip()]
            msg = f"Tailscale link of {name}: {', '.join(data[name]['tailscale']) or 'none'}."
        else:
            del data[name]
            msg = f"Account {name} removed."
    if not any(c["group"] == "admin" for c in data.values()):
        print("Warning: no admin account left.")
    _write(data)
    print(msg + " The server picks it up at once.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
