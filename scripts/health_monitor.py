#!/usr/bin/env python3
"""Watch the time server and post to a webhook when its health changes.

Run it every few minutes from cron on a machine OTHER than the time server,
so it still reports when the Pi itself is down:

    */5 * * * * /usr/local/bin/timeserver-monitor

Each run asks the dashboard for its health (and, optionally, asks the NTP
service itself for the time) and compares the result with the last runs. A
change has to last THRESHOLD runs in a row before anyone is told, so a restart
does not page you. While the server stays unhealthy a reminder goes out every
REMIND_HOURS.

Settings are KEY=VALUE lines in /etc/default/timeserver-monitor (or the file
given with --config); see timeserver-monitor.conf.example. The webhook gets a
JSON body with the message under both "content" (Discord) and "text" (Slack,
Mattermost, Google Chat).

    health_monitor.py --test     send a test message and exit
    health_monitor.py --show     print the current state without notifying
"""

import argparse
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request

DEFAULTS = {
    "NAME": "time server",
    "HEALTH_URL": "http://127.0.0.1:8080/api/health",
    "NTP_HOST": "",              # also query NTP on this host; empty = skip
    # Outside NTP servers to compare the time server with. Empty = skip.
    # Avoid servers that smear leap seconds (Google's, for one).
    "REFERENCES": "time.cloudflare.com pool.ntp.org",
    "MAX_DISAGREE_MS": "10",     # alert if the server is further than this from them
    "WEBHOOK_URL": "",           # empty = log only
    "THRESHOLD": "3",            # runs a change must last before notifying
    "REMIND_HOURS": "24",        # repeat the alert this often while unhealthy
    "STATE_FILE": "/var/lib/timeserver-monitor/state.json",
}
OK = "OK"
NTP_EPOCH_OFFSET = 2208988800    # seconds between 1900 (NTP) and 1970 (Unix)


def load_config(path):
    """Settings from KEY=VALUE lines in `path`, over the defaults."""
    config = dict(DEFAULTS)
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return config


def check_health(url, timeout=10):
    """(state, issues) from the dashboard's /api/health."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            report = json.load(response)
    except urllib.error.HTTPError as e:
        # ?strict=1 answers 503 with the same JSON body when not OK.
        try:
            report = json.load(e)
        except ValueError:
            return "UNREACHABLE", [f"dashboard answered HTTP {e.code}"]
    except (OSError, ValueError) as e:
        # URLError wraps the real cause (connection refused, timed out, ...).
        return "UNREACHABLE", [f"dashboard did not answer: {getattr(e, 'reason', e)}"]
    state = str(report.get("status", "UNKNOWN"))
    return state, [str(i) for i in report.get("issues", [])]


def _ntp_time(raw):
    """Seconds since 1970 from a 64-bit NTP timestamp."""
    seconds, fraction = struct.unpack("!II", raw)
    return seconds - NTP_EPOCH_OFFSET + fraction / 2**32


def ntp_query(host, timeout=5):
    """Ask one NTP server for the time.

    Returns {"offset", "delay", "stratum", "leap"}: offset is how far the
    server's clock is from this machine's (positive = server ahead), worked
    out from the four timestamps of the exchange as NTP clients do, so the
    network delay mostly cancels out. Raises OSError if there is no usable
    answer.
    """
    request = bytearray(48)
    request[0] = 0x23                     # version 4, client mode
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t1 = time.time()
        s.sendto(bytes(request), (host, 123))
        reply, _ = s.recvfrom(512)
        t4 = time.time()
    if len(reply) < 48:
        raise OSError("short reply")
    t2, t3 = _ntp_time(reply[32:40]), _ntp_time(reply[40:48])
    return {"offset": ((t2 - t1) + (t3 - t4)) / 2,
            "delay": (t4 - t1) - (t3 - t2),
            "stratum": reply[1],
            "leap": reply[0] >> 6}


def check_ntp(host, references=(), max_disagree_s=0.010, query=ntp_query):
    """Problems with the time server's NTP service, as a list of descriptions.

    Checks that it answers and says it is synchronised. With `references`,
    also compares its time with theirs: this machine's own clock cancels out,
    so the check is independent of both the time server's health report and
    this machine's clock.
    """
    try:
        server = query(host)
    except OSError as e:
        return [f"NTP did not answer: {getattr(e, 'reason', e) or 'timed out'}"]
    if server["stratum"] == 0 or server["stratum"] > 15 or server["leap"] == 3:
        return ["NTP says it is not synchronised"]
    others = []
    for ref in references:
        try:
            answer = query(ref)
        except OSError:
            continue
        if 0 < answer["stratum"] <= 15 and answer["leap"] != 3:
            others.append(answer)
    if not others:
        return []   # no outside opinion this time; nothing to compare with
    others.sort(key=lambda a: a["offset"])
    middle = others[len(others) // 2]
    difference = server["offset"] - middle["offset"]
    # A single exchange can be wrong by up to half its round trip (the path
    # out and back may differ), so allow for both measurements' share of that
    # on top of the configured margin. Over Wi-Fi that is several ms.
    allowed = max_disagree_s + (server["delay"] + middle["delay"]) / 2
    if abs(difference) > allowed:
        servers = f"{len(others)} outside server{'s' if len(others) > 1 else ''}"
        return [f"NTP time is {difference * 1000:+.1f} ms from {servers}"]
    return []


def observe(config):
    """The server's state right now, as (state, issues)."""
    state, issues = check_health(config["HEALTH_URL"])
    if config["NTP_HOST"]:
        problems = check_ntp(config["NTP_HOST"], config["REFERENCES"].split(),
                             float(config["MAX_DISAGREE_MS"]) / 1000)
        if problems:
            issues.extend(problems)
            if state == OK:
                state = "FAULT"
    return state, issues


