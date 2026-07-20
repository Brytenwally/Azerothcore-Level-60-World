"""
SPELL NERF BRAIN
=================
Finds every damage/heal spell cast by creatures already in
plans/creature_plan.json (from brain.py) - via SmartAI (smart_scripts, the
usual path for trash mobs) or hardcoded boss scripts (.cpp files in the
scripts/ folder, since boss casting logic in AzerothCore lives in compiled
C++, not the database) - and produces plans/spell_plan.json: a nerfed
version of each spell's damage/heal effect(s), scaled to match the caster's
new (downscaled) level.

Read-only against the DB. Reads the scripts/ folder and Spell.csv/
HP_Values.ods from local disk. Requires plans/creature_plan.json to already
exist (run brain.py first).

Design decisions carried over from discussion with the user:
  - Same-ID overwrite in spell_dbc (no cloning) - spell_dbc entries override
    the native Spell.dbc file for that ID everywhere it's referenced, so
    this nerfs the spell globally, not just for the target creature. This is
    accepted as intentional for simplicity ("one database update should fix
    everything") - out-of-scope sharing is flagged for visibility
    (has_outside_caster) but does NOT block the nerf.
  - When the same spell ID is cast by multiple in-scope creatures at
    different levels ("duplicates"), the HIGHEST original level among them
    is used as the basis for the nerf calculation (same "higher wins"
    pattern used for ambiguous item levels) - expected to be rare.
"""
import json
import os
from collections import defaultdict

from config import PLAN_DIR, SPELL_CSV_PATH, HP_VALUES_PATH, BOSS_SCRIPTS_DIR, SPELL_NERF_STATE_TABLE
from db import get_connection, table_columns, table_exists, resolve_column, get_processed_ids
import spell_lookup
import spell_nerf_math as snm
import boss_script_parser as bsp


def load_creature_plan():
    path = os.path.join(PLAN_DIR, "creature_plan.json")
    if not os.path.exists(path):
        raise RuntimeError(f"{path} not found - run brain.py first.")
    with open(path) as f:
        return json.load(f)


def target_creature_levels(creature_plan):
    """Returns {entry: {"old_level":X, "new_level":Y}} for every creature
    already in the plan (simple + cloned)."""
    levels = {}
    for rec in creature_plan.get("simple", []) + creature_plan.get("cloned", []):
        levels[rec["entry"]] = {"old_level": rec["old_minlevel"], "new_level": rec["new_minlevel"]}
    return levels


def _split_target_vs_outside(spell_to_all_casters, target_entries):
    target_set = set(target_entries)
    spell_to_target_casters = defaultdict(set)
    spell_has_outside_caster = defaultdict(bool)
    for spell_id, casters in spell_to_all_casters.items():
        target_hit = casters & target_set
        if target_hit:
            spell_to_target_casters[spell_id] = target_hit
            spell_has_outside_caster[spell_id] = bool(casters - target_set)
    return spell_to_target_casters, spell_has_outside_caster


def gather_smartai_casts(conn, target_entries):
    """Returns (spell_to_target_casters, spell_has_outside_caster):
      spell_to_target_casters: spell_id -> set(creature_template entries in
        our target set) that cast it via SMART_ACTION_CAST.
      spell_has_outside_caster: spell_id -> True if ANY caster (via SmartAI)
        of this spell is NOT in our target set.
    Handles negative entryorguid (a SmartAI row pinned to one specific spawn
    rather than every copy of a template) by resolving it back to a
    creature_template entry via creature.guid."""
    empty = (defaultdict(set), defaultdict(bool))
    if not table_exists(conn, "smart_scripts"):
        return empty

    ss_cols = table_columns(conn, "smart_scripts")
    entry_col = resolve_column(ss_cols, ["entryorguid"], context="smart_scripts entry/guid")
    source_col = resolve_column(ss_cols, ["source_type"], context="smart_scripts source type")
    action_type_col = resolve_column(ss_cols, ["action_type"], context="smart_scripts action type")
    action_param1_col = resolve_column(ss_cols, ["action_param1"], context="smart_scripts action param1")

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {entry_col} AS entryorguid, {action_param1_col} AS spell_id FROM smart_scripts "
            f"WHERE {source_col} = 0 AND {action_type_col} = 11 AND {action_param1_col} > 0"
        )
        rows = cur.fetchall()
    if not rows:
        return empty

    negative_guids = {-r["entryorguid"] for r in rows if r["entryorguid"] < 0}
    guid_to_entry = {}
    if negative_guids and table_exists(conn, "creature"):
        c_cols = table_columns(conn, "creature")
        guid_col = resolve_column(c_cols, ["guid", "GUID"], context="creature guid")
        id_col = resolve_column(c_cols, ["id1", "id"], context="creature spawn template id")
        with conn.cursor() as cur:
            cur.execute(f"SELECT {guid_col} AS guid, {id_col} AS entry FROM creature "
                        f"WHERE {guid_col} IN ({','.join(str(g) for g in negative_guids)})")
            for row in cur.fetchall():
                guid_to_entry[row["guid"]] = row["entry"]

    spell_to_all_casters = defaultdict(set)
    for r in rows:
        eg = r["entryorguid"]
        entry = eg if eg > 0 else guid_to_entry.get(-eg)
        if entry:
            spell_to_all_casters[r["spell_id"]].add(entry)

    return _split_target_vs_outside(spell_to_all_casters, target_entries)


