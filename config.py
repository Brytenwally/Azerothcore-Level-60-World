"""
Configuration for the AzerothCore Level Scaler.

Override credentials with env vars if you don't want them hardcoded:
  AC_DB_HOST, AC_DB_PORT, AC_DB_USER, AC_DB_PASSWORD, AC_DB_NAME
"""
import os

DB_HOST = os.environ.get("AC_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("AC_DB_PORT", "3306"))
DB_USER = os.environ.get("AC_DB_USER", "acore")
DB_PASSWORD = os.environ.get("AC_DB_PASSWORD", "acore")
DB_NAME = os.environ.get("AC_DB_NAME", "acore_world")

EXPANSION_TBC = 1
EXPANSION_WOTLK = 2

# Per-expansion level shift and "below this original level, don't touch it
# at all" floor. Applied uniformly across every map tagged with that
# expansion, whether open-world continent or dungeon.
EXPANSION_SHIFT = {
    EXPANSION_TBC: -10,
    EXPANSION_WOTLK: -20,
}
EXPANSION_MIN_SOURCE_LEVEL = {
    EXPANSION_TBC: 57,
    EXPANSION_WOTLK: 67,
}

# Vendor-sold gear sitting exactly at the expansion's level cap is usually
# pre-raid BIS / reputation / PvP / Tier-equivalent gear - deliberately
# excluded from the general vendor nerf pass since it needs its own,
# more specific formula later rather than the same treatment as leveling gear.
EXPANSION_CAP_LEVEL = {
    EXPANSION_TBC: 70,
    EXPANSION_WOTLK: 80,
}

# Every map this tool will touch: map_id -> (display_name, expansion).
# Backward-compatible MAP_OUTLAND/MAP_NORTHREND constants still exist below
# for anything that references them by name directly.
#
# Dungeon list built from Map.csv (InstanceType=1, ExpansionID in (1,2)),
# with two exclusions:
#   - 269 "Opening of the Dark Portal": a scripted escort/event zone, not a
#     queueable 5-man dungeon.
#   - 598 "Sunwell Fix (Unused)": explicitly an unused dev leftover.
# Heroic mode does NOT need separate exclusion here - in this schema,
# heroic-difficulty creatures live in a completely separate creature_template
# row (referenced via difficulty_entry_1/2/3 on the normal row) that is never
# itself placed in the `creature` spawn table, so it's already unreachable by
# this tool's spawn-based gathering. Raids are excluded simply by not being
# InstanceType=1.
TARGET_MAPS = {
    # --- Open world continents ---
    530: ("Outland", EXPANSION_TBC),
    571: ("Northrend", EXPANSION_WOTLK),

    # --- TBC leveling dungeons ---
    540: ("Hellfire Citadel: The Shattered Halls", EXPANSION_TBC),
    542: ("Hellfire Citadel: The Blood Furnace", EXPANSION_TBC),
    543: ("Hellfire Citadel: Ramparts", EXPANSION_TBC),
    545: ("Coilfang Reservoir: The Steamvault", EXPANSION_TBC),
    546: ("Coilfang Reservoir: The Underbog", EXPANSION_TBC),
    547: ("Coilfang Reservoir: The Slave Pens", EXPANSION_TBC),
    552: ("Tempest Keep: The Arcatraz", EXPANSION_TBC),
    553: ("Tempest Keep: The Botanica", EXPANSION_TBC),
    554: ("Tempest Keep: The Mechanar", EXPANSION_TBC),
    555: ("Auchindoun: Shadow Labyrinth", EXPANSION_TBC),
    556: ("Auchindoun: Sethekk Halls", EXPANSION_TBC),
    557: ("Auchindoun: Mana-Tombs", EXPANSION_TBC),
    558: ("Auchindoun: Auchenai Crypts", EXPANSION_TBC),
    560: ("Old Hillsbrad Foothills: The Escape From Durnholde", EXPANSION_TBC),
    585: ("Magisters' Terrace", EXPANSION_TBC),

    # --- WotLK leveling dungeons ---
    574: ("Utgarde Keep", EXPANSION_WOTLK),
    575: ("Utgarde Pinnacle", EXPANSION_WOTLK),
    576: ("The Nexus", EXPANSION_WOTLK),
    578: ("The Oculus", EXPANSION_WOTLK),
    595: ("Caverns of Time: The Culling of Stratholme", EXPANSION_WOTLK),
    599: ("Halls of Stone", EXPANSION_WOTLK),
    600: ("Drak'Tharon Keep", EXPANSION_WOTLK),
    601: ("Azjol-Nerub", EXPANSION_WOTLK),
    602: ("Halls of Lightning", EXPANSION_WOTLK),
    604: ("Gundrak", EXPANSION_WOTLK),
    608: ("Violet Hold", EXPANSION_WOTLK),
    619: ("Ahn'kahet: The Old Kingdom", EXPANSION_WOTLK),
    632: ("The Forge of Souls", EXPANSION_WOTLK),
    650: ("Trial of the Champion", EXPANSION_WOTLK),
    658: ("Pit of Saron", EXPANSION_WOTLK),
    668: ("Halls of Reflection", EXPANSION_WOTLK),
}

MAP_OUTLAND = 530
MAP_NORTHREND = 571

# Derived, kept as the SAME dict shapes the rest of the codebase already
# depends on (brain.py, item_scaler.py, executor.py all key off these three
# directly) - nothing downstream needs to change to pick up the new maps.
MAP_NAMES = {map_id: name for map_id, (name, exp) in TARGET_MAPS.items()}
LEVEL_SHIFT = {map_id: EXPANSION_SHIFT[exp] for map_id, (name, exp) in TARGET_MAPS.items()}
MIN_SOURCE_LEVEL = {map_id: EXPANSION_MIN_SOURCE_LEVEL[exp] for map_id, (name, exp) in TARGET_MAPS.items()}
MAP_CAP_LEVEL = {map_id: EXPANSION_CAP_LEVEL[exp] for map_id, (name, exp) in TARGET_MAPS.items()}

# CreatureType enum: 8 = Critter. These are never touched.
CREATURE_TYPE_CRITTER = 8

MIN_LEVEL_FLOOR = 1
MIN_QUEST_LEVEL_FLOOR = 1

# Offset used when a creature_template/item_template entry must be cloned
# because it's shared with content outside what we're rescaling. Bumped up
# further per-clone within a single run if it collides with an existing entry.
CLONE_ENTRY_OFFSET = 900000

# Local CSV export of Spell.dbc (NOT a database table), expected in the same
# folder as brain.py/item_scaler.py. Used to classify what an item's on-equip
# spell actually grants (e.g. Rage Reaver's hidden Attack Power), so it can be
# converted into an equivalent literal stat and scaled like any other stat.
# Resolved relative to the script directory so it works regardless of the
# working directory the scripts are launched from.
SPELL_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Spell.csv")

# Pre-trained budget/stat-cost/DPS/armor model (a joblib pickle, NOT a database
# table) built by a separate train_brain.py script run against the live DB.
# Used to make item downscaling far more robust than a narrow live bucket
# search, which can come up empty for uncommon item categories.
BRAIN_PKL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blizzlike_master_brain.pkl")

# Local ODS export of average player HP by level (levels 48-80, leveling
# green gear - itemlevel-irrelevant), used to nerf mob-cast damage spells:
# a spell is assumed to have been tuned against a player of the mob's OLD
# level, so its magnitude is rescaled to deal the same % of a new-level
# player's HP. Healing spells instead scale off the MOB's own HP change
# (via creature_classlevelstats), not this table.
HP_VALUES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "HP_Values.ods")

# Folder of boss AI .cpp files (a flat folder, NOT a database table or the
# AzerothCore source tree) - boss casting logic lives in compiled C++, not
# SmartAI, so this is the only way to discover what spells a boss uses.
BOSS_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")

PLAN_DIR = "plans"

# Tables used to remember what's already been processed, so re-running the
# brain doesn't silently re-shift already-shifted rows.
CREATURE_STATE_TABLE = "level_scaler_creature_state"
QUEST_STATE_TABLE = "level_scaler_quest_state"
ITEM_STATE_TABLE = "level_scaler_item_state"
SPELL_NERF_STATE_TABLE = "level_scaler_spell_nerf_state"
