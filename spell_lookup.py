"""
SPELL LOOKUP (for on-equip "invisible enchant" item spells)
=============================================================
Some items grant a stat via an attached on-equip spell instead of a normal
stat_type/stat_value slot (e.g. Rage Reaver's Attack Power comes from
spell 15808, "Attack Power 38", not a literal stat line). This module reads
a local Spell.csv (a flat export of Spell.dbc - NOT a database table) and
classifies each spell's effects into an equivalent (ItemModType, magnitude)
pair wherever that's a safe, unambiguous conversion.

All numeric constants below were pulled directly from AzerothCore's own
source (SharedDefines.h, SpellAuraDefines.h, ItemTemplate.h) rather than
recalled from memory, specifically to avoid silently mislabeling an aura.

Recognized conversions (deliberately conservative - anything not on this
list is flagged as unhandled rather than guessed at):
  SPELL_AURA_MOD_STAT (29)               -> ITEM_MOD_STRENGTH/AGILITY/STAMINA/
                                             INTELLECT/SPIRIT (via the Stats
                                             index in EffectMiscValue; -1 means
                                             all five stats)
  SPELL_AURA_MOD_ATTACK_POWER (99)       -> ITEM_MOD_ATTACK_POWER
  SPELL_AURA_MOD_RANGED_ATTACK_POWER(124)-> ITEM_MOD_RANGED_ATTACK_POWER
  SPELL_AURA_MOD_DAMAGE_DONE (13) or
  SPELL_AURA_MOD_HEALING_DONE (135),
  when the school mask is magic (not pure physical) -> ITEM_MOD_SPELL_POWER
  (Wrath consolidated the old separate spell-damage/spell-healing item mods
  into one Spell Power stat - ITEM_MOD_SPELL_DAMAGE_DONE/HEALING_DONE are
  marked deprecated in AzerothCore's own header for exactly this reason.)

Everything else (combat rating bitmasks, resistances, procs, non-aura
effects) is left as an unrecognized on-equip spell and flagged for manual
review rather than converted - a wrong guess here is worse than flagging it.
"""
import csv
import os

# --- Verified against AzerothCore source (see module docstring) ---
SPELL_EFFECT_APPLY_AURA = 6

SPELL_AURA_MOD_STAT = 29
SPELL_AURA_MOD_ATTACK_POWER = 99
SPELL_AURA_MOD_RANGED_ATTACK_POWER = 124
SPELL_AURA_MOD_DAMAGE_DONE = 13
SPELL_AURA_MOD_HEALING_DONE = 135

STAT_TO_ITEM_MOD = {
    0: 4,  # STAT_STRENGTH  -> ITEM_MOD_STRENGTH
    1: 3,  # STAT_AGILITY   -> ITEM_MOD_AGILITY
    2: 7,  # STAT_STAMINA   -> ITEM_MOD_STAMINA
    3: 5,  # STAT_INTELLECT -> ITEM_MOD_INTELLECT
    4: 6,  # STAT_SPIRIT    -> ITEM_MOD_SPIRIT
}
ALL_STATS_ITEM_MODS = list(STAT_TO_ITEM_MOD.values())

ITEM_MOD_ATTACK_POWER = 38
ITEM_MOD_RANGED_ATTACK_POWER = 39
ITEM_MOD_SPELL_POWER = 45

SPELL_SCHOOL_MASK_PHYSICAL = 1

ITEM_SPELLTRIGGER_ON_EQUIP = 1

# Only these columns are kept per spell row - the rest of Spell.csv's ~150
# columns are discarded immediately during parsing to keep memory sane
# across ~80k rows.
_WANTED_SUFFIXES = (
    "Effect_1", "Effect_2", "Effect_3",
    "EffectAura_1", "EffectAura_2", "EffectAura_3",
    "EffectBasePoints_1", "EffectBasePoints_2", "EffectBasePoints_3",
    "EffectMiscValue_1", "EffectMiscValue_2", "EffectMiscValue_3",
    "Name_Lang_enUS",
)


