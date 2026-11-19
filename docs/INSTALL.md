# Install and run

Step by step for a Debian LXC container on Proxmox, linked to the controller by a second network card and reachable only through Tailscale. Replace the values between `< >`.

- [Install](#install)
- [HTTPS access with Tailscale](#https-access-with-tailscale)
- [Describe your house](#describe-your-house)
- [Accounts](#accounts)
- [Edit mode](#edit-mode-admins)
- [Log](#log-logs-tab-admins)
- [Update](#update)
- [PLC variables](#plc-variables)

## Install

Requirements: Python 3.11 or newer, a second network card of the container on the controller network (for example `eth1`, `192.168.100.11/24`, no gateway).

```bash
# 1. Dedicated user, without a shell
useradd -r -s /usr/sbin/nologin homeweb

# 2. Code and house files (see "Describe your house")
git clone https://github.com/<account>/home-web.git /opt/home-web
mkdir -p /opt/home-web-house          # house.tpy, rules.json, house.json, plan_default.json
chown -R root:homeweb /opt/home-web /opt/home-web-house && chmod -R g+rX,o-rwx /opt/home-web /opt/home-web-house

# 3. Dependencies
python3 -m venv /opt/home-web-venv && /opt/home-web-venv/bin/pip install -r /opt/home-web/requirements.txt

# 4. Configuration and first account (admin)
cp /opt/home-web/home-web.env.example /etc/home-web.env     # adapt the controller addresses
chown root:homeweb /etc/home-web.env && chmod 640 /etc/home-web.env
cd /opt/home-web && /opt/home-web-venv/bin/python -m app.accounts add <name> --admin

# 5. Service
cp /opt/home-web/home-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now home-web
systemctl status home-web --no-pager
```

The service only listens on `127.0.0.1:8080`. Nothing is reachable until Tailscale is set up.

## HTTPS access with Tailscale

1. Tailscale console → **DNS**: enable MagicDNS, then **Enable HTTPS** (certificates).
2. On the Proxmox host (not in the container): give the container access to the TUN device, then restart it.

```bash
echo 'lxc.cgroup2.devices.allow: c 10:200 rwm' >> /etc/pve/lxc/<id>.conf
echo 'lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file' >> /etc/pve/lxc/<id>.conf
pct reboot <id>
```

3. In the container:

```bash
apt install -y curl
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --hostname=<name> --accept-dns=false   # open the link shown and approve
tailscale serve --bg http://127.0.0.1:8080
tailscale serve status                               # shows https://<name>.<tailnet>.ts.net
```

4. Tailscale console → **Machines** → `<name>` → **Disable key expiry**.

The page is then at `https://<name>.<tailnet>.ts.net`, only from a device of the tailnet. For the family: each person gets a Tailscale account, and the machine is shared with them (Machines → ⋯ → Share).

No "userspace" mode: in that mode Tailscale also relays direct connections to local ports, and port 8080 would become reachable without HTTPS.

Optional, `HOMEWEB_TS_LOGINS=one@example.com,two@example.com` in `/etc/home-web.env`: only these Tailscale accounts can open the page, even with the right password.

## Describe your house

Four files belong to one house and **never go into this public repository** (the `.gitignore` excludes them). Put them in `/opt/home-web-house`, ideally a clone of a **private** Git repository (a branch is not enough: every branch of a public repository is public). The paths are set in `/etc/home-web.env`.

| File | Content | Without it |
|---|---|---|
| `house.tpy` | symbols of the TwinCAT program (`HOMEWEB_TPY`) | the service does not start |
| `rules.json` | fixed safety rules (`HOMEWEB_RULES`) | example rules |
| `house.json` | walls, rooms, inner doors, stairs, per floor (`HOMEWEB_HOUSE`) | example house |
| `plan_default.json` | lamps, devices, openings, detectors and shutters at first start (`HOMEWEB_PLAN_DEFAULT`) | example plan |

The formats are those of the files in `examples/` (coordinates in cm, origin at the top left). The rules file is described in [HOW-IT-WORKS.md](HOW-IT-WORKS.md#rules-file). After the first start, the plan lives in `/var/lib/home-web/plan.json` and is edited from the page: `plan_default.json` is only used once.

To read a private repository from the server, use a read-only deploy key:

```bash
ssh-keygen -t ed25519 -N "" -f /root/.ssh/home-web-house -C "home-web house files"
cat /root/.ssh/home-web-house.pub      # GitHub → private repository → Settings → Deploy keys → Add (no write access)
cat >> /root/.ssh/config <<'EOF'
Host github-home-web-house
  HostName github.com
  User git
  IdentityFile /root/.ssh/home-web-house
  IdentitiesOnly yes
EOF
git clone git@github-home-web-house:<account>/<private-repo>.git /opt/home-web-house
```

## Accounts

No signup form: everything happens in the container console. The server reloads the file by itself.

```bash
cd /opt/home-web
/opt/home-web-venv/bin/python -m app.accounts list
/opt/home-web-venv/bin/python -m app.accounts add sam --user            # asks for the password (12 characters at least)
/opt/home-web-venv/bin/python -m app.accounts add alex --admin --tailscale alex@example.com
/opt/home-web-venv/bin/python -m app.accounts passwd sam
/opt/home-web-venv/bin/python -m app.accounts group sam admin           # or user
/opt/home-web-venv/bin/python -m app.accounts tailscale alex alex@example.com   # no address: removes the link
/opt/home-web-venv/bin/python -m app.accounts remove sam
```

- **user**: sees everything and sends every command.
- **admin**: also edit mode, Logs tab and admin-only functions of the rules file.
- **Tailscale link** (optional, advised for admins): the account only opens from that Tailscale account.
- File `/etc/home-web-accounts.json` (root:homeweb, 640): only scrypt hashes of the passwords.
- An account removed or moved to another group takes effect at once, even on a page already open.

## Edit mode (admins)

"Edit the plan" button under the floors, then touch an element (or "Edit" in the lists):

- rename, move, change the state read (output or input) and the command variable of any element;
- link a window to a shutter and change the ▲ ▼ variables of a shutter;
- "Add a device": touch the place, then choose the type (lamp, LED, socket, fan, heater, pump, other) and, for a device, its icon;
- "History": go back to one of the last 100 versions of the plan.

The lists only offer the variables of the `.tpy`. The server refuses forbidden outputs, state variables, the variables of the functions and of the garage, and a variable used by two elements. Only lamps and devices can be added or removed. Each edit is logged with the before and after.

## Log (Logs tab, admins)

Logins (successful, refused, blocked), logouts, commands with their result, plan edits, account changes, refused attempts, every door and window opened or closed (under the name of the element, with how long it stayed open) and the alarm siren triggered or stopped. Filters by account or element (front door and garage door first) and by type. One file per month in `/var/lib/home-web/log-YYYY-MM.jsonl`, deleted after 12 months. Also in `journalctl -u home-web`.

## Update

One command, `tools/update.sh`: `git pull` of the code and of the house files, rights, systemd unit if it changed, check of the `.tpy`, restart and check.

```bash
ln -sf /opt/home-web/tools/update.sh /usr/local/bin/home-web-update   # once
home-web-update                                                     # at each update
```

Pages already open keep the old version until they are reloaded.

**A new PLC program**: build it (TwinCAT PLC Control → Project → Rebuild all), download it to the controller, replace `house.tpy` with the new file, then `home-web-update`. Variables newly wired in the program become active in the page.

## PLC variables

The BX9000 only lets the network write the flags `%M`. The program declares one variable `<prefix>… AT %MX… : BOOL;` per command and adds it (with OR) to the condition of the matching button. The server sends a pulse: TRUE for 100 ms (`HOMEWEB_PULSE_MS`), then FALSE. Nothing to add in MAIN.

Check a `.tpy` before installing it:

```bash
/opt/home-web-venv/bin/python -m tools.check_tpy check house.tpy
/opt/home-web-venv/bin/python -m tools.check_tpy table house.tpy > ADDRESSES.md   # full table (keep it private)
/opt/home-web-venv/bin/python -m tools.check_tpy declarations                    # declarations to paste into TwinCAT
```

Guide for the program side: [PLC-GUIDE.md](PLC-GUIDE.md). Details and safeguards: [HOW-IT-WORKS.md](HOW-IT-WORKS.md).
