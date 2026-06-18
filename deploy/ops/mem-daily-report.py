#!/usr/bin/env python3
"""Daily memory-health digest for the NAC-Pay / CrewRef EC2 box.

Reads the 5-minute sampler log (~/mem-monitor.log, written by mem-monitor.sh),
summarizes the last 24h, and emails it via the Resend API using the same
credentials the app uses (/opt/nac-pay/deploy/.env.prod). Invoked once a day
by cron. Pure stdlib — no pip deps.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import urllib.error
import urllib.request

HOME = pathlib.Path.home()
LOG = HOME / "mem-monitor.log"
ENV = pathlib.Path("/opt/nac-pay/deploy/.env.prod")
TO = "dennfish@gmail.com"
WINDOW_HOURS = 24
AVAIL_FLOOR_MB = 400      # below this = pressure
SWAP_HEAVY_MB = 768       # above this = pressure


def env(key: str, default: str = "") -> str:
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return default


def tok(line: str, key: str) -> str | None:
    for t in line.split():
        if t.startswith(key + "="):
            return t[len(key) + 1:].replace("MB", "")
    return None


def main() -> int:
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=WINDOW_HOURS)
    lines: list[str] = []
    if LOG.exists():
        for ln in LOG.read_text().splitlines():
            try:
                ts = datetime.datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts >= cutoff:
                lines.append(ln)

    min_avail = None
    max_swap = 0
    total_mb = None
    low = heavy = max_restarts = 0
    oom = False
    for ln in lines:
        a, s, r, t = tok(ln, "avail"), tok(ln, "swap"), tok(ln, "restarts"), tok(ln, "total")
        if a is not None:
            a = int(a)
            min_avail = a if min_avail is None else min(min_avail, a)
        if s is not None:
            max_swap = max(max_swap, int(s))
        if r is not None:
            max_restarts = max(max_restarts, int(r))
        if t is not None:
            total_mb = int(t)
        if "oomkilled=true" in ln:
            oom = True
        if "[LOW_AVAIL]" in ln:
            low += 1
        if "[SWAP_HEAVY]" in ln:
            heavy += 1

    n = len(lines)
    latest = lines[-1] if lines else "(no samples in window)"
    healthy = (
        (min_avail is None or min_avail > AVAIL_FLOOR_MB)
        and max_swap < SWAP_HEAVY_MB
        and low == 0 and heavy == 0 and max_restarts == 0 and not oom
    )
    status = "Healthy" if healthy else "PRESSURE DETECTED"
    today = datetime.date.today().isoformat()
    subject = f"[{'OK' if healthy else 'ALERT'}] NAC-Pay box memory — {today}"
    body = (
        f"NAC-Pay box (pch-ledger / CrewRef EC2) — {WINDOW_HOURS}h memory report\n"
        f"Status: {status}\n\n"
        f"Samples: {n} (5-min cadence)\n"
        f"Min available RAM: {min_avail} MB"
        + (f" of {total_mb}\n" if total_mb else "\n")
        + f"Peak swap used: {max_swap} MB of 2048\n"
        f"LOW_AVAIL flags (<{AVAIL_FLOOR_MB}MB): {low}\n"
        f"SWAP_HEAVY flags (>{SWAP_HEAVY_MB}MB): {heavy}\n"
        f"nac-pay restarts (window max): {max_restarts}\n"
        f"OOM kill seen: {'YES' if oom else 'no'}\n\n"
        f"Latest sample:\n{latest}\n"
    )

    api_key = env("RESEND_API_KEY")
    sender = env("RESEND_FROM_EMAIL", "no-reply@pch-ledger.com")
    if not api_key:
        print("FATAL: no RESEND_API_KEY in", ENV)
        return 1
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({"from": sender, "to": [TO], "subject": subject, "text": body}).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend's edge 403s the default "Python-urllib/x" UA; set our own.
            "User-Agent": "nac-pay-monitor/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"{datetime.datetime.now():%F %T} sent ({status}) http={resp.status}")
            return 0
    except urllib.error.HTTPError as e:  # surface the API error body
        print(f"{datetime.datetime.now():%F %T} SEND FAILED: {e.code} {e.read().decode(errors='replace')}")
        return 1
    except Exception as e:  # noqa: BLE001 — cron job, log and exit nonzero
        print(f"{datetime.datetime.now():%F %T} SEND FAILED: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