def load_spell_index(path):
    """Reads Spell.csv into a dict keyed by spell ID (int). Returns None if
    the file doesn't exist (callers should treat that as 'feature unavailable'
    rather than crash, since this is optional local data, not a DB table)."""
    if not os.path.exists(path):
        return None

    index = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        id_col = "ID" if "ID" in reader.fieldnames else "Id"
        for row in reader:
            try:
                spell_id = int(row[id_col])
            except (ValueError, KeyError):
                continue
            trimmed = {}
            for col in _WANTED_SUFFIXES:
                val = row.get(col, "")
                if col.startswith("Name_Lang"):
                    trimmed[col] = val
                else:
                    try:
                        trimmed[col] = int(val) if val not in ("", None) else 0
                    except ValueError:
                        trimmed[col] = 0
            index[spell_id] = trimmed
    return index


def classify_spell_stats(spell_row):
    """Given a loaded spell row, returns (converted, unhandled) where:
      converted = list of (item_mod_type, magnitude) for recognized effects
      unhandled = list of human-readable descriptions of effects we didn't
                  convert (e.g. combat rating, resistance, proc effects)
    """
    converted = []
    unhandled = []

    for k in (1, 2, 3):
        effect = spell_row.get(f"Effect_{k}", 0)
        if effect != SPELL_EFFECT_APPLY_AURA:
            continue
        aura = spell_row.get(f"EffectAura_{k}", 0)
        if aura == 0:
            continue
        base_points = spell_row.get(f"EffectBasePoints_{k}", 0)
        misc = spell_row.get(f"EffectMiscValue_{k}", 0)
        magnitude = base_points + 1  # DBC convention: stored value is real value minus 1

        if aura == SPELL_AURA_MOD_STAT:
            if misc == -1:
                for item_mod in ALL_STATS_ITEM_MODS:
                    converted.append((item_mod, magnitude))
            elif misc in STAT_TO_ITEM_MOD:
                converted.append((STAT_TO_ITEM_MOD[misc], magnitude))
            else:
                unhandled.append(f"effect {k}: MOD_STAT with unrecognized stat index {misc}")
        elif aura == SPELL_AURA_MOD_ATTACK_POWER:
            converted.append((ITEM_MOD_ATTACK_POWER, magnitude))
        elif aura == SPELL_AURA_MOD_RANGED_ATTACK_POWER:
            converted.append((ITEM_MOD_RANGED_ATTACK_POWER, magnitude))
        elif aura in (SPELL_AURA_MOD_DAMAGE_DONE, SPELL_AURA_MOD_HEALING_DONE):
            # Only auto-convert if it's a magic-school (not pure physical) bonus -
            # Wrath itemization folds these into one Spell Power stat.
            if misc != SPELL_SCHOOL_MASK_PHYSICAL and misc != 0:
                converted.append((ITEM_MOD_SPELL_POWER, magnitude))
            else:
                unhandled.append(f"effect {k}: physical damage/healing bonus, no clean item-mod equivalent")
        else:
            unhandled.append(f"effect {k}: aura type {aura} not in the recognized conversion list")

    return converted, unhandled


# Spell.csv (a WDBX-style flat DBC export) and the real spell_dbc TABLE use
# different names for a handful of columns, and each has a few fields the
# other doesn't. Confirmed directly by diffing the two real files - not
# assumed. Rows read from Spell.csv are translated to spell_dbc's actual
# column names before being used to build any spell_dbc INSERT, so we never
# try to write to a column that doesn't exist there.
SPELL_CSV_TO_DBC_RENAME = {
    "AttributesExB": "AttributesEx2",
    "AttributesExC": "AttributesEx3",
    "AttributesExD": "AttributesEx4",
    "AttributesExE": "AttributesEx5",
    "AttributesExF": "AttributesEx6",
    "AttributesExG": "AttributesEx7",
}
# Present in Spell.csv but with no corresponding column in spell_dbc at all -
# dropped rather than attempted, which would be a hard SQL error.
SPELL_CSV_ONLY_FIELDS_TO_DROP = {"Field227", "Field228", "Field229"}
# Present in spell_dbc but with no source data in Spell.csv (introduced in a
# later DBC revision than what this flat export covers) - intentionally left
# out of any fresh INSERT we build, so MySQL applies the table's own column
# defaults rather than us guessing a value with no real source for it.


