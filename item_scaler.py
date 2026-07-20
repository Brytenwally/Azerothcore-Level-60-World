"""
ITEM SCALER (part of the brain)
================================
Read-only. Finds every weapon/armor item awarded by an Outland/Northrend
quest, or dropped by an Outland/Northrend open-world creature (from the plans
already produced by brain.py), and works out a downscaled version of each.

Requires plans/creature_plan.json and plans/quest_plan.json to already exist
(run brain.py first).

Algorithm (as specified):
  Quest reward items:
    - The item has no RequiredLevel of its own, so we treat its quest's
      NEW (already-downscaled) level as its target level.
  Creature drop items:
    - target level = item's own current RequiredLevel minus 10 (if sourced
      only from Outland) or minus 20 (if sourced only from Northrend).

  For every target item, find "reference" items already in item_template
  with the same class/subclass/InventoryType/Quality at the target level
  (widening the level search outward if nothing matches exactly). From that
  reference set:
    - budget_stats  = representative total stat points (sum of non-zero
                       stat_value_N across a reference item, averaged)
    - budget_armor  = representative armor value (armor items only)
    - budget_dps    = representative damage-per-second (weapons only)
    - ref_itemlevel = representative ItemLevel

  Apply to the item being downscaled:
    - scale_factor = budget_stats / item's own current total stat points
      -> each stat_value_N *= scale_factor (stat_type_N, i.e. WHICH stat,
         is never changed - only magnitude). This preserves the item's own
         stat ratios (20 STR / 10 STA -> 10 STR / 5 STA if scale_factor=0.5).
    - armor is set directly to budget_armor (single field, no ratio to keep).
    - weapon damage: new average hit damage = budget_dps * item's own delay;
      dmg_min/dmg_max are rescaled from their OLD values toward that new
      average using the same scale-factor approach, preserving the item's
      own min/max spread. delay itself is never changed.
    - ItemLevel is copied directly from the reference set.
    - displayid is never changed.

  Items also used outside the target quests/creatures (i.e. also awarded by
  an Azeroth quest, or also dropped by a non-Outland/Northrend creature -
  including indirectly via a shared reference_loot_template group like
  "World Loot Level 24") are never mutated directly - they're flagged for
  cloning (new entry) instead, same pattern as the creature clone logic in
  brain.py.
"""
import json
import os
from collections import defaultdict

from config import (LEVEL_SHIFT, MAP_NAMES, MAP_CAP_LEVEL, PLAN_DIR, CLONE_ENTRY_OFFSET,
                    ITEM_STATE_TABLE, SPELL_CSV_PATH, BRAIN_PKL_PATH)
from db import get_connection, table_columns, table_exists, resolve_column, get_processed_ids
import spell_lookup
import blizzlike_brain

ITEM_CLASS_WEAPON = 2
ITEM_CLASS_ARMOR = 4
MAX_LEVEL_SEARCH_RADIUS = 10


def load_plan(name):
    with open(os.path.join(PLAN_DIR, f"{name}.json")) as f:
        return json.load(f)


def resolve_item_columns(conn):
    cols = table_columns(conn, "item_template")
    c = {
        "entry": resolve_column(cols, ["entry", "Entry", "ID"], context="item_template PK"),
        "class": resolve_column(cols, ["class", "Class"], context="item class"),
        "subclass": resolve_column(cols, ["subclass", "Subclass"], context="item subclass"),
        "name": resolve_column(cols, ["name", "Name"], context="item name"),
        "quality": resolve_column(cols, ["Quality", "quality"], context="item quality/rarity"),
        "itemlevel": resolve_column(cols, ["ItemLevel", "itemlevel"], context="item level"),
        "reqlevel": resolve_column(cols, ["RequiredLevel", "requiredlevel"], context="item required level"),
        "invtype": resolve_column(cols, ["InventoryType", "inventoryType"], context="item slot/inventory type"),
        "armor": resolve_column(cols, ["armor", "Armor"], required=False, context="item armor value"),
        "delay": resolve_column(cols, ["delay", "Delay"], required=False, context="item weapon delay"),
        "dmgmin": resolve_column(cols, ["dmg_min1", "DamageMin1"], required=False, context="item weapon min damage"),
        "dmgmax": resolve_column(cols, ["dmg_max1", "DamageMax1"], required=False, context="item weapon max damage"),
        "displayid": resolve_column(cols, ["displayid", "DisplayID"], required=False, context="item display id"),
        "randomproperty": resolve_column(cols, ["RandomProperty", "randomproperty"], required=False,
                                          context="item random property id"),
        "randomsuffix": resolve_column(cols, ["RandomSuffix", "randomsuffix"], required=False,
                                        context="item random suffix id"),
    }
    stat_types, stat_values = [], []
    for i in range(1, 11):
        st = resolve_column(cols, [f"stat_type{i}", f"StatType{i}"], required=False)
        sv = resolve_column(cols, [f"stat_value{i}", f"StatValue{i}"], required=False)
        if st and sv:
            stat_types.append(st)
            stat_values.append(sv)
    if not stat_types:
        raise RuntimeError(
            "item_template has no stat_type_N / stat_value_N column pairs. This module needs "
            "BOTH (type tells us WHICH attribute, value tells us HOW MUCH) - a value-only export "
            "can't be used to reconstruct stats. Re-export the full item_template table."
        )
    c["stat_types"], c["stat_values"] = stat_types, stat_values

    spell_slots = []
    for i in range(1, 6):
        sid = resolve_column(cols, [f"spellid_{i}"], required=False)
        strig = resolve_column(cols, [f"spelltrigger_{i}"], required=False)
        if sid and strig:
            spell_slots.append((sid, strig))
    c["spell_slots"] = spell_slots

    return c


