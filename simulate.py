"""
Spendenlauf Simulator
=====================
Registers test runners, then simulates them running laps by
firing scan events at the backend.  Great for testing without
any ESP32 hardware.

Usage:
  python simulate.py               # defaults to http://localhost:8000
  python simulate.py --url http://192.168.1.50:8000
  python simulate.py --runners 10  # override runner count (default 20)
  python simulate.py --speed 5     # 5x real time (default 10x)
"""

import argparse
import random
import time
import requests
from datetime import datetime, timedelta

# ── Configuration ──────────────────────────────────────────────

CHECKPOINT_COUNT = 5
REAL_LAP_SECONDS = 10 * 60  # ~10 min for a 2 km lap at jogging pace

FIRST_NAMES = [
    "Lena", "Max", "Sophie", "Jonas", "Emma", "Felix", "Mia", "Leon",
    "Hannah", "Paul", "Lea", "Tim", "Anna", "Ben", "Laura", "Noah",
    "Marie", "Lukas", "Ella", "Finn",
]

LAST_NAMES = [
    "Müller", "Schmidt", "Weber", "Fischer", "Meyer", "Wagner",
    "Becker", "Schulz", "Hoffmann", "Koch", "Richter", "Wolf",
    "Klein", "Schröder", "Neumann", "Braun", "Zimmermann",
    "Krüger", "Hartmann", "Lange",
]


def mac(i: int) -> str:
    """Generate a fake beacon MAC for runner index i."""
    return f"AA:BB:CC:DD:EE:{i:02X}"


def register_runners(base_url: str, n: int):
    """Register n test runners, each with a random donation amount."""
    print(f"\n📋 Registering {n} runners…")
    runners = []
    used_names = set()

    for i in range(n):
        # Pick a unique first/last name combination
        while True:
            vorname  = random.choice(FIRST_NAMES)
            nachname = random.choice(LAST_NAMES)
            name = f"{vorname} {nachname}"
            if name not in used_names:
                used_names.add(name)
                break

        bib = i + 1
        beacon_mac = mac(bib)

        # 1. Pair the start number with a beacon MAC.
        requests.post(f"{base_url}/api/beacons",
                      json={"bib_number": bib, "beacon_mac": beacon_mac})

        # 2. Register the runner against that start number.
        runner = {
            "vorname": vorname,
            "nachname": nachname,
            "bib_number": bib,
            "donation_per_km": round(random.uniform(0.5, 5.0), 2),
        }
        runner["beacon_mac"] = beacon_mac  # kept locally for the scan loop

        resp = requests.post(f"{base_url}/api/runners", json=runner)
        if resp.status_code == 200:
            print(f"   ✔ #{bib:>2}  {name:<22} "
                  f"€{runner['donation_per_km']:.2f}/km  [{beacon_mac}]")
        elif resp.status_code == 409:
            print(f"   ⚠ #{bib} already exists, skipping")
        else:
            print(f"   ✘ #{bib} failed: {resp.text}")

        runners.append(runner)

    return runners


def simulate(base_url: str, runners: list, speed: float):
    """
    Simulate runners circling the course.

    Each runner gets a random pace (seconds per checkpoint gap).
    The simulation runs in accelerated time controlled by --speed.
    """
    print(f"\n🏃 Starting simulation at {speed}× speed  (Ctrl+C to stop)\n")

    # Each runner: next checkpoint, simulated time until next scan
    states = []
    for r in runners:
        gap = REAL_LAP_SECONDS / CHECKPOINT_COUNT
        pace = gap * random.uniform(0.8, 1.2)  # ±20% speed variance
        states.append({
            "runner":         r,
            "next_cp":        1,
            "secs_remaining": random.uniform(0, pace),  # stagger starts
            "pace":           pace,                       # secs per checkpoint
        })

    sim_time = datetime.now()
    tick = 0.25  # real seconds per tick

    try:
        while True:
            sim_advance = tick * speed  # simulated seconds this tick
            sim_time += timedelta(seconds=sim_advance)

            for s in states:
                s["secs_remaining"] -= sim_advance
                if s["secs_remaining"] <= 0:
                    # Runner hits the next checkpoint
                    scan = {
                        "station_id": s["next_cp"],
                        "beacon_mac": s["runner"]["beacon_mac"],
                        "timestamp":  sim_time.isoformat(),
                        "rssi":       random.randint(-75, -55),
                    }
                    resp = requests.post(f"{base_url}/api/scan", json=scan)
                    result = resp.json()

                    bib  = s["runner"]["bib_number"]
                    name = f"{s['runner']['vorname']} {s['runner']['nachname']}"
                    cp   = s["next_cp"]
                    evt  = result.get("event", "?")

                    if "lap_complete" in evt:
                        laps = result.get("laps", "?")
                        print(f"   🏁 #{bib:>2} {name:<20} Station {cp} → "
                              f"LAP {laps} COMPLETE!")
                    else:
                        print(f"   📍 #{bib:>2} {name:<20} Station {cp}  ({evt})")

                    # Advance to next checkpoint
                    s["next_cp"] = (s["next_cp"] % CHECKPOINT_COUNT) + 1
                    # New random pace for variety
                    base_gap = REAL_LAP_SECONDS / CHECKPOINT_COUNT
                    s["pace"] = base_gap * random.uniform(0.8, 1.2)
                    s["secs_remaining"] = s["pace"]

            time.sleep(tick)

    except KeyboardInterrupt:
        print("\n\n⏹  Simulation stopped.")


def main():
    parser = argparse.ArgumentParser(description="Spendenlauf event simulator")
    parser.add_argument("--url",     default="http://localhost:8000",
                        help="Backend URL (default: http://localhost:8000)")
    parser.add_argument("--runners", type=int, default=20,
                        help="Number of test runners (default: 20)")
    parser.add_argument("--speed",   type=float, default=10.0,
                        help="Simulation speed multiplier (default: 10×)")
    parser.add_argument("--reset",   action="store_true",
                        help="Wipe all data before starting")
    args = parser.parse_args()

    # Health check
    try:
        requests.get(f"{args.url}/api/stats", timeout=3)
    except requests.ConnectionError:
        print(f"✘ Cannot reach server at {args.url}")
        print(f"  Start it first:  uvicorn server:app --host 0.0.0.0 --port 8000")
        return

    if args.reset:
        print("🗑  Resetting all data…")
        requests.post(f"{args.url}/api/reset")

    runners = register_runners(args.url, args.runners)
    simulate(args.url, runners, args.speed)


if __name__ == "__main__":
    main()
