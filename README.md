# QIDI Box driver for FilaMan

A [FilaMan](https://github.com/Fire-Devils/filaman-system) printer driver for the QIDI Box (for example on the QIDI Q2). It charges each spool for the filament its slot actually used, including across colour changes.

**Nothing is installed on the printer.** The driver runs inside FilaMan and uses Moonraker's normal API. It reads the Box and the running job, and the only command it sends sets a slot's filament when you assign a spool.

## What it does

- Finds the Box's slots and shows which are occupied, with their colour.
- Lets you assign a FilaMan spool to each slot from the spool's page. Assigning a spool to a new slot releases it from its old one.
- Sets the slot on the printer to match the spool you assign: its exact colour, and the printer's nearest filament (see below). The touchscreen and QIDI Studio then show your real spool.
- During a print, follows `box_extras.loaded_slot` and charges the net filament used to that slot's spool.
- Releases a slot's spool after it has read empty for 30 seconds with no print running, so a swapped spool can't be charged for the old one's filament.
- Restores that spool if the slot refills with the same filament, going by the colour, material and filament code on the printer. So an eject and reload needs no reassigning. If you select a different filament on the printer's screen within 10 minutes, the spool is released again.
- Ignores the Box's filament details for a minute after the printer or Box powers up, because it first reports default filament for every slot.
- Feeds FilaMan's AMS View with the Box's slots, temperature and humidity.

## How usage is counted

The figures come from `print_stats.filament_used`, converted to grams with the filament's diameter and density from FilaMan (1.75 mm and 1.24 g/cm³ if missing).

- Retractions count. Each unload pulls about 150 mm back onto the spool, so the total is net.
- Purge is charged to the slot that's feeding it, which is the newly loaded one.
- A running slot reports in steps of at least 500 mm and holds back 200 mm, because FilaMan can't take a report back when the unload retracts. The rest is settled when the slot unloads or the print ends.
- If readings are missed across a slot change, that filament isn't charged to either spool. It appears as `unattributed_mm` in the driver's health. An undercount is better than the wrong spool.

## Limitations

- **Two spools of identical filament:** the driver can't tell them apart, so swapping one for the other in a slot keeps charging the first. Assign the right spool on its page when you swap them.
- **Changing a restored slot's filament after 10 minutes:** the driver only watches for a different filament for 10 minutes after restoring a spool. A change on the printer's screen after that keeps the old spool assigned, so assign the right one on its page.
- **FilaMan restarting while a slot is empty:** the spool waiting to be restored is forgotten, so you'd assign it again by hand.
- **FilaMan restarting mid-print:** usage since its last report (up to about 700 mm, or 2 g) is lost, and the driver carries on from the current reading.
- **Filament left below the cutter** at each change isn't counted, as the printer doesn't report it.
- **Moving a spool in FilaMan:** the driver doesn't change a spool's FilaMan location.
- Tested on one Q2 with one Box, with a complete multi-colour print and a cancelled one. The charges matched the printer's job totals, but haven't been checked against a scale. A pause is covered by tests only, and auto refill and FilaMan restarting mid-print haven't been tested.

## Setting the slot on the printer

When you assign a spool, the driver sends the printer the command its touchscreen uses:

```
UPDATE_FILAMENT_INFORMATION SLOT=<n> COLOR='<hex>' VENDOR='<vendor>' FILAMENT='<name>'
```

- **Colour:** the spool's colour, exactly. The Box keeps any colour, not only the touchscreen's 24 presets.
- **Vendor and filament:** the printer only accepts filaments from its own list (QIDI, Generic, Bambu, HATCHBOX, Overture and Polymaker on a Q2), so the driver picks the closest one. It uses the spool's brand when the printer knows it ("Bambu Lab" is Bambu), and the filament of the same material, with words in the spool's name such as Matte, Silk or + choosing between them. Other brands get the Generic filament of that material, and a material without a Generic one gets any vendor's, QIDI first. A material the printer doesn't list at all, such as PVA, leaves the slot as it is.
- It isn't sent during a print, and a failed command never undoes the assignment. The driver's health shows the last result as `last_printer_update`.
- Turn it off with **Set slot filament on the printer** in the printer's settings.

The list is a copy of the printer's `filament-0.4.json` (in `qidi_box/catalog.py`). If a firmware update adds filaments, they can be added there.

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