# Exact FLOAT-typed columns in spell_dbc, taken directly from the
# authoritative CREATE TABLE schema (Spell_dbc_table_schema.txt) - every
# other numeric column in the table is INT/BIGINT/UNSIGNED. Confirmed
# EffectRealPointsPerLevel_1/2/3 are the ONLY three columns in the whole
# table with no DEFAULT clause, so they must always receive a valid number.
SPELL_DBC_FLOAT_COLUMNS = {
    "Speed",
    "EffectRealPointsPerLevel_1", "EffectRealPointsPerLevel_2", "EffectRealPointsPerLevel_3",
    "EffectMultipleValue_1", "EffectMultipleValue_2", "EffectMultipleValue_3",
    "EffectPointsPerCombo_1", "EffectPointsPerCombo_2", "EffectPointsPerCombo_3",
    "EffectChainAmplitude_1", "EffectChainAmplitude_2", "EffectChainAmplitude_3",
    "EffectBonusMultiplier_1", "EffectBonusMultiplier_2", "EffectBonusMultiplier_3",
}
# Free-text columns (localized names/descriptions) - every other column in
# the table is numeric, so anything NOT starting with one of these prefixes
# gets treated as a numeric column by _normalize_spell_dbc_value.
SPELL_DBC_TEXT_COLUMN_PREFIXES = ("Name_Lang_", "NameSubtext_Lang_", "Description_Lang_", "AuraDescription_Lang_")


def _normalize_spell_dbc_value(col, val):
    """Ensures a value bound for a numeric spell_dbc column is a clean
    Python number, never a locale-formatted string like "2,5" - applied to
    EVERY numeric column in the row, not just the ones this tool is
    actually nerfing. Spell.csv's export uses the exporting machine's
    locale for ALL numeric fields, so an untouched field (e.g. a different
    effect slot's EffectRealPointsPerLevel) can just as easily trip up the
    SQL layer as the ones we're deliberately changing - this was the actual
    gap in the earlier, narrower fix (which only normalized the specific
    fields being nerfed)."""
    if any(col.startswith(p) for p in SPELL_DBC_TEXT_COLUMN_PREFIXES):
        return val  # free text, never touched
    if col in SPELL_DBC_FLOAT_COLUMNS:
        return _safe_float(val, 0.0)
    return int(_safe_float(val, 0))  # every other column is INT/BIGINT/UNSIGNED


def _translate_spell_csv_row_to_dbc_columns(row):
    translated = {}
    for col, val in row.items():
        if col in SPELL_CSV_ONLY_FIELDS_TO_DROP:
            continue
        dbc_col = SPELL_CSV_TO_DBC_RENAME.get(col, col)
        translated[dbc_col] = _normalize_spell_dbc_value(dbc_col, val)
    return translated


def load_full_spell_rows(path, needed_ids):
    """Single targeted pass over Spell.csv, returning FULL rows (translated
    to spell_dbc's actual column names - see SPELL_CSV_TO_DBC_RENAME) for
    only the given spell IDs. Used as the INSERT source when spell_dbc
    doesn't already have an override row for that ID - load_spell_index()
    only keeps a small classification subset, this is for when we need
    every column to build a valid, complete spell_dbc row.

    Rows fetched live from spell_dbc itself (spell_nerf.fetch_spell_dbc_rows,
    for custom server-only spells) need NO translation - they already use
    the table's real column names since they come from the table directly."""
    if not os.path.exists(path) or not needed_ids:
        return {}
    needed = set(needed_ids)
    found = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        id_col = "ID" if "ID" in reader.fieldnames else "Id"
        for row in reader:
            try:
                spell_id = int(row[id_col])
            except (ValueError, KeyError):
                continue
            if spell_id in needed:
                found[spell_id] = _translate_spell_csv_row_to_dbc_columns(row)
                if len(found) == len(needed):
                    break
    return found


