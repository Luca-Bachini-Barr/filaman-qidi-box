# QIDI Box driver for FilaMan

A [FilaMan](https://github.com/Fire-Devils/filaman-system) printer driver for the QIDI Box (for example on the QIDI Q2). It charges each spool for the filament its slot actually used, including across colour changes.

**Nothing is installed on the printer.** The driver runs inside FilaMan and only reads Moonraker's normal API.

## What it does

- Finds the Box's slots and shows which are occupied, with their colour.
- Lets you assign a FilaMan spool to each slot from the spool's page. Assigning a spool to a new slot releases it from its old one.
- During a print, follows `box_extras.loaded_slot` and charges the net filament used to that slot's spool.
- Releases a slot's spool after it has read empty for 30 seconds with no print running, so a swapped spool can't be charged for the old one's filament.
- Feeds FilaMan's AMS View with the Box's slots, temperature and humidity.

## How usage is counted

The figures come from `print_stats.filament_used`, converted to grams with the filament's diameter and density from FilaMan (1.75 mm and 1.24 g/cm³ if missing).

- Retractions count. Each unload pulls about 150 mm back onto the spool, so the total is net.
- Purge is charged to the slot that's feeding it, which is the newly loaded one.
- A running slot reports in steps of at least 500 mm and holds back 200 mm, because FilaMan can't take a report back when the unload retracts. The rest is settled when the slot unloads or the print ends.
- If readings are missed across a slot change, that filament isn't charged to either spool. It appears as `unattributed_mm` in the driver's health. An undercount is better than the wrong spool.

## Limitations

- **FilaMan restarting mid-print:** usage since its last report (up to about 700 mm, or 2 g) is lost, and the driver carries on from the current reading.
- **Filament left below the cutter** at each change isn't counted, as the printer doesn't report it.
- **Moving a spool in FilaMan:** the driver doesn't change a spool's FilaMan location.
- Tested on one Q2 with one Box, with a complete multi-colour print and a cancelled one. The charges matched the printer's job totals, but haven't been checked against a scale. A pause is covered by tests only, and auto refill and FilaMan restarting mid-print haven't been tested.

## Install

1. Run `./build.sh`, or download a release zip.
2. In FilaMan, go to **Admin → System → Plugins** and upload `qidi_box-<version>.zip`.
3. Add a printer with the **QIDI Box** driver and your Moonraker URL, for example `http://192.168.1.20:7125`.
4. Assign your spools to slots from each spool's page.

**Upgrading:** upload the new zip, then restart FilaMan. It keeps a loaded driver in memory, so the new version doesn't run until the restart. FilaMan's upload window can keep showing its progress bar after a successful install, so close it and check the plugin's version in the list.

## Development

The accounting is in `qidi_box/usage.py`, with no FilaMan code, and the tests replay a real multi-colour print from `captures/`. `tools/box_print_logger.py` records a print from your own printer in the same format. It's read-only.

```
uv venv --python 3.11 && uv pip install pytest pytest-cov httpx
pytest --cov=qidi_box --cov-branch tests
```

The tests replace FilaMan's modules with stand-ins, so they don't need a FilaMan install.