def gather_boss_script_casts(conn, target_entries):
    """Parses scripts/*.cpp, matches each struct name against
    creature_template.ScriptName, and returns the same shape as
    gather_smartai_casts, plus the raw parsed file list for reporting."""
    parsed = bsp.parse_cpp_folder(BOSS_SCRIPTS_DIR)
    script_names = sorted({p["script_name"] for p in parsed if p["script_name"]})
    if not script_names:
        return defaultdict(set), defaultdict(bool), parsed

    ct_cols = table_columns(conn, "creature_template")
    entry_col = resolve_column(ct_cols, ["entry", "Entry"], context="creature_template PK")
    script_col = resolve_column(ct_cols, ["ScriptName", "scriptname"], context="creature_template ScriptName")

    with conn.cursor() as cur:
        placeholders = ", ".join(["%s"] * len(script_names))
        cur.execute(
            f"SELECT {script_col} AS script_name, {entry_col} AS entry FROM creature_template "
            f"WHERE {script_col} IN ({placeholders})",
            script_names,
        )
        script_to_entries = defaultdict(set)
        for row in cur.fetchall():
            script_to_entries[row["script_name"]].add(row["entry"])

    spell_to_all_casters = defaultdict(set)
    for p in parsed:
        if not p["script_name"]:
            continue
        entries = script_to_entries.get(p["script_name"], set())
        for spell_id in p["spell_ids"]:
            spell_to_all_casters[spell_id] |= entries

    spell_to_target, spell_outside = _split_target_vs_outside(spell_to_all_casters, target_entries)
    return spell_to_target, spell_outside, parsed


def get_mob_hp_ratio(conn, unit_class, old_level, new_level, cache):
    """ratio = creature_classlevelstats[new_level][class].basehp /
               creature_classlevelstats[old_level][class].basehp
    HealthModifier cancels out (unchanged on either side of the ratio), so
    this matches how the creature's actual in-game HP will change - same
    technique used elsewhere in this project for reasoning purposes, just
    actually queried this time since we need the ratio ourselves for heal
    scaling, rather than letting the engine recompute HP on its own as
    happens for the creature's own health.

    NOTE: this is the least-verified piece of the new engine - the exact HP
    column name on creature_classlevelstats hasn't been confirmed against a
    real export the way every other table in this project has been. Falls
    back to ratio=1.0 (no heal scaling) if the table/columns can't be
    resolved, rather than guessing wrong - check this carefully against
    your first real run.
    """
    key = (unit_class, old_level, new_level)
    if key in cache:
        return cache[key]

    if not table_exists(conn, "creature_classlevelstats"):
        cache[key] = 1.0
        return 1.0

    cls_cols = table_columns(conn, "creature_classlevelstats")
    level_col = resolve_column(cls_cols, ["level", "Level"], required=False)
    class_col = resolve_column(cls_cols, ["class", "Class"], required=False)
    hp_col = resolve_column(cls_cols, ["basehp0", "BaseHealth", "basehp", "BaseHP"], required=False)
    if not (level_col and class_col and hp_col):
        cache[key] = 1.0
        return 1.0

    with conn.cursor() as cur:
        cur.execute(f"SELECT {hp_col} AS hp FROM creature_classlevelstats WHERE {level_col}=%s AND {class_col}=%s",
                    (old_level, unit_class))
        old_row = cur.fetchone()
        cur.execute(f"SELECT {hp_col} AS hp FROM creature_classlevelstats WHERE {level_col}=%s AND {class_col}=%s",
                    (new_level, unit_class))
        new_row = cur.fetchone()

    if not old_row or not new_row or not old_row.get("hp"):
        ratio = 1.0
    else:
        ratio = new_row["hp"] / old_row["hp"]
    cache[key] = ratio
    return ratio


