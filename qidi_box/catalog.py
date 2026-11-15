"""The QIDI Box's own filament list, and matching a FilaMan spool to it.

The printer resolves `UPDATE_FILAMENT_INFORMATION ... VENDOR='<vendor>'
FILAMENT='<name>'` against its material database, the same list its touchscreen
offers: /home/qidi/QIDI_Client/resource/filament_config/filament-0.4.json on a Q2.
This is a copy of that list as {vendor: [(name, type)]}. A name the printer doesn't
know could leave the slot without a filament, so only names from here are sent.
"""

from __future__ import annotations

import re

PRINTER_FILAMENTS: dict[str, list[tuple[str, str]]] = {
    "QIDI": [
        ("ABS Odorless", "ABS"),
        ("ABS Rapido", "ABS"),
        ("ABS Rapido Metal", "ABS"),
        ("ABS-GF", "ABS-GF"),
        ("ASA", "ASA"),
        ("ASA-Aero", "ASA-AERO"),
        ("ASA-CF20 Core", "ASA-CF"),
        ("PA12-CF", "PA12-CF"),
        ("PA6-CF", "PA6-CF"),
        ("PAHT-CF", "PAHT-CF"),
        ("PAHT-GF", "PAHT-GF"),
        ("PC/ABS-FR", "PC-ABS-FR"),
        ("PEBA 95A", "PEBA"),
        ("PET-CF", "PET-CF"),
        ("PET-GF", "PET-GF"),
        ("PETG Basic", "PETG"),
        ("PETG Rapido", "PETG"),
        ("PETG Tough", "PETG"),
        ("PETG Translucent", "PETG"),
        ("PETG-CF", "PETG-CF"),
        ("PETG-GF", "PETG-GF"),
        ("PLA Basic", "PLA"),
        ("PLA Matte Basic", "PLA"),
        ("PLA Rapido", "PLA"),
        ("PLA Rapido Matte", "PLA"),
        ("PLA Rapido Metal", "PLA"),
        ("PLA Silk", "PLA"),
        ("PLA-CF", "PLA-CF"),
        ("PPS-CF", "PPS-CF"),
        ("PPS-GF", "PPS-GF"),
        ("TPU 95A-HF", "TPU"),
        ("TPU-Aero", "TPU-AERO"),
        ("TPU-GF", "TPU-GF"),
        ("UltraPA", "UltraPA"),
        ("UltraPA-CF25", "UltraPA-CF25"),
        ("WOOD Rapido", "PLA"),
    ],
    "Generic": [
        ("ABS", "ABS"),
        ("PC", "PC"),
        ("PETG", "PETG"),
        ("PLA", "PLA"),
        ("PLA Silk", "PLA"),
        ("PLA+", "PLA"),
        ("TPU 95A", "TPU"),
    ],
    "Bambu": [("ABS", "ABS"), ("PETG", "PETG"), ("PLA", "PLA")],
    "HATCHBOX": [("ABS", "ABS"), ("PETG", "PETG"), ("PLA", "PLA")],
    "Overture": [("ABS", "ABS"), ("PLA", "PLA")],
    "Polymaker": [("ABS", "ABS"), ("PLA", "PLA"), ("PLA-HT", "PLA")],
}

# Each vendor's everyday filament, picked when nothing in the spool's name says more.
_DEFAULT_WORDS = {"basic"}


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _words(text: str) -> set[str]:
    """Lower-case words, with "+" as a word of its own (so "Silk+" is silk and +)."""
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    return words | {"+"} if "+" in text else words


def _vendor_for(manufacturer: str) -> str:
    """The printer's vendor for a FilaMan manufacturer, or Generic."""
    key = _key(manufacturer)
    if key:
        for vendor in PRINTER_FILAMENTS:
            if vendor != "Generic" and key.startswith(_key(vendor)):
                return vendor
    return "Generic"


def _best(entries: list[tuple[str, str]], material: str, designation: str) -> str | None:
    """The entry of this material whose extra words best fit the spool's name."""
    wanted = _words(designation)
    best: tuple[int, int, int] | None = None
    choice = None
    for index, (name, kind) in enumerate(entries):
        if _key(kind) != _key(material):
            continue
        extra = _words(name) - _words(kind)
        hits = len(extra & wanted)
        misses = len(extra - wanted - _DEFAULT_WORDS)
        score = (hits - misses, -len(extra - wanted), -index)
        if best is None or score > best:
            best, choice = score, name
    return choice


def match(manufacturer: str, designation: str, material: str) -> tuple[str, str] | None:
    """Pick the printer's vendor and filament name for a spool, or None.

    The spool's own vendor is used when the printer knows it and lists the
    material; otherwise a Generic filament of the same material; otherwise any
    vendor's filament of that material, QIDI first.
    """
    material = material or ""
    vendor = _vendor_for(manufacturer or "")
    for candidate in dict.fromkeys([vendor, "Generic", *PRINTER_FILAMENTS]):
        name = _best(PRINTER_FILAMENTS[candidate], material, designation or "")
        if name:
            return candidate, name
    return None
