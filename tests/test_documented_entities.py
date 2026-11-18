"""Documentation drift test: the entities in README.md must match the entity registry.

A fully capable *synthetic* fake machine (every capability flag and key present) is set up and the
registry entries (platform + name) are compared with the entity tables of README.md. Adding an entity
in code without documenting it, or documenting one that does not exist, fails this test. The
"disabled by default" statements in the README are compared with ``disabled_by`` as well.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import SetupIntegration
from .fake_server import FakeGaggiMate, load_json

README = Path(__file__).parent.parent / "README.md"

# README section heading -> registry platform. Each section holds a table with the columns
# "Entity" and "Enabled by default" (Yes/No); other tables in a section (e.g. the Events
# "Event type" table) are not entity lists and are skipped.
SECTION_PLATFORM: dict[str, str] = {
    "Sensors": "sensor",
    "Binary Sensors": "binary_sensor",
    "Buttons": "button",
    "Switches": "switch",
    "Numbers": "number",
    "Selects": "select",
    "Events": "event",
}
ENABLED_COLUMN = "Enabled by default"

Key = tuple[str, str]  # (platform, entity name)


def _synthetic_full_frames() -> tuple[dict, dict]:
    """One synthetic frame set that carries every capability flag and key the integration reads."""
    data = copy.deepcopy(load_json("dual_boiler_idle"))
    state, telemetry = data["state"], data["telemetry"]
    # (synthetic: not a firmware capture; every optional capability is switched on)
    state.update(
        {
            "hs": True,  # hardware scale -> Tare Scale button
            "cp": True, "cd": True,  # pressure + dimming -> puck sensors
            "htl": True, "tlo": True,  # tank switch + reading -> Tank Level Low
            "hsl": True, "sbl": False,  # steam level sensor + reading -> Steam Boiler Low
            "bta": 1, "gtd": 25000, "gtv": 40.0, "gt": 0, "gact": 0,  # grind entities
            "sbat": 80, "scaleSource": "inactive",
            "up": False,
            "sys": {"s": "ready", "m": "", "c": 0},
        }
    )
    state["warn"] = [
        {"k": key, "l": 1, "a": False}
        for key in (
            "water", "temperature", "switch", "flush", "scaleConnected", "scaleBattery", "backflush", "descaling"
        )
    ]
    telemetry.update(
        {
            "db": True, "cst": 125.3, "tst": 145, "lnk": "ble", "rssi": -60, "lat": 20, "rtx": 0,
            "hp": 12.5, "pw": 0, "pkr": 0, "pf": 0, "tf": 0, "wp": 0,
        }
    )
    return state, telemetry


def _parse_readme() -> tuple[dict[Key, bool], list[str]]:
    """Return ({(platform, name): enabled_by_default}, ambiguities) from the README entity tables."""
    entities: dict[Key, bool] = {}
    problems: list[str] = []
    section: str | None = None
    columns: list[str] = []
    skip_table = False
    in_entities = False
    for line in README.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^(#{2,3}) (.+?)\s*$", line)
        if heading:
            if heading.group(1) == "##":
                in_entities = heading.group(2) == "Supported Entities"
            section = heading.group(2) if in_entities else None
            columns, skip_table = [], False
            continue
        if not line.startswith("|"):
            columns, skip_table = [], False  # a table ends at the first non-table line
            continue
        if section not in SECTION_PLATFORM or skip_table:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not columns:
            columns = cells
            if ENABLED_COLUMN not in columns:
                if "Event type" in columns:
                    skip_table = True  # attribute reference table, not an entity list
                else:
                    problems.append(f"{section}: table header lacks {ENABLED_COLUMN!r}: {columns}")
                    skip_table = True
            elif columns[0] != "Entity":
                problems.append(f"{section}: first column is {columns[0]!r}, expected 'Entity'")
            continue
        if set("".join(cells)) <= set("-: "):
            continue  # separator row
        if len(cells) != len(columns):
            problems.append(f"{section}: row has {len(cells)} cells, header has {len(columns)}: {line[:60]}")
            continue
        name = cells[0]
        enabled = cells[columns.index(ENABLED_COLUMN)]
        if enabled not in ("Yes", "No"):
            problems.append(f"{section}: {name!r} has {ENABLED_COLUMN} = {enabled!r}, expected Yes or No")
            continue
        key = (SECTION_PLATFORM[section], name)
        if key in entities:
            problems.append(f"{section}: {key} documented twice")
        entities[key] = enabled == "Yes"
    return entities, problems


async def test_readme_entity_tables_match_registry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    """Registry entities of a fully capable machine == entities documented in README.md."""
    fake_gaggimate.state, fake_gaggimate.telemetry = _synthetic_full_frames()
    await setup_integration()

    documented, problems = _parse_readme()
    assert not problems, "README entity tables could not be parsed unambiguously: " + "; ".join(problems)
    assert documented, "no entity rows found in README.md"

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    actual: dict[Key, er.RegistryEntry] = {}
    for entry in entries:
        name = entry.original_name or entry.name
        assert isinstance(name, str), f"{entry.entity_id} has no name"
        key = (entry.domain, name)
        assert key not in actual, f"two registry entities share platform+name {key}"
        actual[key] = entry

    undocumented = sorted(set(actual) - set(documented))
    phantom = sorted(set(documented) - set(actual))
    assert not undocumented, f"entities created by the code but missing in README.md: {undocumented}"
    assert not phantom, f"entities documented in README.md but not created: {phantom}"

    # default-enabled flag
    wrong: list[str] = []
    for key, doc_enabled in documented.items():
        disabled_by = actual[key].disabled_by
        if disabled_by not in (None, er.RegistryEntryDisabler.INTEGRATION):
            wrong.append(f"{key}: unexpected disabled_by {disabled_by}")
        elif doc_enabled != (disabled_by is None):
            wrong.append(f"{key}: README says enabled={doc_enabled}, registry disabled_by={disabled_by}")
    assert not wrong, "default-enabled state differs from README.md: " + "; ".join(wrong)


def test_readme_parser_is_not_vacuous() -> None:
    """The parser finds the known tables (guards against a README restructure silently emptying it)."""
    documented, problems = _parse_readme()
    assert not problems
    platforms = {platform for platform, _ in documented}
    assert platforms == {"sensor", "binary_sensor", "switch", "select", "number", "button", "event"}
    assert ("sensor", "Current Temperature") in documented
    assert ("event", "Shot Events") in documented
    assert ("button", "Start Backflush") in documented
    assert documented[("sensor", "Puck Resistance")] is False  # No = disabled by default