# ---------------------------------------------------------------------------
# Gathering candidate items
# ---------------------------------------------------------------------------

def gather_target_items(conn, creature_plan, quest_plan, ic):
    """Returns item_id -> list of source dicts describing everywhere in our
    target set that references it."""
    item_sources = defaultdict(list)

    # --- Quest reward items ---
    qt_cols = table_columns(conn, "quest_template")
    q_id_col = resolve_column(qt_cols, ["ID", "id", "Id"], context="quest_template PK")
    reward_cols = [c for c in
                   (["RewardItem1", "RewardItem2", "RewardItem3", "RewardItem4"] +
                    [f"RewardChoiceItemID{i}" for i in range(1, 7)])
                   if c in qt_cols]

    quests = quest_plan.get("quests", [])
    quest_ids = [q["quest_id"] for q in quests]

    # Use the quest's downscaled MinLevel (the REAL level required to accept
    # it) rather than downscaled QuestLevel (the content/design level, which
    # is often noticeably higher - QuestLevel=60/MinLevel=58 is common, and
    # for some quests the gap is much wider). QuestLevel overstates how
    # strong a reward should be. Falls back to QuestLevel-derived new_level
    # when MinLevel is 0/unset, since that's not a meaningful signal on its
    # own (many quests never had it populated) - using it blindly would
    # under-level items just as wrongly in the other direction.
    quest_item_target_level = {}
    for q in quests:
        old_minlevel = q.get("old_minlevel")
        if old_minlevel is not None and old_minlevel > 0:
            quest_item_target_level[q["quest_id"]] = q["new_minlevel"]
        else:
            quest_item_target_level[q["quest_id"]] = q["new_level"]

    if quest_ids and reward_cols:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {q_id_col} AS qid, {', '.join(reward_cols)} FROM quest_template "
                f"WHERE {q_id_col} IN ({','.join(str(int(q)) for q in quest_ids)})"
            )
            for row in cur.fetchall():
                for col in reward_cols:
                    item_id = row[col]
                    if item_id and item_id > 0:
                        item_sources[item_id].append({
                            "type": "quest", "quest_id": row["qid"],
                            "new_level": quest_item_target_level[row["qid"]],
                        })

    # --- Creature drop items ---
    ct_cols = table_columns(conn, "creature_template")
    ct_entry_col = resolve_column(ct_cols, ["entry", "Entry"], context="creature_template PK")
    lootid_col = resolve_column(ct_cols, ["lootid", "LootID"], required=False, context="creature_template lootid")

    target_entries_shift = {}
    for rec in creature_plan.get("simple", []):
        target_entries_shift[rec["entry"]] = rec["map"]
    for rec in creature_plan.get("cloned", []):
        target_entries_shift[rec["entry"]] = rec["map"]

    if lootid_col and target_entries_shift and table_exists(conn, "creature_loot_template"):
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {ct_entry_col} AS entry, {lootid_col} AS lootid FROM creature_template "
                f"WHERE {ct_entry_col} IN ({','.join(str(e) for e in target_entries_shift)})"
            )
            lootid_to_entries = defaultdict(list)
            for row in cur.fetchall():
                if row["lootid"]:
                    lootid_to_entries[row["lootid"]].append(row["entry"])

        if lootid_to_entries:
            clt_cols = table_columns(conn, "creature_loot_template")
            clt_entry_col = resolve_column(clt_cols, ["Entry", "entry"], context="creature_loot_template PK")
            clt_item_col = resolve_column(clt_cols, ["Item", "item"], context="creature_loot_template item")
            clt_ref_col = resolve_column(clt_cols, ["Reference", "reference"], required=False,
                                          context="creature_loot_template reference")
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {clt_entry_col} AS lootid, {clt_item_col} AS item"
                    + (f", {clt_ref_col} AS reference" if clt_ref_col else "")
                    + f" FROM creature_loot_template WHERE {clt_entry_col} IN "
                    f"({','.join(str(l) for l in lootid_to_entries)})"
                )
                rows = cur.fetchall()

            direct_item_ids = set()
            reference_ids = set()
            for row in rows:
                ref = row.get("reference") if clt_ref_col else 0
                if ref:
                    reference_ids.add(ref)
                elif row["item"] and row["item"] > 0:
                    direct_item_ids.add(row["item"])

            # Expand reference groups (e.g. "World Loot Level 24") into their
            # real items. One level deep - references chaining into further
            # references are rare and not followed here.
            ref_group_items = defaultdict(set)  # reference_id -> set(item_id)
            if reference_ids and table_exists(conn, "reference_loot_template"):
                rlt_cols = table_columns(conn, "reference_loot_template")
                rlt_entry_col = resolve_column(rlt_cols, ["Entry", "entry"], context="reference_loot_template PK")
                rlt_item_col = resolve_column(rlt_cols, ["Item", "item"], context="reference_loot_template item")
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {rlt_entry_col} AS ref, {rlt_item_col} AS item FROM reference_loot_template "
                        f"WHERE {rlt_entry_col} IN ({','.join(str(r) for r in reference_ids)}) AND {rlt_item_col} > 0"
                    )
                    for row in cur.fetchall():
                        ref_group_items[row["ref"]].add(row["item"])

            for row in rows:
                for creature_entry in lootid_to_entries[row["lootid"]]:
                    map_id = target_entries_shift[creature_entry]
                    shift_info = {"type": "creature", "creature_entry": creature_entry,
                                  "map": map_id, "shift": LEVEL_SHIFT[map_id]}
                    ref = row.get("reference") if clt_ref_col else 0
                    if ref:
                        for item_id in ref_group_items.get(ref, ()):
                            item_sources[item_id].append(dict(shift_info, via_reference=ref))
                    elif row["item"] and row["item"] > 0:
                        item_sources[row["item"]].append(shift_info)
    elif not lootid_col:
        print("[WARN] No lootid column found on creature_template - skipping mob-drop items entirely.")

    # --- Vendor-sold items ---
    # Vendor items behave like mob-drop items for leveling purposes:
    # RequiredLevel is real, player-visible data, and the item's OWN current
    # RequiredLevel minus the map's shift determines its new target level -
    # same rule as creature-drop items, not the "pretend level" quest
    # rewards use. Items sitting exactly at the expansion's level cap are
    # skipped entirely (not gathered at all) - that's usually pre-raid BIS/
    # reputation/PvP/Tier-equivalent gear needing its own formula later.
    if target_entries_shift and table_exists(conn, "npc_vendor"):
        nv_cols = table_columns(conn, "npc_vendor")
        nv_entry_col = resolve_column(nv_cols, ["entry", "Entry"], context="npc_vendor creature entry")
        nv_item_col = resolve_column(nv_cols, ["item", "Item"], context="npc_vendor item")

        with conn.cursor() as cur:
            cur.execute(
                f"SELECT nv.{nv_entry_col} AS entry, nv.{nv_item_col} AS item, "
                f"it.{ic['reqlevel']} AS reqlevel FROM npc_vendor nv "
                f"JOIN item_template it ON it.{ic['entry']} = nv.{nv_item_col} "
                f"WHERE nv.{nv_entry_col} IN ({','.join(str(e) for e in target_entries_shift)}) "
                f"AND it.{ic['class']} IN ({ITEM_CLASS_WEAPON}, {ITEM_CLASS_ARMOR})"
            )
            vendor_rows = cur.fetchall()

        n_capped = 0
        for row in vendor_rows:
            creature_entry = row["entry"]
            map_id = target_entries_shift[creature_entry]
            cap = MAP_CAP_LEVEL.get(map_id)
            if cap is not None and row["reqlevel"] == cap:
                n_capped += 1
                continue
            item_sources[row["item"]].append({
                "type": "vendor", "creature_entry": creature_entry,
                "map": map_id, "shift": LEVEL_SHIFT[map_id],
            })
        if n_capped:
            print(f"  Skipped {n_capped} vendor item(s) sitting exactly at the expansion level cap "
                  f"(pre-raid BIS/rep/PvP/Tier-equivalent gear - needs its own formula later).")

    return item_sources


