"""Event log: logins, commands, plan edits, doors opened and closed.

One JSON line per event, one file per month in the state folder of the service
(/var/lib/home-web/log-2026-11.jsonl). Files older than 12 months are deleted automatically.
About 250 bytes per event, so a few MB per year.

Fields of a line:
  t      date and time in HOMEWEB_TZ (e.g. "2026-11-15T20:03:12+01:00")
  user   account that acted ("" if unknown, e.g. wrong password);
         for the "house" type: name of the element ("Front door")
  ts     Tailscale account the request came from
  type   login | command | edit | account | denied | house (door opened / closed, siren)
  evt    readable text ("Switch on: Kitchen spots")
  res    result (ok, refused, no visible effect, error...)
  detail optional: details (variable, before / after...)

Only admins can read the log (Logs tab of the page).
"""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("HOMEWEB_TZ", "UTC"))
except Exception:                       # time zone database missing: stay in UTC
    TZ = timezone.utc

log = logging.getLogger("home-web")
KEEP_MONTHS = 12
TYPES = ("login", "command", "edit", "account", "denied", "house")
UNKNOWN_PREFIX = "(unknown) "           # attempts with an account name that does not exist


class EventLog:
    def __init__(self, folder):
        self.folder = Path(folder) if folder else None
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._purged = None
        self._memory = []               # demo mode without a folder: last events in memory

    def _file(self, when):
        return self.folder / f"log-{when:%Y-%m}.jsonl"

    def write(self, type_, evt, user="", ts="", res="", **detail):
        """Adds an event (and copies it to journalctl)."""
        now = datetime.now(TZ)
        line = {"t": now.isoformat(timespec="seconds"), "user": user or "", "ts": ts or "",
                "type": type_, "evt": evt, "res": res}
        if detail:
            line["detail"] = detail
        log.info("[%s] %s %s%s", type_, user or "-", evt, f" : {res}" if res else "")
        if not self.folder:
            with self._lock:
                self._memory.append(line)
                del self._memory[:-2000]
            return
        with self._lock:
            try:
                with open(self._file(now), "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
            except OSError as e:
                log.error("Cannot write the log: %s", e)
            if self._purged != now.date():
                self._purge(now)

    def _purge(self, now):
        """Deletes the files older than KEEP_MONTHS months (at most once a day)."""
        self._purged = now.date()
        limit = (now.replace(day=1) - timedelta(days=31 * KEEP_MONTHS)).strftime("%Y-%m")
        for f in self.folder.glob("log-*.jsonl"):
            if f.stem[len("log-"):] < limit:
                try:
                    f.unlink()
                    log.info("Log: %s deleted (older than %d months)", f.name, KEEP_MONTHS)
                except OSError:
                    pass

    def _lines(self):
        """All lines, newest first."""
        if not self.folder:
            with self._lock:
                yield from reversed(list(self._memory))
            return
        with self._lock:
            files = sorted(self.folder.glob("log-*.jsonl"), reverse=True)
        for f in files:
            try:
                raw = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for r in reversed(raw):
                try:
                    yield json.loads(r)
                except ValueError:
                    continue

    def read(self, limit=200, before=None, user=None, type_=None):
        """Events from the newest to the oldest.

        before: keep only those strictly older than this date (for "load more").
        Returns (lines, more); more = True if older ones are left.
        """
        out = []
        for e in self._lines():
            if before and e.get("t", "") >= before:
                continue
            if user and e.get("user") != user:
                continue
            if type_ and e.get("type") != type_:
                continue
            if len(out) == limit:
                return out, True
            out.append(e)
        return out, False

    def names(self):
        """Names found in the log, for the filter of the Logs tab: (accounts, elements).

        Elements are the doors and windows (lines of type "house").
        """
        accounts, elements = set(), set()
        for d in self._lines():
            u = d.get("user")
            if not u or u.startswith(UNKNOWN_PREFIX):
                continue
            (elements if d.get("type") == "house" else accounts).add(u)
        return sorted(accounts), sorted(elements)