def span(seconds):
    """A duration in words: 25 min, 2 h 10 min, 1 d 2 h."""
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def decide(previous, state, issues, now, name, threshold, remind_s):
    """Work out the new stored state and the message to send, if any.

    `previous` is what the last run stored (or {} the first time):
      reported        the state people were last told about
      reported_since  when that state began
      candidate       a different state seen recently, not yet reported
      candidate_runs  how many runs in a row it has been seen
      candidate_since when it was first seen
      last_notified   when the last message went out
    """
    new = dict(previous)
    reported = previous.get("reported")
    detail = "; ".join(issues) if issues else "no details"

    if reported is None:
        # First run: adopt whatever we see, and say so only if it is bad.
        new.update(reported=state, reported_since=now, candidate=None,
                   candidate_runs=0, last_notified=now)
        if state != OK:
            return new, f"[ALERT] {name}: {state} - {detail}"
        return new, None

    if state == reported:
        new.update(candidate=None, candidate_runs=0)
        if state != OK and now - previous.get("last_notified", 0) >= remind_s:
            new["last_notified"] = now
            lasted = span(now - previous.get("reported_since", now))
            return new, f"[ALERT] {name}: still {state} after {lasted} - {detail}"
        return new, None

    # A different state. Count how long it has lasted.
    if previous.get("candidate") == state:
        runs = previous.get("candidate_runs", 0) + 1
        since = previous.get("candidate_since", now)
    else:
        runs, since = 1, now
    new.update(candidate=state, candidate_runs=runs, candidate_since=since)
    if runs < threshold:
        return new, None

    lasted = span(since - previous.get("reported_since", since))
    new.update(reported=state, reported_since=since, candidate=None,
               candidate_runs=0, last_notified=now)
    if state == OK:
        return new, f"[OK] {name}: healthy again (was {reported} for {lasted})"
    return new, f"[ALERT] {name}: {state} (was {reported}) - {detail}"


def send(webhook_url, message, timeout=10):
    """POST the message to the webhook; raises OSError if it is not delivered."""
    body = json.dumps({"content": message, "text": message}).encode()
    request = urllib.request.Request(
        webhook_url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "timeserver-monitor"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def load_state(path):
    """What the previous run stored, or {} if there is nothing usable."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    """Store the state for the next run, replacing the file in one step."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main(argv=None):
    """Run one check; returns the process exit status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="/etc/default/timeserver-monitor")
    parser.add_argument("--test", action="store_true",
                        help="send a test message to the webhook and exit")
    parser.add_argument("--show", action="store_true",
                        help="print what a run would see, without notifying")
    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.test:
        if not config["WEBHOOK_URL"]:
            print(f"WEBHOOK_URL is not set in {args.config}", file=sys.stderr)
            return 2
        send(config["WEBHOOK_URL"], f"[TEST] {config['NAME']}: alerts are set up")
        print("test message sent")
        return 0

    state, issues = observe(config)
    if args.show:
        print(state, "-", "; ".join(issues) or "no issues")
        return 0

    previous = load_state(config["STATE_FILE"])
    new, message = decide(previous, state, issues, time.time(), config["NAME"],
                          int(config["THRESHOLD"]),
                          float(config["REMIND_HOURS"]) * 3600)
    if message:
        print(message)
        if config["WEBHOOK_URL"]:
            try:
                send(config["WEBHOOK_URL"], message)
            except (OSError, ValueError) as e:
                # Keep the old state so the next run tries again.
                print(f"could not reach the webhook: {e}", file=sys.stderr)
                return 1
    save_state(config["STATE_FILE"], new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