def find_shared_usage(conn, item_ids, target_quest_ids, target_creature_entries):
    """Items also referenced by a quest/creature OUTSIDE our target set can't
    be mutated directly - flag them for cloning instead."""
    if not item_ids:
        return set()
    shared = set()
    ids_sql = ",".join(str(i) for i in item_ids)

    qt_cols = table_columns(conn, "quest_template")
    q_id_col = resolve_column(qt_cols, ["ID", "id", "Id"], context="quest_template PK")
    reward_cols = [c for c in
                   (["RewardItem1", "RewardItem2", "RewardItem3", "RewardItem4"] +
                    [f"RewardChoiceItemID{i}" for i in range(1, 7)])
                   if c in qt_cols]
    if reward_cols:
        where = " OR ".join(f"{c} IN ({ids_sql})" for c in reward_cols)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {q_id_col} AS qid, {', '.join(reward_cols)} FROM quest_template WHERE {where}")
            for row in cur.fetchall():
                if row["qid"] not in target_quest_ids:
                    for c in reward_cols:
                        if row[c] in item_ids:
                            shared.add(row[c])

    if table_exists(conn, "creature_loot_template"):
        clt_cols = table_columns(conn, "creature_loot_template")
        clt_entry_col = resolve_column(clt_cols, ["Entry", "entry"], context="creature_loot_template PK")
        clt_item_col = resolve_column(clt_cols, ["Item", "item"], context="creature_loot_template item")
        clt_ref_col = resolve_column(clt_cols, ["Reference", "reference"], required=False,
                                      context="creature_loot_template reference")
        ct_cols = table_columns(conn, "creature_template")
        ct_entry_col = resolve_column(ct_cols, ["entry", "Entry"], context="creature_template PK")
        lootid_col = resolve_column(ct_cols, ["lootid", "LootID"], required=False)

        if lootid_col:
            # Direct (non-reference) drops
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT c.{ct_entry_col} AS centry, l.{clt_item_col} AS item "
                    f"FROM creature_loot_template l "
                    f"JOIN creature_template c ON c.{lootid_col} = l.{clt_entry_col} "
                    f"WHERE l.{clt_item_col} IN ({ids_sql})"
                    + (f" AND (l.{clt_ref_col} = 0 OR l.{clt_ref_col} IS NULL)" if clt_ref_col else "")
                )
                for row in cur.fetchall():
                    if row["item"] in item_ids and row["centry"] not in target_creature_entries:
                        shared.add(row["item"])

            # Reference-group drops: any item reachable via a reference group
            # that ANY creature outside our target set also points at is shared.
            if clt_ref_col and table_exists(conn, "reference_loot_template"):
                rlt_cols = table_columns(conn, "reference_loot_template")
                rlt_entry_col = resolve_column(rlt_cols, ["Entry", "entry"], context="reference_loot_template PK")
                rlt_item_col = resolve_column(rlt_cols, ["Item", "item"], context="reference_loot_template item")
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {rlt_entry_col} AS ref, {rlt_item_col} AS item FROM reference_loot_template "
                        f"WHERE {rlt_item_col} IN ({ids_sql})"
                    )
                    item_to_refs = defaultdict(set)
                    for row in cur.fetchall():
                        item_to_refs[row["item"]].add(row["ref"])

                all_refs = {r for refs in item_to_refs.values() for r in refs}
                if all_refs:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT c.{ct_entry_col} AS centry, l.{clt_ref_col} AS ref "
                            f"FROM creature_loot_template l "
                            f"JOIN creature_template c ON c.{lootid_col} = l.{clt_entry_col} "
                            f"WHERE l.{clt_ref_col} IN ({','.join(str(r) for r in all_refs)})"
                        )
                        ref_to_outside_creature = defaultdict(bool)
                        for row in cur.fetchall():
                            if row["centry"] not in target_creature_entries:
                                ref_to_outside_creature[row["ref"]] = True

                    for item_id, refs in item_to_refs.items():
                        if any(ref_to_outside_creature.get(r) for r in refs):
                            shared.add(item_id)

    if table_exists(conn, "npc_vendor"):
        nv_cols = table_columns(conn, "npc_vendor")
        nv_entry_col = resolve_column(nv_cols, ["entry", "Entry"], context="npc_vendor creature entry")
        nv_item_col = resolve_column(nv_cols, ["item", "Item"], context="npc_vendor item")
        with conn.cursor() as cur:
            cur.execute(f"SELECT {nv_entry_col} AS centry, {nv_item_col} AS item FROM npc_vendor "
                        f"WHERE {nv_item_col} IN ({ids_sql})")
            for row in cur.fetchall():
                if row["item"] in item_ids and row["centry"] not in target_creature_entries:
                    shared.add(row["item"])

    return shared