def _safe_float(v, default=0.0):
    """Parses a float defensively, tolerating European-locale decimal commas
    (e.g. "2,5" meaning 2.5) that can show up in CSV exports produced on a
    non-English-locale machine."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return default
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return default


def build_nerfed_spell_row(base_row, effect_changes):
    """base_row: a full spell_dbc/Spell.csv row (dict, string or numeric
    values - either is fine, we only touch the specific effect columns).
    effect_changes: list of (slot, old_magnitude, new_magnitude) from
    spell_nerf_math's classification + nerf calculation.

    Returns a NEW dict (copy of base_row) with EffectBasePoints_N rewritten
    to the new magnitude, and EffectDieSides_N/EffectRealPointsPerLevel_N
    scaled by the same ratio if the original spell had any roll variance or
    per-level scaling on that effect - so a spell that rolled a range, or
    auto-scaled with caster level, keeps doing so proportionally rather than
    being flattened to a single fixed number."""
    new_row = dict(base_row)
    for slot, old_magnitude, new_magnitude in effect_changes:
        ratio = (new_magnitude / old_magnitude) if old_magnitude else 1.0

        new_row[f"EffectBasePoints_{slot}"] = new_magnitude - 1  # DBC convention

        die_sides_col = f"EffectDieSides_{slot}"
        old_die_sides = int(_safe_float(base_row.get(die_sides_col, 1), 1))
        if old_die_sides > 1:
            new_row[die_sides_col] = max(1, round(old_die_sides * ratio))

        ppl_col = f"EffectRealPointsPerLevel_{slot}"
        old_ppl = _safe_float(base_row.get(ppl_col, 0), 0)
        if old_ppl:
            new_row[ppl_col] = old_ppl * ratio
    return new_row


def spell_dbc_upsert_sql(row, all_columns, id_col="ID"):
    """Builds a full INSERT ... ON DUPLICATE KEY UPDATE statement covering
    every column in all_columns, for when spell_dbc has no existing override
    row for this spell ID yet (needs a complete row, not a partial UPDATE)."""
    values = [row.get(c) for c in all_columns]
    collist = ", ".join(f"`{c}`" for c in all_columns)
    placeholders = ", ".join(["%s"] * len(all_columns))
    update_clause = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in all_columns if c != id_col)
    stmt = (f"INSERT INTO spell_dbc ({collist}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {update_clause}")
    return stmt, values


def spell_dbc_targeted_update_sql(spell_id, effect_changes, id_col="ID"):
    """Builds a narrow UPDATE touching ONLY the changed effect columns, for
    when spell_dbc already has an override row for this spell ID - preserves
    whatever else that existing override customized rather than replacing
    the whole row from Spell.csv."""
    clauses, params = [], []
    for slot, old_magnitude, new_magnitude in effect_changes:
        ratio = (new_magnitude / old_magnitude) if old_magnitude else 1.0
        clauses.append(f"`EffectBasePoints_{slot}` = %s")
        params.append(new_magnitude - 1)
        clauses.append(f"`EffectDieSides_{slot}` = GREATEST(1, ROUND(`EffectDieSides_{slot}` * %s))")
        params.append(ratio)
        clauses.append(f"`EffectRealPointsPerLevel_{slot}` = `EffectRealPointsPerLevel_{slot}` * %s")
        params.append(ratio)
    stmt = f"UPDATE spell_dbc SET {', '.join(clauses)} WHERE `{id_col}` = %s"
    params.append(spell_id)
    return stmt, params



    # Standalone sanity check - this module has no other CLI, it's normally
    # only imported by item_scaler.py. Run this directly to confirm Spell.csv
    # is being found and parsed correctly before running the full pipeline.
    #
    # Usage:
    #   python spell_lookup.py                  uses config.SPELL_CSV_PATH
    #   python spell_lookup.py path\to\Spell.csv uses an explicit path
    #   python spell_lookup.py path\to\Spell.csv 15808   also classifies spell 15808
    import sys

    if len(sys.argv) >= 2:
        csv_path = sys.argv[1]
    else:
        try:
            from config import SPELL_CSV_PATH
            csv_path = SPELL_CSV_PATH
        except ImportError:
            csv_path = "Spell.csv"

    print(f"Looking for Spell.csv at: {csv_path}")
    print(f"File exists: {os.path.exists(csv_path)}")

    index = load_spell_index(csv_path)
    if index is None:
        print("RESULT: load_spell_index() returned None - the file was not found at that path.")
        print("Check the path above is exactly right (including the .csv extension and any typos).")
        sys.exit(1)

    print(f"RESULT: loaded {len(index)} spells successfully.")
    sample_ids = list(index.keys())[:5]
    print(f"Sample spell IDs loaded: {sample_ids}")

    if len(sys.argv) >= 3:
        try:
            spell_id = int(sys.argv[2])
        except ValueError:
            print(f"'{sys.argv[2]}' isn't a valid integer spell ID.")
            sys.exit(1)
        row = index.get(spell_id)
        if row is None:
            print(f"\nSpell {spell_id} was not found in the loaded index.")
        else:
            print(f"\nSpell {spell_id} ({row.get('Name_Lang_enUS', '?')}): {row}")
            converted, unhandled = classify_spell_stats(row)
            print(f"Converted stats: {converted}")
            print(f"Unhandled effects: {unhandled}")