def fetch_spell_dbc_rows(conn, spell_ids, id_col="ID"):
    """Live fallback for spell IDs not found in Spell.csv - these are almost
    always custom server additions that never existed in native Spell.dbc at
    all (as opposed to a modification of an existing spell), so they can
    ONLY be found in the spell_dbc table itself, not the DBC export.
    Returns {spell_id: row_dict} using the DB's native Python types (ints/
    floats, not strings) - classify_spell_effects/build_nerfed_spell_row
    already tolerate either since they check isinstance before parsing."""
    if not spell_ids or not table_exists(conn, "spell_dbc"):
        return {}
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM spell_dbc WHERE {id_col} IN "
                    f"({','.join(str(int(s)) for s in spell_ids)})")
        return {row[id_col]: row for row in cur.fetchall()}


def analyze_spells(conn):
    creature_plan = load_creature_plan()
    levels_by_entry = target_creature_levels(creature_plan)
    target_entries = list(levels_by_entry.keys())

    if not target_entries:
        return {"spells": [], "note": "No creatures found in creature_plan.json - nothing to do."}

    print(f"  {len(target_entries)} target creatures loaded from creature_plan.json")

    print("  Scanning smart_scripts for SMART_ACTION_CAST rows...")
    smartai_casters, smartai_outside = gather_smartai_casts(conn, target_entries)
    print(f"  Found {len(smartai_casters)} distinct spells cast by target creatures via SmartAI")

    print(f"  Parsing boss scripts in {BOSS_SCRIPTS_DIR} ...")
    boss_casters, boss_outside, parsed_files = gather_boss_script_casts(conn, target_entries)
    print(f"  Parsed {len(parsed_files)} .cpp file(s), found {len(boss_casters)} distinct spells "
          f"cast by target creatures via boss scripts")
    inline_only_files = [p["file"] for p in parsed_files if p["has_inline_numeric_casts"]]
    if inline_only_files:
        print(f"  [WARN] {len(inline_only_files)} boss script(s) use inline numeric spell IDs instead of "
              f"an enum and were NOT parsed for spells - review manually: {inline_only_files}")

    all_spell_ids = set(smartai_casters) | set(boss_casters)
    spell_to_casters = defaultdict(set)
    spell_has_outside = defaultdict(bool)
    for spell_id in all_spell_ids:
        spell_to_casters[spell_id] = smartai_casters.get(spell_id, set()) | boss_casters.get(spell_id, set())
        spell_has_outside[spell_id] = smartai_outside.get(spell_id, False) or boss_outside.get(spell_id, False)

    print(f"  {len(all_spell_ids)} distinct spells total across both sources - loading full spell data...")
    full_rows = spell_lookup.load_full_spell_rows(SPELL_CSV_PATH, all_spell_ids)
    missing_from_csv = all_spell_ids - set(full_rows.keys())
    if missing_from_csv:
        print(f"  {len(missing_from_csv)} spell ID(s) not found in Spell.csv - these are likely custom "
              f"server additions that only ever existed in spell_dbc, never in native Spell.dbc. "
              f"Checking spell_dbc directly...")
        dbc_fallback_rows = fetch_spell_dbc_rows(conn, missing_from_csv)
        full_rows.update(dbc_fallback_rows)
        print(f"  Found {len(dbc_fallback_rows)} of those directly in spell_dbc.")
        still_missing = missing_from_csv - set(dbc_fallback_rows.keys())
        if still_missing:
            preview = sorted(still_missing)[:20]
            print(f"  [WARN] {len(still_missing)} spell ID(s) not found in EITHER Spell.csv or spell_dbc - "
                  f"these genuinely can't be nerfed: {preview}" + (" ..." if len(still_missing) > 20 else ""))

    hp_curve = snm.load_hp_curve(HP_VALUES_PATH)
    if hp_curve is None:
        print(f"  [WARN] HP_Values.ods not found/unreadable at {HP_VALUES_PATH} - damage spells "
              f"cannot be nerfed without it (healing spells are unaffected by this).")
    else:
        print(f"  HP_Values.ods loaded ({len(hp_curve)} level nodes).")

    ct_cols = table_columns(conn, "creature_template")
    ct_entry_col = resolve_column(ct_cols, ["entry", "Entry"], context="creature_template PK")
    ct_class_col = resolve_column(ct_cols, ["unit_class", "UnitClass"], required=False)
    unit_classes = {}
    if ct_class_col:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {ct_entry_col} AS entry, {ct_class_col} AS unit_class FROM creature_template "
                        f"WHERE {ct_entry_col} IN ({','.join(str(e) for e in target_entries)})")
            for row in cur.fetchall():
                unit_classes[row["entry"]] = row["unit_class"] or 1

    processed_ids = get_processed_ids(conn, SPELL_NERF_STATE_TABLE)
    hp_ratio_cache = {}
    already_processed = []
    plan = []
    skipped_no_data = []
    skipped_not_damage_or_heal = []

    for spell_id in sorted(all_spell_ids):
        if spell_id in processed_ids:
            already_processed.append(spell_id)
            continue

        base_row = full_rows.get(spell_id)
        if not base_row:
            skipped_no_data.append(spell_id)
            continue

        effects = snm.classify_spell_effects(base_row)
        if not effects:
            skipped_not_damage_or_heal.append(spell_id)
            continue

        casters = spell_to_casters[spell_id]
        level_pairs = sorted(
            {(levels_by_entry[c]["old_level"], levels_by_entry[c]["new_level"]) for c in casters},
            reverse=True,
        )
        old_level, new_level = level_pairs[0]  # "higher wins" for duplicate casters
        notes = []
        if len(level_pairs) > 1:
            notes.append(f"Cast by {len(casters)} different creatures at {len(level_pairs)} different "
                          f"levels {level_pairs} - used the highest ({old_level}->{new_level}) as basis.")

        effect_changes = []
        for slot, kind, old_magnitude in effects:
            if kind == "damage":
                if hp_curve is None:
                    notes.append(f"effect {slot}: damage, but no HP curve available - left unchanged")
                    continue
                new_magnitude = snm.nerf_damage_magnitude(old_magnitude, old_level, new_level, hp_curve)
            else:  # heal - scales off the mob's OWN hp ratio, not the player curve
                rep_entry = next(iter(casters))  # representative caster for unit_class in the rare multi-caster case
                unit_class = unit_classes.get(rep_entry, 1)
                ratio = get_mob_hp_ratio(conn, unit_class, old_level, new_level, hp_ratio_cache)
                new_magnitude = snm.nerf_heal_magnitude(old_magnitude, old_mob_hp=1.0, new_mob_hp=ratio)
            if new_magnitude is not None:
                effect_changes.append((slot, old_magnitude, new_magnitude))

        if not effect_changes:
            skipped_not_damage_or_heal.append(spell_id)
            continue

        if spell_has_outside[spell_id]:
            notes.append("This spell is ALSO cast by at least one creature OUTSIDE the current target "
                          "set (open world/dungeons being downscaled) - overwriting spell_dbc nerfs it "
                          "there too. Flagged for visibility, not blocked.")

        changed_slots = {s for s, _, _ in effect_changes}
        magnitude_by_slot = {s: nm for s, _, nm in effect_changes}
        kind_by_slot = {s: k for s, k, _ in effects}
        new_row = spell_lookup.build_nerfed_spell_row(base_row, effect_changes)
        plan.append({
            "spell_id": spell_id,
            "name": base_row.get("Name_Lang_enUS", "?"),
            "caster_entries": sorted(casters),
            "basis_old_level": old_level,
            "basis_new_level": new_level,
            "effect_changes": [
                {"slot": s, "kind": kind_by_slot[s], "old_magnitude": om, "new_magnitude": magnitude_by_slot[s]}
                for s, om, _ in effect_changes
            ],
            "has_outside_caster": spell_has_outside[spell_id],
            "note": " | ".join(notes) if notes else None,
            "new_row": new_row,
        })

    return {
        "spells": plan,
        "already_processed_count": len(already_processed),
        "skipped_no_spell_data_count": len(skipped_no_data),
        "skipped_no_spell_data_ids": skipped_no_data[:50],
        "skipped_not_damage_or_heal_count": len(skipped_not_damage_or_heal),
        "unmatched_boss_scripts": [p["file"] for p in parsed_files if not p["script_name"]],
        "inline_only_boss_scripts": inline_only_files,
    }


def main():
    os.makedirs(PLAN_DIR, exist_ok=True)
    with get_connection() as conn:
        print("Analyzing creature-cast spells...")
        spell_plan = analyze_spells(conn)

    with open(os.path.join(PLAN_DIR, "spell_plan.json"), "w") as f:
        json.dump(spell_plan, f, indent=2, default=str)

    print("\n=== SPELL NERF SUMMARY ===")
    print(f"  Spells to nerf:                  {len(spell_plan.get('spells', []))}")
    print(f"  Already processed (skipped):     {spell_plan.get('already_processed_count', 0)}")
    print(f"  Skipped (no data in Spell.csv):  {spell_plan.get('skipped_no_spell_data_count', 0)}")
    print(f"  Skipped (not damage/heal):       {spell_plan.get('skipped_not_damage_or_heal_count', 0)}")
    outside = [s for s in spell_plan.get("spells", []) if s.get("has_outside_caster")]
    if outside:
        print(f"  -> NOTE: {len(outside)} spells are also cast by creatures outside the target set - "
              f"see 'has_outside_caster' per spell in the plan file.")
    print(f"\nPlan written to {PLAN_DIR}/spell_plan.json")


if __name__ == "__main__":
    main()
