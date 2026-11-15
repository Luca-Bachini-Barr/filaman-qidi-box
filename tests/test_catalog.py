"""Matching FilaMan spools to the QIDI Box's own filament list."""

from __future__ import annotations

import pytest

from qidi_box.catalog import PRINTER_FILAMENTS, match


@pytest.mark.parametrize(
    ("manufacturer", "designation", "material", "expected"),
    [
        # The printer knows the brand: its filament of that material.
        ("Bambu Lab", "Matte Dark Green", "PLA", ("Bambu", "PLA")),
        ("Bambu Lab", "Silk+ Purple", "PLA", ("Bambu", "PLA")),
        ("Polymaker", "PolyLite", "PLA", ("Polymaker", "PLA")),
        ("HATCHBOX", "Black", "petg", ("HATCHBOX", "PETG")),
        # Words in the spool's name choose between a brand's filaments.
        ("QIDI Tech", "Matte Watermelon Red", "PLA", ("QIDI", "PLA Matte Basic")),
        ("QIDI Tech", "Red", "PLA", ("QIDI", "PLA Basic")),
        ("QIDI", "Rapido Matte Grey", "PLA", ("QIDI", "PLA Rapido Matte")),
        ("QIDI", "Silk Gold", "PLA", ("QIDI", "PLA Silk")),
        ("QIDI", "Black", "PETG", ("QIDI", "PETG Basic")),
        # Unknown brands, or a material the brand lacks: Generic.
        ("Creality", "CR-PETG Black", "PETG", ("Generic", "PETG")),
        ("ELEGOO", "Wood Color", "PLA", ("Generic", "PLA")),
        ("ELEGOO", "Silk+ Gold", "PLA", ("Generic", "PLA Silk")),
        ("eSun", "PLA+ Black", "PLA", ("Generic", "PLA+")),
        ("Bambu Lab", "Basic", "TPU", ("Generic", "TPU 95A")),
        ("", "", "ABS", ("Generic", "ABS")),
        # No Generic one either: the closest any vendor has.
        ("Eyrone", "Navy", "ASA", ("QIDI", "ASA")),
    ],
)
def test_match(manufacturer, designation, material, expected):
    assert match(manufacturer, designation, material) == expected


@pytest.mark.parametrize("material", ["PVA", "", "Unobtainium"])
def test_material_the_printer_lacks(material):
    assert match("Bambu Lab", "Natural", material) is None


def test_names_are_safe_to_quote_in_gcode():
    for entries in PRINTER_FILAMENTS.values():
        for name, _kind in entries:
            assert "'" not in name and "\n" not in name