# ---------------------------------------------------------------------------
# Budget calculation
# ---------------------------------------------------------------------------

def total_stat_points(row, ic):
    return sum(row[c] or 0 for c in ic["stat_values"])


def build_reference_index(conn, ic):
    """Fetch every weapon/armor item ONCE and build three progressively
    broader indices, all keyed down to {level: [rows]}:
      exact:     (class, subclass, invtype, quality)  - e.g. epic two-hand staves
      subclass:  (class, subclass, quality)            - e.g. epic staves, any slot
      class:     (class, quality)                       - e.g. epic weapons, any subtype
    Rarer categories (staves, fishing poles, wands...) often have zero other
    items at the exact same slot+subtype+quality near a given level - the
    search falls back through these broader buckets rather than giving up,
    while always still preserving Quality (the single biggest value driver)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM item_template WHERE {ic['class']} IN (%s, %s)",
            (ITEM_CLASS_WEAPON, ITEM_CLASS_ARMOR),
        )
        rows = cur.fetchall()
    idx_exact = defaultdict(lambda: defaultdict(list))
    idx_subclass = defaultdict(lambda: defaultdict(list))
    idx_class = defaultdict(lambda: defaultdict(list))
    for row in rows:
        c, sc = row[ic["class"]], row[ic["subclass"]]
        it, q, lvl = row[ic["invtype"]], row[ic["quality"]], row[ic["reqlevel"]]
        idx_exact[(c, sc, it, q)][lvl].append(row)
        idx_subclass[(c, sc, q)][lvl].append(row)
        idx_class[(c, q)][lvl].append(row)
    return {"exact": idx_exact, "subclass": idx_subclass, "class": idx_class}


def find_reference_budget(ref_index, ic, item_class, item_subclass, invtype, quality, target_level, exclude_entry):
    stages = [
        ("exact_slot", ref_index["exact"].get((item_class, item_subclass, invtype, quality), {})),
        ("same_subclass_any_slot", ref_index["subclass"].get((item_class, item_subclass, quality), {})),
        ("same_class_any_subclass", ref_index["class"].get((item_class, quality), {})),
    ]
    for stage_name, by_level in stages:
        if not by_level:
            continue
        for radius in range(0, MAX_LEVEL_SEARCH_RADIUS + 1):
            levels = [target_level] if radius == 0 else [target_level - radius, target_level + radius]
            levels = [l for l in levels if l >= 1]
            candidates = []
            for l in levels:
                candidates.extend(r for r in by_level.get(l, []) if r[ic["entry"]] != exclude_entry)
            if candidates:
                return candidates, radius, stage_name
    return [], None, None


def median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2


def extract_item_spell_stats(item_row, ic, spell_index):
    """Scans every ON_EQUIP spell slot on this item and classifies what each
    one grants via spell_lookup. Returns:
      converted            - list of (item_mod_type, magnitude) to fold into
                              this item's stats
      spellid_cols_to_clear - which spellid_N columns should be zeroed since
                              their effect has been converted to a literal stat
      unhandled_notes       - human-readable reasons anything was left alone
      unrecognized_count    - count of on-equip spells that were left
                              entirely unconverted (used to still approximate
                              their contribution to the item's old power via
                              a flat per-spell budget estimate, even though we
                              don't know exactly what they grant)

    item_template.spellid_N is per-item row data, not a shared pointer like
    RandomProperty - zeroing it on one item's row never touches any other
    item, so there's no clone/sharing concern here at all, unlike items or
    creature templates.

    If a single spell has BOTH recognized and unrecognized effects, NONE of
    it is converted - partially removing a multi-effect spell would leave it
    active with only some effects gone, which is worse than leaving it alone."""
    converted, spellid_cols_to_clear, unhandled_notes = [], [], []
    unrecognized_count = 0
    if not spell_index:
        return converted, spellid_cols_to_clear, unhandled_notes, unrecognized_count

    for sid_col, strig_col in ic.get("spell_slots", []):
        spell_id = item_row.get(sid_col)
        trigger = item_row.get(strig_col)
        if not spell_id or trigger != spell_lookup.ITEM_SPELLTRIGGER_ON_EQUIP:
            continue
        spell_row = spell_index.get(spell_id)
        if not spell_row:
            unhandled_notes.append(f"on-equip spell {spell_id} not found in Spell.csv index")
            unrecognized_count += 1
            continue
        this_converted, this_unhandled = spell_lookup.classify_spell_stats(spell_row)
        if this_converted and not this_unhandled:
            converted.extend(this_converted)
            spellid_cols_to_clear.append(sid_col)
        elif this_converted and this_unhandled:
            name = spell_row.get("Name_Lang_enUS", "?")
            unhandled_notes.append(
                f"spell {spell_id} ({name}) has both recognized and unrecognized effects - "
                f"left entirely unconverted for safety"
            )
            unrecognized_count += 1
        elif this_unhandled:
            name = spell_row.get("Name_Lang_enUS", "?")
            unhandled_notes.append(f"on-equip spell {spell_id} ({name}) not auto-convertible: "
                                    + "; ".join(this_unhandled))
            unrecognized_count += 1
    return converted, spellid_cols_to_clear, unhandled_notes, unrecognized_count


# Budget bonus per unrecognized on-equip passive spell slot, calibrated to
# match train_brain.py's EQUIP_SPELL_BUDGET_FACTOR so an item's old weighted
# total isn't underestimated just because we can't literally convert what an
# on-equip spell grants.
EQUIP_SPELL_BUDGET_FACTOR = 0.33


def build_item_record(ref_index, spell_index, brain, ic, item_row, target_level, source_list):
    entry = item_row[ic["entry"]]
    item_class = item_row[ic["class"]]
    subclass = item_row[ic["subclass"]]
    invtype = item_row[ic["invtype"]]
    quality = item_row[ic["quality"]]
    old_item_level = item_row[ic["itemlevel"]] or 0
    old_required_level = item_row[ic["reqlevel"]] or 0

    # Quest reward items have no real RequiredLevel of their own - the quest's
    # (downscaled) level is only ever used internally as a stand-in to pick
    # the right budget/itemlevel target. Actually writing that value into
    # RequiredLevel would be new, artificial gating that wasn't there before -
    # a reward you just earned should stay wearable immediately, not
    # suddenly locked behind a level requirement invented for this purpose.
    # Mob-drop items are different: RequiredLevel is real, player-visible
    # data there, and should scale down along with everything else.
    is_quest_sourced = any(s.get("type") == "quest" for s in source_list)

    # Still do the live search - needed for new_item_level, RandomProperty
    # substitute data, and as a fallback data source if the brain has
    # nothing for this exact combo.
    ref_rows, radius, match_stage = find_reference_budget(
        ref_index, ic, item_class, subclass, invtype, quality, target_level, entry
    )

    confidence_notes = []

    # --- new item level: live search, then a ratio-preserving heuristic ---
    if ref_rows:
        new_item_level = median([r[ic["itemlevel"]] for r in ref_rows])
        itemlevel_source = f"live_search({match_stage}, radius={radius})"
    elif old_required_level and old_item_level:
        new_item_level = round(old_item_level * (target_level / old_required_level))
        itemlevel_source = "heuristic_ratio"
        confidence_notes.append("no comparable reference items found anywhere - item level estimated "
                                 "by preserving this item's own itemlevel/required-level ratio")
    else:
        new_item_level = target_level
        itemlevel_source = "heuristic_fallback"
        confidence_notes.append("no comparable reference items and no usable old itemlevel/required "
                                 "level - item level crudely set equal to the new required level")

    # --- budget: pre-trained curve first (robust to sparse categories like
    # staves), then the live search, then nothing (handled below via a
    # simple level-ratio scale as the ultimate fallback) ---
    target_budget, budget_source = blizzlike_brain.get_budget(
        brain, item_class, subclass, invtype, quality, new_item_level)
    if target_budget is None and ref_rows:
        target_budget = median([total_stat_points(r, ic) for r in ref_rows])
        budget_source = f"live_search({match_stage})"

    # --- armor / dps: same brain-first, live-search-second priority ---
    brain_stats = blizzlike_brain.get_lookup_stats(brain, item_class, subclass, invtype, quality, new_item_level)
    target_armor = brain_stats.get("avg_armor") if brain_stats else None
    armor_source = "brain" if target_armor else None
    if not target_armor and ref_rows and ic["armor"]:
        armor_vals = [r[ic["armor"]] for r in ref_rows if r.get(ic["armor"])]
        if armor_vals:
            target_armor = median(armor_vals)
            armor_source = f"live_search({match_stage})"

    target_dps = brain_stats.get("avg_dps") if brain_stats else None
    dps_source = "brain" if target_dps else None
    if not target_dps and ref_rows and ic["dmgmin"] and ic["dmgmax"] and ic["delay"]:
        dps_vals = []
        for r in ref_rows:
            d = r[ic["delay"]] or 0
            if d > 0:
                avg_dmg = ((r[ic["dmgmin"]] or 0) + (r[ic["dmgmax"]] or 0)) / 2
                dps_vals.append(avg_dmg / (d / 1000.0))
        if dps_vals:
            target_dps = median(dps_vals)
            dps_source = f"live_search({match_stage})"

    # An on-equip spell stat (e.g. Rage Reaver's Attack Power +38) is folded
    # into the item's own weighted total BEFORE computing scale_factor, so it
    # gets the same proportional treatment as every plain stat. Unrecognized
    # on-equip spells still contribute a flat itemlevel-based approximation
    # to the total, so scale_factor doesn't underestimate the item's original
    # power just because we can't literally convert what they grant.
    spell_stats, spellid_cols_to_clear, spell_unhandled_notes, unrecognized_spell_count = \
        extract_item_spell_stats(item_row, ic, spell_index)
    spell_weighted_total = sum(
        blizzlike_brain.get_stat_cost(brain, mod, old_item_level) * mag for mod, mag in spell_stats
    )
    unrecognized_spell_budget = unrecognized_spell_count * old_item_level * EQUIP_SPELL_BUDGET_FACTOR

    old_weighted_stats = blizzlike_brain.weighted_stat_total(brain, item_row, ic, old_item_level)
    old_weighted_total = old_weighted_stats + spell_weighted_total + unrecognized_spell_budget

    if target_budget is not None and old_weighted_total:
        scale_factor = target_budget / old_weighted_total
        scale_source = budget_source
    elif old_required_level:
        # Ultimate fallback: no budget data anywhere for this category at
        # all. Scale everything by the plain level ratio instead of leaving
        # the item completely untouched.
        scale_factor = target_level / old_required_level
        scale_source = "heuristic_level_ratio"
        confidence_notes.append("no budget data available (brain or live search) for this category - "
                                 "stats scaled by simple level ratio instead")
    else:
        scale_factor = 1.0
        scale_source = "none"
        confidence_notes.append("could not determine any scale factor - stats left unchanged")

    new_stats = {}
    for st_col, sv_col in zip(ic["stat_types"], ic["stat_values"]):
        old_val = item_row[sv_col] or 0
        new_stats[sv_col] = round(old_val * scale_factor) if old_val else old_val

    # Place the (scaled) spell-derived stats: merge into an existing slot of
    # the same stat type if the item already has one, otherwise claim a free
    # slot. spellid_N is only actually cleared for spells that got placed.
    new_stat_types = {}
    cleared_spell_slots = []
    if spell_stats:
        type_to_value_col = {item_row[st]: sv for st, sv in zip(ic["stat_types"], ic["stat_values"]) if item_row[sv]}
        free_slots = iter([(st, sv) for st, sv in zip(ic["stat_types"], ic["stat_values"])
                            if not (item_row[sv] or 0)])
        placed_all = True
        for item_mod_type, magnitude in spell_stats:
            scaled = round(magnitude * scale_factor)
            if item_mod_type in type_to_value_col:
                sv_col = type_to_value_col[item_mod_type]
                new_stats[sv_col] = new_stats.get(sv_col, 0) + scaled
            else:
                nxt = next(free_slots, None)
                if nxt is None:
                    spell_unhandled_notes.append(
                        f"no free stat slot available to place converted item_mod {item_mod_type}")
                    placed_all = False
                    continue
                st_col, sv_col = nxt
                new_stat_types[st_col] = item_mod_type
                new_stats[sv_col] = scaled
                type_to_value_col[item_mod_type] = sv_col
        if placed_all:
            cleared_spell_slots = spellid_cols_to_clear

    new_armor = None
    if item_class == ITEM_CLASS_ARMOR and ic["armor"]:
        old_armor = item_row[ic["armor"]] or 0
        if target_armor is not None:
            new_armor = round(target_armor)
        elif old_armor:
            new_armor = round(old_armor * scale_factor)
            confidence_notes.append("armor scaled by the stat scale_factor - no direct armor reference available")

    new_dmgmin = new_dmgmax = None
    if item_class == ITEM_CLASS_WEAPON and ic["dmgmin"] and ic["dmgmax"] and ic["delay"]:
        old_delay = item_row[ic["delay"]] or 0
        old_dmgmin, old_dmgmax = item_row[ic["dmgmin"]] or 0, item_row[ic["dmgmax"]] or 0
        old_avg = (old_dmgmin + old_dmgmax) / 2
        dmg_scale = None
        if target_dps is not None and old_delay:
            new_avg = target_dps * (old_delay / 1000.0)
            dmg_scale = (new_avg / old_avg) if old_avg else None
        elif old_avg:
            dmg_scale = scale_factor
            confidence_notes.append("weapon damage scaled by the stat scale_factor - no direct DPS "
                                     "reference available")
        if dmg_scale is not None:
            new_dmgmin = round(old_dmgmin * dmg_scale)
            new_dmgmax = round(old_dmgmax * dmg_scale)

    # RandomProperty is a FIXED, non-scaling bonus (crit/hit/haste/resilience
    # etc. via ItemRandomProperties.dbc) - rather than needing that whole DBC
    # chain, swap it for a RandomSuffix borrowed from one of our own reference
    # items at the target level: RandomSuffix bonuses scale automatically
    # with ItemLevel, so once ItemLevel is already lowered (which we do
    # regardless), the swapped-in bonus shrinks for free. Simple, not exact.
    new_random_property = new_random_suffix = None
    random_note = None
    rp_col, rs_col = ic.get("randomproperty"), ic.get("randomsuffix")
    if rp_col and item_row.get(rp_col):
        substitute = next((r[rs_col] for r in ref_rows if rs_col and r.get(rs_col)), None)
        if substitute:
            new_random_property = 0
            new_random_suffix = substitute
            random_note = (f"RandomProperty {item_row[rp_col]} swapped for RandomSuffix {substitute} "
                            f"borrowed from a level-{target_level} reference item - RandomSuffix bonuses "
                            f"scale automatically with ItemLevel, so this is a simple approximation, not "
                            f"a stat-exact conversion.")
        else:
            random_note = (f"Item has RandomProperty {item_row[rp_col]} but no reference item at this "
                            f"level/category/quality has a RandomSuffix to substitute - left unchanged, "
                            f"review manually.")
    has_unhandled_random_bonus = bool(rp_col and item_row.get(rp_col) and new_random_suffix is None)

    notes = list(confidence_notes)
    if is_quest_sourced:
        notes.append(f"RequiredLevel NOT written (quest reward item) - level {target_level} was only used "
                      f"as a reference to pick the right stat budget/itemlevel target")
    if random_note:
        notes.append(random_note)
    notes.extend(spell_unhandled_notes)
    combined_note = " | ".join(notes) if notes else None

    return {
        "entry": entry,
        "name": item_row[ic["name"]],
        "class": item_class,
        "resolved": True,
        "item_level_source": itemlevel_source,
        "budget_source": scale_source,
        "armor_source": armor_source,
        "dps_source": dps_source,
        "reference_match_stage": match_stage,
        "reference_level_search_radius": radius,
        "reference_sample_size": len(ref_rows),
        "target_level": target_level,
        "old_required_level": old_required_level,
        "new_required_level": target_level,
        "write_required_level": not is_quest_sourced,
        "old_item_level": old_item_level,
        "new_item_level": new_item_level,
        "old_stat_values": {sv: (item_row[sv] or 0) for sv in ic["stat_values"]},
        "new_stat_values": new_stats,
        "new_stat_types": new_stat_types,
        "cleared_spell_slots": cleared_spell_slots,
        "old_armor": item_row[ic["armor"]] if ic["armor"] else None,
        "new_armor": new_armor,
        "old_dmg_min": item_row[ic["dmgmin"]] if ic["dmgmin"] else None,
        "old_dmg_max": item_row[ic["dmgmax"]] if ic["dmgmax"] else None,
        "new_dmg_min": new_dmgmin,
        "new_dmg_max": new_dmgmax,
        "delay_unchanged": item_row[ic["delay"]] if ic["delay"] else None,
        "displayid_unchanged": item_row[ic["displayid"]] if ic["displayid"] else None,
        "new_random_property": new_random_property,
        "new_random_suffix": new_random_suffix,
        "has_unhandled_random_bonus": has_unhandled_random_bonus,
        "has_unhandled_spell_stat": bool(spell_unhandled_notes),
        "low_confidence": bool(confidence_notes),
        "note": combined_note,
        "sources": source_list,
    }


# ---------------------------------------------------------------------------

def analyze_items(conn):
    creature_plan = load_plan("creature_plan")
    quest_plan = load_plan("quest_plan")
    ic = resolve_item_columns(conn)

    print("  Gathering quest reward + creature drop items...")
    item_sources = gather_target_items(conn, creature_plan, quest_plan, ic)
    if not item_sources:
        return {"items": [], "note": "No candidate items found."}
    print(f"  {len(item_sources)} distinct candidate items found across all sources.")

    target_quest_ids = {q["quest_id"] for q in quest_plan.get("quests", [])}
    target_creature_entries = set(
        [r["entry"] for r in creature_plan.get("simple", [])] +
        [r["entry"] for r in creature_plan.get("cloned", [])]
    )
    print("  Checking which candidate items are also used outside the target set (shared-usage check)...")
    shared_items = find_shared_usage(conn, set(item_sources.keys()), target_quest_ids, target_creature_entries)
    print(f"  {len(shared_items)} items are shared with content outside Outland/Northrend and will be cloned.")

    all_ids = list(item_sources.keys())
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM item_template WHERE {ic['entry']} IN ({','.join(str(i) for i in all_ids)})")
        item_rows = {r[ic["entry"]]: r for r in cur.fetchall()}

    print(f"  {len(item_rows)} candidate items found. Building in-memory reference index "
          f"(one-time full scan of weapon/armor items)...")
    ref_index = build_reference_index(conn, ic)
    print(f"  Reference index built ({sum(len(v) for v in ref_index.values())} class/subclass/slot/quality "
          f"buckets). Scoring items...")

    spell_index = spell_lookup.load_spell_index(SPELL_CSV_PATH)
    if spell_index is None:
        print(f"  [WARN] Spell.csv not found at {SPELL_CSV_PATH} - on-equip spell stats "
              f"(e.g. hidden Attack Power) will NOT be converted, only left as-is.")
    else:
        print(f"  Spell.csv loaded ({len(spell_index)} spells indexed).")

    brain = blizzlike_brain.load_brain(BRAIN_PKL_PATH)
    if brain is None:
        print(f"  [WARN] blizzlike_master_brain.pkl not found at {BRAIN_PKL_PATH} (or joblib isn't "
              f"installed) - falling back to live narrow-bucket search only, which may leave more "
              f"items with lower-confidence estimates. Run train_brain.py to build it.")
    else:
        print(f"  blizzlike_master_brain.pkl loaded "
              f"({len(brain.get('slot_budget_curves', {}))} budget curves, "
              f"{'stat-cost weighting available' if brain.get('bracket_stat_costs') else 'NO stat-cost weighting (re-run train_brain.py with the bracket_stat_costs fix to get this)'}).")

    simple, cloned, skipped_not_gear, skipped_ambiguous = [], [], [], []
    processed_ids = get_processed_ids(conn, ITEM_STATE_TABLE)
    already_processed = []

    total = len(item_sources)
    for i, (item_id, sources) in enumerate(item_sources.items(), 1):
        if i % 200 == 0 or i == total:
            print(f"  ...{i}/{total} items scored", flush=True)
        if item_id in processed_ids:
            already_processed.append(item_id)
            continue
        row = item_rows.get(item_id)
        if not row:
            continue
        if row[ic["class"]] not in (ITEM_CLASS_WEAPON, ITEM_CLASS_ARMOR):
            skipped_not_gear.append({"entry": item_id, "name": row[ic["name"]], "class": row[ic["class"]]})
            continue

        quest_sources = [s for s in sources if s["type"] == "quest"]
        # Creature-drop and vendor sources both use the item's own REAL
        # RequiredLevel (old_req + map shift), unlike quest sources which
        # use a "pretend" level - so they share the same resolution bucket.
        creature_or_vendor_sources = [s for s in sources if s["type"] in ("creature", "vendor")]

        # Resolution policy when sources disagree on target level:
        #   1. Quest sourcing always wins over creature-drop/vendor sourcing
        #      (a quest reward shouldn't be leveled off some unrelated mob's
        #      loot table or vendor stock for the same item).
        #   2. Among multiple quests rewarding the same item at different
        #      downscaled levels, the HIGHER level is used as the base.
        #   3. Among multiple creature/vendor sources (no quest sourcing at
        #      all), the same higher-wins rule applies for consistency.
        quest_levels = {s["new_level"] for s in quest_sources}
        creature_levels = set()
        for s in creature_or_vendor_sources:
            old_req = row[ic["reqlevel"]]
            creature_levels.add(max(1, old_req + s["shift"]))

        level_note = None
        if quest_levels:
            target_level = max(quest_levels)
            if len(quest_levels) > 1:
                level_note = (f"Multiple quests reward this item at different downscaled levels "
                               f"{sorted(quest_levels)} - used the higher level ({target_level}) as base.")
            overridden_creature_levels = creature_levels - quest_levels
            if overridden_creature_levels:
                extra = (f"Creature-drop/vendor-derived level(s) {sorted(overridden_creature_levels)} were "
                         f"overridden in favor of quest sourcing.")
                level_note = f"{level_note} {extra}" if level_note else extra
        elif creature_levels:
            target_level = max(creature_levels)
            if len(creature_levels) > 1:
                level_note = (f"Multiple creature/vendor sources imply different downscaled levels "
                               f"{sorted(creature_levels)} - used the higher level ({target_level}) as base.")
        else:
            continue  # no usable sources at all - shouldn't happen given how item_sources is built

        record = build_item_record(ref_index, spell_index, brain, ic, row, target_level, sources)
        if level_note:
            record["note"] = f"{record['note']} | {level_note}" if record.get("note") else level_note

        if item_id in shared_items:
            record["needs_clone"] = True
            cloned.append(record)
        else:
            record["needs_clone"] = False
            simple.append(record)

    return {
        "simple": simple,
        "cloned": cloned,
        "skipped_not_weapon_or_armor": skipped_not_gear,
        "skipped_ambiguous_level": skipped_ambiguous,
        "already_processed_count": len(already_processed),
        "already_processed_item_ids": already_processed,
        "columns": ic,
    }


def _print_item_list(label, items, name_key="name", extra=None, limit=15):
    print(f"  {label}: {len(items)}")
    for rec in items[:limit]:
        extra_str = f" [{extra(rec)}]" if extra else ""
        print(f"    - entry={rec.get('entry')} {rec.get(name_key, '?')}{extra_str}")
    if len(items) > limit:
        print(f"    ... and {len(items) - limit} more (see item_plan.json)")


def main():
    os.makedirs(PLAN_DIR, exist_ok=True)
    with get_connection() as conn:
        print("Analyzing items...")
        item_plan = analyze_items(conn)

    with open(os.path.join(PLAN_DIR, "item_plan.json"), "w") as f:
        json.dump(item_plan, f, indent=2, default=str)

    print("\n=== ITEM SUMMARY ===")
    print(f"  Direct downscale (unshared items): {len(item_plan.get('simple', []))}")

    cloned = item_plan.get("cloned", [])
    if cloned:
        print(f"\n  NOTE: these are only applied if you run executor.py with --include-clones:")
        _print_item_list("Clone-required (shared items)", cloned)

    ambiguous = item_plan.get("skipped_ambiguous_level", [])
    if ambiguous:
        _print_item_list("Skipped (ambiguous target level)", ambiguous,
                          extra=lambda r: f"levels seen: {r.get('candidate_levels')}")

    not_gear = item_plan.get("skipped_not_weapon_or_armor", [])
    if not_gear:
        _print_item_list("Skipped (not weapon/armor class)", not_gear, extra=lambda r: f"class={r.get('class')}")

    print(f"\n  Already processed (skipped):        {item_plan.get('already_processed_count', 0)}")

    all_processed = item_plan.get("simple", []) + item_plan.get("cloned", [])
    unresolved = [i for i in all_processed if not i.get("resolved", True)]
    if unresolved:
        _print_item_list("WARNING: could not process at all", unresolved)

    low_confidence = [i for i in all_processed if i.get("low_confidence")]
    if low_confidence:
        print(f"\n  {len(low_confidence)} items scaled using a lower-confidence fallback "
              f"(no brain/live-search data available - see 'note' per item in item_plan.json):")
        _print_item_list("Low-confidence items", low_confidence, limit=10)

    source_counts = {}
    for i in all_processed:
        source_counts[i.get("budget_source")] = source_counts.get(i.get("budget_source"), 0) + 1
    if source_counts:
        print(f"\n  Budget data source breakdown: {source_counts}")

    random_bonus_items = [i for i in all_processed if i.get("has_unhandled_random_bonus")]
    if random_bonus_items:
        print(f"  -> NOTE: {len(random_bonus_items)} items have a RandomProperty/RandomSuffix bonus "
              f"(crit/hit/haste/resilience etc.) that is NOT rescaled - see 'has_unhandled_random_bonus' "
              f"in the plan file.")
    print(f"\nPlan written to {PLAN_DIR}/item_plan.json")


if __name__ == "__main__":
    main()
