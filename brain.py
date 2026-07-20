"""
THE BRAIN
=========
Read-only. Never writes to the database. Connects, understands the current
state of Outland/Northrend content, and produces two reviewable JSON plans:

    plans/creature_plan.json
    plans/quest_plan.json

Run this, read the plans (and the printed summary/warnings), THEN hand the
plan files to executor.py to actually apply changes.

    python brain.py
"""
import json
import os
from collections import defaultdict

from config import (
    MAP_NAMES, LEVEL_SHIFT, MIN_SOURCE_LEVEL,
    CREATURE_TYPE_CRITTER, MIN_LEVEL_FLOOR, MIN_QUEST_LEVEL_FLOOR,
    CLONE_ENTRY_OFFSET, PLAN_DIR, CREATURE_STATE_TABLE, QUEST_STATE_TABLE,
)
from db import get_connection, table_columns, table_exists, resolve_column, get_processed_ids

# Every map id we'll consider - now includes Outland, Northrend, and every
# TBC/WotLK leveling dungeon (see config.TARGET_MAPS for the full registry
# with names/expansions). Derived from MAP_NAMES so this always stays in
# sync with config.py without needing a separate list maintained here.
TARGET_MAPS = tuple(MAP_NAMES.keys())


def clamp_level(level, shift, floor=MIN_LEVEL_FLOOR):
    return max(floor, level + shift)


# ---------------------------------------------------------------------------
# CREATURES
# ---------------------------------------------------------------------------

def analyze_creatures(conn):
    ct_cols = table_columns(conn, "creature_template")
    c_cols = table_columns(conn, "creature")

    entry_col = resolve_column(ct_cols, ["entry", "Entry", "ID"], context="creature_template PK")
    name_col = resolve_column(ct_cols, ["name", "Name"], context="creature_template name")
    minlvl_col = resolve_column(ct_cols, ["minlevel", "MinLevel"], context="creature_template minlevel")
    maxlvl_col = resolve_column(ct_cols, ["maxlevel", "MaxLevel"], context="creature_template maxlevel")
    type_col = resolve_column(ct_cols, ["type", "CreatureType"], context="creature_template type")

    spawn_id_col = resolve_column(c_cols, ["id1", "id"], context="creature spawn template id")
    spawn_map_col = resolve_column(c_cols, ["map", "Map"], context="creature spawn map")
    spawn_guid_col = resolve_column(c_cols, ["guid", "GUID", "spawnID"], context="creature spawn guid")

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {entry_col} AS entry, {name_col} AS name, {minlvl_col} AS minlevel, "
            f"{maxlvl_col} AS maxlevel, {type_col} AS type "
            f"FROM creature_template WHERE {type_col} != %s",
            (CREATURE_TYPE_CRITTER,),
        )
        templates = {row["entry"]: row for row in cur.fetchall()}

        cur.execute(
            f"SELECT {spawn_guid_col} AS guid, {spawn_id_col} AS entry, {spawn_map_col} AS map "
            f"FROM creature"
        )
        spawns = cur.fetchall()

    maps_by_entry = defaultdict(set)
    guids_by_entry_map = defaultdict(list)
    for s in spawns:
        if s["entry"] not in templates:
            continue  # critter or unknown entry, skip
        maps_by_entry[s["entry"]].add(s["map"])
        guids_by_entry_map[(s["entry"], s["map"])].append(s["guid"])

    simple, cloned, ignored_no_spawn, skipped_multi_target, skipped_below_threshold = [], [], [], [], []
    already_processed = []
    processed_ids = get_processed_ids(conn, CREATURE_STATE_TABLE)
    used_entries = set(templates.keys())
    next_clone_entry = max(used_entries, default=0) + CLONE_ENTRY_OFFSET

    for entry, tpl in templates.items():
        if entry in processed_ids:
            already_processed.append(entry)
            continue
        entry_maps = maps_by_entry.get(entry, set())
        target_maps_hit = entry_maps & set(TARGET_MAPS)
        if not target_maps_hit:
            continue  # never spawns in Outland/Northrend, not our concern
        if len(target_maps_hit) > 1:
            # spawns in BOTH Outland and Northrend under one entry - shift amount is ambiguous
            skipped_multi_target.append({
                "entry": entry, "name": tpl["name"],
                "maps": sorted(entry_maps),
                "reason": "Spawns in both Outland and Northrend under the same entry; "
                          "shift amount (-10 vs -20) is ambiguous. Needs manual split.",
            })
            continue

        target_map = next(iter(target_maps_hit))

        if tpl["minlevel"] < MIN_SOURCE_LEVEL[target_map]:
            skipped_below_threshold.append({
                "entry": entry, "name": tpl["name"], "map": target_map,
                "map_name": MAP_NAMES[target_map], "minlevel": tpl["minlevel"],
                "reason": f"Below the level-{MIN_SOURCE_LEVEL[target_map]} floor for "
                          f"{MAP_NAMES[target_map]} - likely edge-case content, not core zone content.",
            })
            continue

        shift = LEVEL_SHIFT[target_map]
        other_maps = entry_maps - {target_map}

        new_min = clamp_level(tpl["minlevel"], shift)
        new_max = clamp_level(tpl["maxlevel"], shift)

        record = {
            "entry": entry,
            "name": tpl["name"],
            "map": target_map,
            "map_name": MAP_NAMES[target_map],
            "old_minlevel": tpl["minlevel"],
            "old_maxlevel": tpl["maxlevel"],
            "new_minlevel": new_min,
            "new_maxlevel": new_max,
        }

        if not other_maps:
            simple.append(record)
        else:
            # Shared with maps outside our target set (e.g. also spawned in
            # Azeroth). Don't mutate the shared template - propose a clone.
            new_entry = next_clone_entry
            next_clone_entry += 1
            record["new_entry"] = new_entry
            record["also_spawns_in_maps"] = sorted(other_maps)
            record["spawn_guids_to_repoint"] = guids_by_entry_map[(entry, target_map)]
            cloned.append(record)

    return {
        "simple": simple,
        "cloned": cloned,
        "skipped_multi_target": skipped_multi_target,
        "skipped_below_threshold": skipped_below_threshold,
        "already_processed_count": len(already_processed),
        "already_processed_entries": already_processed,
        "columns": {
            "creature_template": {"entry": entry_col, "name": name_col,
                                   "minlevel": minlvl_col, "maxlevel": maxlvl_col,
                                   "type": type_col},
            "creature": {"id": spawn_id_col, "map": spawn_map_col, "guid": spawn_guid_col},
        },
    }


