#!/usr/bin/env python3
"""Log what a QIDI Box printer reports during a print. Read-only.

Waits for a print to start, writes one JSON line per poll to its own file, then
adds the job's metadata and history and a per-slot summary when it ends. It only
sends GET requests to Moonraker, so it can't affect the printer.

    box_print_logger.py --host PRINTER [--dir logs]    watch and log
    box_print_logger.py --host PRINTER --once          show one reading
    box_print_logger.py --summarize FILE.jsonl         re-print a summary
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ACTIVE_STATES = {"printing", "paused"}
IDLE_POLL = 5.0
PRINT_POLL = 1.0

# None asks Moonraker for every field of the object
OBJECTS = {
    "print_stats": ["state", "filename", "filament_used", "print_duration", "info"],
    "toolhead": ["position", "extruder"],
    "box_extras": ["loaded_slot", "last_load_slot", "is_tool_change"],
    "multi_color_controller": ["task", "operation"],
    "filament_switch_sensor filament_switch_sensor": None,
    "box_stepper slot0": None,
    "box_stepper slot1": None,
    "box_stepper slot2": None,
    "box_stepper slot3": None,
}


def get_json(host: str, path: str, timeout: float = 5.0):
    with urllib.request.urlopen(f"http://{host}:7125{path}", timeout=timeout) as resp:
        return json.load(resp)["result"]


def query_path() -> str:
    parts = []
    for name, fields in OBJECTS.items():
        part = urllib.parse.quote(name)
        if fields:
            part += "=" + ",".join(fields)
        parts.append(part)
    return "/printer/objects/query?" + "&".join(parts)


def read_status(host: str) -> dict:
    return get_json(host, query_path())["status"]


def job_info(host: str, filename: str) -> dict:
    """Best effort: what the slicer says about this file, and the printer's state."""
    info: dict = {}
    requests = {
        "metadata": "/server/files/metadata?filename=" + urllib.parse.quote(filename),
        "save_variables": "/printer/objects/query?save_variables",
    }
    for key, path in requests.items():
        try:
            info[key] = get_json(host, path)
        except (OSError, ValueError, KeyError) as err:
            info[key] = f"unavailable: {err}"
    return info


def summarize(path: Path) -> str:
    """Net filament_used and extruder movement per loaded slot, and the slot changes."""
    used: dict[str, float] = {}
    extruded: dict[str, float] = {}
    changes: list[str] = []
    prev = None
    count = 0
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        if "status" not in rec:
            continue
        count += 1
        status = rec["status"]
        slot = str((status.get("box_extras") or {}).get("loaded_slot"))
        filament = (status.get("print_stats") or {}).get("filament_used") or 0.0
        position = (status.get("toolhead") or {}).get("position") or [0, 0, 0, 0]
        if prev is not None:
            # Signed: a retraction hands filament back to the spool, so forward
            # moves alone would overstate usage.
            used[prev["slot"]] = used.get(prev["slot"], 0.0) + filament - prev["filament"]
            extruded[prev["slot"]] = extruded.get(prev["slot"], 0.0) + position[3] - prev["epos"]
            if slot != prev["slot"]:
                changes.append(
                    f"{rec['time']}  {prev['slot']} -> {slot}  (filament_used {filament:.1f} mm)"
                )
        prev = {"slot": slot, "filament": filament, "epos": position[3]}

    lines = [f"{path.name}: {count} readings, {len(changes)} slot changes"]
    if prev is not None:
        lines.append(f"final filament_used: {prev['filament']:.1f} mm")
    lines.append("per slot (net filament_used / net extruder movement, mm):")
    for slot in sorted(set(used) | set(extruded)):
        lines.append(f"  {slot}: {used.get(slot, 0.0):.1f} / {extruded.get(slot, 0.0):.1f}")
    lines.append("slot changes:")
    lines.extend(f"  {change}" for change in changes)
    return "\n".join(lines)


def log_print(host: str, out_dir: Path, first_status: dict) -> Path:
    """Log one print from its first reading until it leaves the active states."""
    filename = first_status["print_stats"].get("filename") or "unknown"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in filename)
    path = out_dir / f"{stamp}-{safe_name}.jsonl"
    print(f"{stamp} print started: {filename} -> {path}", flush=True)

    status = first_status
    failures = 0
    with path.open("a") as log:
        log.write(json.dumps({"job_start": job_info(host, filename)}) + "\n")
        while True:
            log.write(json.dumps({"time": time.strftime("%H:%M:%S"), "status": status}) + "\n")
            log.flush()
            if status["print_stats"].get("state") not in ACTIVE_STATES:
                break
            time.sleep(PRINT_POLL)
            try:
                status = read_status(host)
                failures = 0
            except (OSError, ValueError, KeyError) as err:
                failures += 1
                log.write(json.dumps({"time": time.strftime("%H:%M:%S"), "error": str(err)}) + "\n")
                # The printer may be restarting. Five minutes silent means it's over
                if failures * (PRINT_POLL + 5) > 300:
                    break
        try:
            history = get_json(host, "/server/history/list?limit=1&order=desc")
        except (OSError, ValueError, KeyError) as err:
            history = f"unavailable: {err}"
        log.write(json.dumps({"job_end": history}) + "\n")

    summary = summarize(path)
    path.with_suffix(".summary.txt").write_text(summary + "\n")
    print(summary, flush=True)
    return path


def watch(host: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"watching {host}, logging to {out_dir}", flush=True)
    while True:
        try:
            status = read_status(host)
        except (OSError, ValueError, KeyError):
            time.sleep(IDLE_POLL)
            continue
        if status["print_stats"].get("state") in ACTIVE_STATES:
            log_print(host, out_dir, status)
        time.sleep(IDLE_POLL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", help="the printer's address")
    parser.add_argument("--dir", type=Path, default=Path(__file__).parent / "logs")
    parser.add_argument("--once", action="store_true", help="show one reading and exit")
    parser.add_argument("--summarize", type=Path, help="summarize a log and exit")
    args = parser.parse_args()

    if args.summarize:
        print(summarize(args.summarize))
    elif not args.host:
        parser.error("--host is required")
    elif args.once:
        print(json.dumps(read_status(args.host), indent=1))
    else:
        with contextlib.suppress(KeyboardInterrupt):
            watch(args.host, args.dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