# ---------------------------------------------------------------------------
# QUESTS
# ---------------------------------------------------------------------------

def build_money_curve(conn, level_col, money_col):
    """Average RewardMoney by QuestLevel, built from the full quest_template.
    RewardMoney is a flat copper value tuned for the quest's original level -
    unlike XP, the engine does NOT recompute it from level at runtime, so it
    needs the same curve-ratio rescaling XP would have needed if it were
    stored raw."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {level_col} AS lvl, {money_col} AS money FROM quest_template "
            f"WHERE {money_col} > 0 AND {level_col} BETWEEN 1 AND 80"
        )
        rows = cur.fetchall()

    buckets = defaultdict(list)
    for r in rows:
        buckets[r["lvl"]].append(r["money"])

    curve = {lvl: sum(vals) / len(vals) for lvl, vals in buckets.items() if vals}
    return curve


def curve_lookup(curve, level):
    """Nearest-known-level lookup with linear interpolation for gaps."""
    if level in curve:
        return curve[level]
    known = sorted(curve.keys())
    if not known:
        return None
    if level < known[0]:
        return curve[known[0]]
    if level > known[-1]:
        return curve[known[-1]]
    lower = max(k for k in known if k < level)
    upper = min(k for k in known if k > level)
    if upper == lower:
        return curve[lower]
    frac = (level - lower) / (upper - lower)
    return curve[lower] + frac * (curve[upper] - curve[lower])


def analyze_quests(conn):
    qt_cols = table_columns(conn, "quest_template")
    id_col = resolve_column(qt_cols, ["ID", "id", "Id"], context="quest_template PK")
    level_col = resolve_column(qt_cols, ["QuestLevel", "questlevel"], context="quest_template level")
    minlevel_col = resolve_column(qt_cols, ["MinLevel", "minlevel"], required=False, context="quest_template min level")
    money_col = resolve_column(qt_cols, ["RewardMoney", "RewMoney"], required=False, context="quest_template money reward")
    xpdiff_col = resolve_column(qt_cols, ["RewardXPDifficulty", "RewXPId"], required=False,
                                 context="quest_template XP difficulty index")
    poi_continent_col = resolve_column(qt_cols, ["POIContinent", "PointOptionContinentID"], required=False,
                                        context="quest_template POI continent")
    title_col = resolve_column(qt_cols, ["LogTitle", "Title", "logtitle"], required=False, context="quest title")

    quest_map = {}          # quest_id -> map
    quest_map_source = {}   # quest_id -> how we determined it

    # Source 0: POIContinent column directly on quest_template, when populated.
    # It's frequently 0/unset for quests where nobody filled it in, but 0 is
    # also a legit map id (Eastern Kingdoms) - since none of our target maps
    # is ever 0, checking membership in TARGET_MAPS is safe either way.
    if poi_continent_col:
        placeholders = ", ".join(["%s"] * len(TARGET_MAPS))
        with conn.cursor() as cur:
            cur.execute(f"SELECT {id_col} AS qid, {poi_continent_col} AS map FROM quest_template "
                        f"WHERE {poi_continent_col} IN ({placeholders})", TARGET_MAPS)
            for row in cur.fetchall():
                quest_map[row["qid"]] = row["map"]
                quest_map_source[row["qid"]] = "quest_template.POIContinent"

    # Source 1: quest_poi, if present and it carries a real map id
    if table_exists(conn, "quest_poi"):
        poi_cols = table_columns(conn, "quest_poi")
        poi_quest_col = resolve_column(poi_cols, ["QuestID", "questID", "questId"], required=False)
        poi_map_col = resolve_column(poi_cols, ["MapID", "mapid", "Map"], required=False)
        if poi_quest_col and poi_map_col:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {poi_quest_col} AS qid, {poi_map_col} AS map FROM quest_poi")
                for row in cur.fetchall():
                    if row["map"] in TARGET_MAPS and row["qid"] not in quest_map:
                        quest_map[row["qid"]] = row["map"]
                        quest_map_source[row["qid"]] = "quest_poi"

    # Source 2: quest givers (creature_questrelation / gameobject_questrelation)
    def add_giver_source(rel_table, rel_id_col_candidates, ent_table, ent_id_col_candidates, ent_map_candidates):
        if not table_exists(conn, rel_table) or not table_exists(conn, ent_table):
            return
        rel_cols = table_columns(conn, rel_table)
        ent_cols = table_columns(conn, ent_table)
        rel_ent_col = resolve_column(rel_cols, rel_id_col_candidates, required=False)
        rel_quest_col = resolve_column(rel_cols, ["quest", "Quest", "QuestId"], required=False)
        ent_id_col = resolve_column(ent_cols, ent_id_col_candidates, required=False)
        ent_map_col = resolve_column(ent_cols, ent_map_candidates, required=False)
        if not all([rel_ent_col, rel_quest_col, ent_id_col, ent_map_col]):
            return
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT r.{rel_quest_col} AS qid, e.{ent_map_col} AS map "
                f"FROM {rel_table} r JOIN {ent_table} e ON e.{ent_id_col} = r.{rel_ent_col}"
            )
            for row in cur.fetchall():
                if row["map"] in TARGET_MAPS and row["qid"] not in quest_map:
                    quest_map[row["qid"]] = row["map"]
                    quest_map_source[row["qid"]] = f"{rel_table}"

    add_giver_source("creature_questrelation", ["id", "CreatureID"], "creature",
                      ["id1", "id"], ["map", "Map"])
    add_giver_source("gameobject_questrelation", ["id", "GameObjectID"], "gameobject",
                      ["id", "ID"], ["map", "Map"])

    # Pull full quest rows for anything we mapped
    if not quest_map:
        return {"quests": [], "unmapped_note": "No quests resolved to Outland/Northrend via quest_poi or givers."}

    with conn.cursor() as cur:
        cols = f"{id_col} AS id, {level_col} AS level"
        if minlevel_col:
            cols += f", {minlevel_col} AS minlevel"
        if money_col:
            cols += f", {money_col} AS money"
        if xpdiff_col:
            cols += f", {xpdiff_col} AS xpdiff"
        if title_col:
            cols += f", {title_col} AS title"
        cur.execute(f"SELECT {cols} FROM quest_template WHERE {id_col} IN "
                    f"({','.join(str(int(q)) for q in quest_map.keys())})")
        quest_rows = {row["id"]: row for row in cur.fetchall()}

    money_curve = build_money_curve(conn, level_col, money_col) if money_col else {}

    processed_ids = get_processed_ids(conn, QUEST_STATE_TABLE)
    already_processed = [qid for qid in quest_map if qid in processed_ids]

    plan = []
    skipped_below_threshold = []
    for qid, map_id in quest_map.items():
        if qid in processed_ids:
            continue
        row = quest_rows.get(qid)
        if not row:
            continue
        old_level = row["level"]

        if old_level < MIN_SOURCE_LEVEL[map_id]:
            skipped_below_threshold.append({
                "quest_id": qid, "title": row.get("title"), "map": map_id,
                "map_name": MAP_NAMES[map_id], "old_level": old_level,
                "reason": f"Below the level-{MIN_SOURCE_LEVEL[map_id]} floor for "
                          f"{MAP_NAMES[map_id]} - likely edge-case content, not core zone content.",
            })
            continue

        shift = LEVEL_SHIFT[map_id]
        new_level = clamp_level(old_level, shift, floor=MIN_QUEST_LEVEL_FLOOR)

        old_minlevel = row.get("minlevel")
        new_minlevel = clamp_level(old_minlevel, shift, floor=MIN_QUEST_LEVEL_FLOOR) if old_minlevel is not None else None

        old_money = row.get("money") or 0
        money_note = None
        if money_col and old_money > 0:
            ref_old = curve_lookup(money_curve, old_level)
            ref_new = curve_lookup(money_curve, new_level)
            if ref_old and ref_new is not None:
                ratio = old_money / ref_old
                new_money = round(ref_new * ratio)
            else:
                new_money = old_money
                money_note = "no money curve reference available, left unchanged - review manually"
        else:
            new_money = old_money

        plan.append({
            "quest_id": qid,
            "title": row.get("title"),
            "map": map_id,
            "map_name": MAP_NAMES[map_id],
            "map_source": quest_map_source.get(qid),
            "old_level": old_level,
            "new_level": new_level,
            "old_minlevel": old_minlevel,
            "new_minlevel": new_minlevel,
            "old_reward_money": old_money,
            "new_reward_money": new_money,
            "reward_xp_difficulty": row.get("xpdiff"),
            "note": money_note,
        })

    return {
        "quests": plan,
        "already_processed_count": len(already_processed),
        "already_processed_quest_ids": already_processed,
        "skipped_below_threshold": skipped_below_threshold,
        "columns": {"quest_template": {"id": id_col, "level": level_col, "minlevel": minlevel_col,
                                        "money": money_col, "xpdiff": xpdiff_col, "title": title_col}},
        "money_curve_sample_points": len(money_curve),
        "xp_note": ("XP is not stored as a raw value in this schema (RewardXPDifficulty is an index "
                    "into the client's QuestXP.dbc). The engine recomputes correct XP from QuestLevel "
                    "at runtime, so no XP field needs to be written - shifting QuestLevel is sufficient."),
    }


# ---------------------------------------------------------------------------

def main():
    os.makedirs(PLAN_DIR, exist_ok=True)
    with get_connection() as conn:
        print("Analyzing creatures...")
        creature_plan = analyze_creatures(conn)
        print("Analyzing quests...")
        quest_plan = analyze_quests(conn)

    with open(os.path.join(PLAN_DIR, "creature_plan.json"), "w") as f:
        json.dump(creature_plan, f, indent=2)
    with open(os.path.join(PLAN_DIR, "quest_plan.json"), "w") as f:
        json.dump(quest_plan, f, indent=2)

    print("\n=== CREATURE SUMMARY ===")
    print(f"  Simple level updates:        {len(creature_plan['simple'])}")
    print(f"  Clone-required (shared tpl): {len(creature_plan['cloned'])}")
    print(f"  Skipped (ambiguous shift):   {len(creature_plan['skipped_multi_target'])}")
    print(f"  Skipped (below level floor): {len(creature_plan.get('skipped_below_threshold', []))}")
    print(f"  Already processed (skipped): {creature_plan.get('already_processed_count', 0)}")
    if creature_plan["skipped_multi_target"]:
        print("  -> WARNING: these entries spawn in both Outland and Northrend, see plan file.")

    print("\n=== QUEST SUMMARY ===")
    print(f"  Quests mapped to Outland/Northrend:   {len(quest_plan.get('quests', []))}")
    print(f"  Skipped (below level floor):          {len(quest_plan.get('skipped_below_threshold', []))}")
    print(f"  Already processed (skipped):          {quest_plan.get('already_processed_count', 0)}")
    print(f"  Money curve reference points built:   {quest_plan.get('money_curve_sample_points', 0)}")
    print(f"  Note: {quest_plan.get('xp_note', '')}")
    unresolved = [q for q in quest_plan.get("quests", []) if q.get("note")]
    if unresolved:
        print(f"  -> WARNING: {len(unresolved)} quests had no money curve reference, left unchanged.")

    print(f"\nPlans written to {PLAN_DIR}/creature_plan.json and {PLAN_DIR}/quest_plan.json")
    print("Review them, then run executor.py.")


if __name__ == "__main__":
    main()
