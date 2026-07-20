# AzerothCore Level Scaler (Outland -10 / Northrend -20)

A two-part DB-only mod: **brain.py** (read-only analysis, produces JSON plans)
and **executor.py** (applies a reviewed plan, with backups and dry-run).

## Why this works without touching cpp

AzerothCore computes creature HP/mana/armor/damage at runtime from
`creature_classlevelstats` (base stats per class per level) multiplied by
`HealthModifier` / `ManaModifier` / `ArmorModifier` / `DamageModifier` on
`creature_template`. So changing `minlevel`/`maxlevel` alone already gives you
correctly-scaled combat stats for the new level — no manual HP/damage math
needed. This script does not touch the modifier columns, so relative
toughness (a "hard hitting" mob stays proportionally hard-hitting) is
preserved.

Quest XP works the same way, once you check the actual schema: `quest_template`
has no raw `RewardXP` column. It has `RewardXPDifficulty`, an index into the
client's `QuestXP.dbc`, which the core uses (together with `QuestLevel` and
the player's level) to compute XP at runtime — the DB never stores a flat XP
number. So **shifting `QuestLevel` is enough**; the engine recalculates
correct, Blizzard-tuned XP for the new level on its own. No XP field needs to
be written at all.

`RewardMoney`, on the other hand, *is* a flat stored value tuned for the
quest's original level, and the engine does not rescale it for you. Left
alone, a shifted level-48 quest would still pay level-58 gold. So the brain
builds a reference curve — average `RewardMoney` by `QuestLevel` across the
whole table — and rescales each shifted quest's money proportionally:
`new_money = curve[new_level] * (old_money / curve[old_level])`. This
preserves each quest's relative generosity (an escort/elite quest that paid
above-average money still does) while bringing the absolute amount in line
with the new level.

`MinLevel` is shifted by the same amount as `QuestLevel`. `RewardXPDifficulty`
is left untouched — it encodes a relative difficulty tier, not an absolute
level, so it should still mean the same thing after the shift.

## Exporting SQL instead of (or as well as) applying directly

```
python executor.py --export-sql changes.sql                        # write the SQL, don't touch the DB
python executor.py --apply --export-sql changes.sql                 # apply AND keep a copy of the SQL
python executor.py --apply --include-clones --export-sql changes.sql # everything, plus the file
```

This writes every statement the executor would run - backups, creature/quest/
item updates, clone inserts and repoints - fully substituted with real,
properly-escaped values (not `%s` placeholders) into a single standalone
`.sql` file wrapped in `START TRANSACTION; ... COMMIT;`. Useful for reviewing
the exact SQL before it touches anything, keeping an audit trail, or running
it by hand later via HeidiSQL/phpMyAdmin/the `mysql` CLI instead of through
this tool. Works independently of `--apply` - you can generate the file
without writing to the DB at all, or do both in the same run.

## Re-running safely

Every applied change is logged to a small state table
(`level_scaler_creature_state`, `level_scaler_quest_state`,
`level_scaler_item_state` - one row per entry/quest/item id, created
automatically on first `--apply`). `brain.py` and `item_scaler.py` check
these tables and skip anything already logged, so re-running the pipeline
won't re-shift an already-shifted creature, quest, or item. The skip counts
show up in each script's summary output as "Already processed."

This only protects you going forward, though - it can't retroactively know
about changes made before this tracking existed. If you already ran an
earlier version of this tool against your live DB, restore
`creature_template`/`quest_template`/`item_template` from the timestamped
`_backup_` tables the executor created before that run, then do one fresh
run of the full pipeline (`brain.py` -> `item_scaler.py` -> `executor.py`)
so everything gets logged from a clean baseline.

## Restoring to the original (pre-tool) state

```
python restore.py --list                 # see every backup table found, no changes made
python restore.py                         # dry run - shows exactly what it would restore
python restore.py --apply                 # restore everything to its EARLIEST backup (the true original)
python restore.py --apply --only creature_template,quest_template   # restore just these tables
python restore.py --apply --to item_template=20260710_101500         # restore to a specific backup instead
```

Finds every `<table>_backup_<timestamp>` the executor created and restores
each table to its **earliest** backup by default - genuinely back to before
this tool ever touched it, not just undoing the most recent run. All swaps
happen in a single `RENAME TABLE` statement (atomic - all tables succeed or
none do). Your current (modified) tables are never dropped, only renamed to
`<table>_pre_restore_<timestamp>`, so you can double-check before deleting
them yourself. The idempotency state tables (`level_scaler_*_state`) are
cleared automatically as part of a restore, so the restored content is
eligible for `brain.py` again afterward.

One thing this can't undo: if you ran `--include-clones`, cloned rows were
inserted as brand new entries (new creature/item IDs) rather than modifying
existing ones - restoring the backup removes those clone rows entirely
(since the backup predates their creation), but if you separately, manually
repointed a `RewardItem`/`creature_loot_template.Item` column to point at one
of those clone IDs (per the "manual check needed" notes the executor prints),
you'd need to revert that by hand too, since restore.py has no way to know
you made that edit.

## Level floor (excluding edge-case low-level content)

Creatures/quests below level 57 in Outland or level 67 in Northrend are
skipped entirely, not just clamped - content that low is very likely an
outlier (test content, a stray low-level spawn, etc.) rather than actual
zone-appropriate content, and shifting it is more likely to do harm than
good. Adjust `MIN_SOURCE_LEVEL` in `config.py` if you want different floors.
Skipped counts show up in `brain.py`'s summary as "Skipped (below level
floor)", with full detail in the plan JSON files.

## RandomProperty / RandomSuffix and on-equip spell stats

Two more itemization mechanics beyond plain `stat_value_N` are now handled:

**RandomProperty -> RandomSuffix swap.** `RandomProperty` (crit/hit/haste/
resilience etc. via `ItemRandomProperties.dbc`) is a *fixed* bonus that
doesn't scale with anything. Rather than needing the full
`ItemRandomProperties.dbc`/`SpellItemEnchantment.dbc` chain, an item with
`RandomProperty` set gets it swapped for a `RandomSuffix` borrowed from one
of the reference items already found for that class/subclass/slot/quality at
the target level. `RandomSuffix` bonuses scale automatically with the item's
own `ItemLevel` at runtime - and since `ItemLevel` is already being lowered
anyway, the swapped-in bonus shrinks for free. Simple, not stat-exact (as
expected) - if no reference item has a `RandomSuffix` to borrow, the item is
left alone and flagged (`has_unhandled_random_bonus`).

**On-equip "invisible enchant" spells.** Some items grant a stat via an
attached on-equip spell instead of a literal stat line - e.g. Rage Reaver's
Attack Power comes from spell 15808, not a stat_value slot. `item_scaler.py`
reads a local `Spell.csv` (a flat export of Spell.dbc, **not** a database
table - drop it in the same folder as the scripts; path is
`SPELL_CSV_PATH` in `config.py`) and classifies each on-equip spell's
effects. Recognized types (stat, attack power, ranged attack power, magic
spell power) are folded into the item's total stat budget - so they scale
by the exact same `scale_factor` as every other stat - then written into a
literal stat slot (merged into a matching existing slot, or claiming a free
one) and the spell reference is cleared from that item's row. Unrecognized
effects (combat rating bitmasks, resistances, procs) are deliberately left
untouched and flagged (`has_unhandled_spell_stat`) rather than guessed at -
and if a single spell has both recognized and unrecognized effects, *none*
of it is converted, since partially removing a multi-effect spell would
leave it active with only some effects gone.

`item_template.spellid_N` is per-item row data, not a shared pointer like
`RandomProperty` or a shared table like `item_template` itself - clearing it
on one item never affects any other item that happens to reference the same
spell ID, so there's no cloning concern for this part.

If `Spell.csv` isn't found next to the scripts, this feature just degrades
gracefully - on-equip spells are left untouched and `item_scaler.py` prints
a warning, everything else still works.

## The blizzlike brain (replacing the old narrow live search)

Item budget/DPS/armor lookups previously required an *exact* match on
(class, subclass, InventoryType, Quality) within +/-10 levels of the target -
for uncommon combos (staves, fishing poles, rare quality/slot pairings) that
search could come up completely empty, leaving the item untouched. That was
the root cause behind items and stats getting silently skipped.

This is now backed by `train_brain.py` (a separate script, run once against
your live DB) which regresses real budget/stat-cost/DPS/armor curves across
the *entire* `item_template` table and saves them to
`blizzlike_master_brain.pkl`. Curves are continuous across itemlevel
(interpolated, not bucketed), so a lookup essentially always has an answer.

```
python train_brain.py                    # run once against your live DB - produces the .pkl
# drop blizzlike_master_brain.pkl in the same folder as the other scripts
python item_scaler.py                     # now uses it automatically if present
```

If you already have an older `blizzlike_master_brain.pkl` without
`bracket_stat_costs`/`ilvl_brackets` in it, re-run `train_brain.py` - two
fields were added to what it saves so `item_scaler.py` can weight different
stat types by their real regressed cost instead of a flat 1:1 sum.

**Fallback chain per item** (each tier is used only if the one before it has
nothing for that specific item):
1. `blizzlike_master_brain.pkl` curves (robust, interpolated - the primary path)
2. Live narrow-bucket search against `item_template` (the old method, still
   used as backup, and always used to determine the required-level→item-level
   mapping and to source RandomSuffix substitutes)
3. A crude heuristic: scale everything by the plain `new_level / old_level`
   ratio, preserving the item's own itemlevel/required-level relationship

Tier 3 means **an item is essentially never left completely untouched
anymore** - worst case, it gets a simple proportional scale instead of a
budget-informed one. Every item's plan entry now records `item_level_source`,
`budget_source`, `armor_source`, `dps_source` (which tier supplied that
number) and `low_confidence` (true if any tier-3 fallback was used anywhere
for that item), so you can see exactly how much to trust each result rather
than it being an opaque pass/fail. `item_scaler.py`'s summary output surfaces
low-confidence items and a source-tier breakdown directly in the console.

If `blizzlike_master_brain.pkl` isn't found (or `joblib` isn't installed),
everything still works via tiers 2/3 - it just degrades to the old behavior
rather than failing.

## RequiredLevel is reference-only for quest reward items

Quest reward items don't have a real level requirement of their own - the
quest's downscaled level is used *internally* to pick the right stat budget/
itemlevel target, but it's never written to the item's actual `RequiredLevel`
column for items sourced only from a quest. Writing it would add an
artificial equip-lock that wasn't there before - a reward you just earned
should stay wearable immediately. Mob-drop items are different:
`RequiredLevel` is real, player-visible data there, and does get scaled down
along with everything else. If an item is sourced from *both* a quest and a
creature drop, it's treated as quest-sourced (RequiredLevel left alone) -
tell me if you'd rather that case go the other way. Each item's plan entry
records `write_required_level` (`true`/`false`) so you can see which rule
applied.

## Ambiguous target level resolution

If an item is sourced from multiple quests/creatures that imply different
downscaled levels, this is resolved deterministically instead of being
skipped:
1. Quest sourcing always wins over creature-drop sourcing.
2. Among multiple quests rewarding the same item, the HIGHER downscaled
   level is used as the base.
3. Among multiple creature sources only (no quest sourcing at all), the same
   higher-wins rule applies.

The old `skipped_ambiguous_level` bucket still exists in the plan JSON for
backward compatibility but should now always be empty - every item resolves
to a level. When a conflict was resolved this way, the item's `note` field
records exactly what was overridden and why, so it's still auditable.

## Quest reward items now scale off MinLevel, not QuestLevel

An item's "pretend required level" (used to pick the stat budget/itemlevel
target) is now the quest's downscaled `MinLevel` (the real level required to
*accept* the quest), not downscaled `QuestLevel` (the content/design level
the quest was tuned around). These are frequently different -
`QuestLevel=60`/`MinLevel=58` is common - and `QuestLevel` alone
systematically overstated how strong a reward should be, since it doesn't
reflect the level a player actually receives it at. Falls back to
`QuestLevel`-derived behavior when `MinLevel` is 0/unset, since an
unpopulated `MinLevel` isn't a meaningful signal on its own.

## Dungeons (leveling only, no heroics/raids)

The map registry now covers Outland, Northrend, and 31 TBC/WotLK leveling
dungeons (see `config.TARGET_MAPS` for the full list with names). Everything
else - `brain.py`, `item_scaler.py`, `executor.py` - needed no changes, since
they already worked purely off the `LEVEL_SHIFT`/`MAP_NAMES`/
`MIN_SOURCE_LEVEL` dicts keyed by map id, not the map count.

Two dungeon-list exclusions from the raw `Map.csv` `InstanceType=1` set:
"Opening of the Dark Portal" (a scripted event zone, not a real 5-man) and
"Sunwell Fix (Unused)" (an explicit dev leftover).

**Heroic mode needed no special exclusion.** In this schema, a heroic-
difficulty creature lives in a completely separate `creature_template` row
(referenced via `difficulty_entry_1/2/3` on the normal-mode row) that is
never itself placed in the `creature` spawn table - the engine swaps to it
internally based on the party's chosen difficulty. Since this tool only ever
reaches creature_template entries through actual spawns, heroic-only data is
structurally unreachable already. Raids are excluded simply by not being
`InstanceType=1`.

One real bug found and fixed while wiring this up: `brain.py`'s
`POIContinent` quest-mapping query had a hardcoded 2-placeholder `IN (%s, %s)`
that only worked for exactly 2 target maps - would have broken outright
against 33. Fixed to scale with any map count.

## Spell nerf system (fully wired)

```
python brain.py                # must run first - creature_plan.json is the input
python spell_nerf.py            # produces plans/spell_plan.json
python executor.py --apply      # applies creatures/quests/items/spells together, or:
python executor.py --skip-creatures --skip-quests --skip-items --apply   # spells only
```

Finds every damage/heal spell cast by creatures already in
`creature_plan.json`, from two sources:

- **SmartAI** (`smart_scripts`, `action_type=11`/`SMART_ACTION_CAST`) for
  regular trash mobs. Handles negative `entryorguid` (a row pinned to one
  specific spawn rather than every copy of a template) by resolving it back
  to a `creature_template` entry via `creature.guid`.
- **Boss scripts** - dungeon/raid boss casting logic lives in hand-written
  C++ (`src/server/scripts/.../boss_xyz.cpp`), not the database. Copy every
  boss `.cpp` file you want covered into the `scripts/` folder (flat, not
  recursive) next to the other scripts. `boss_script_parser.py` extracts
  spell IDs from each file's `enum ...Spells... { NAME = 12345, ... }`
  block and matches the AI struct name against `creature_template.ScriptName`
  to find the corresponding creature. A handful of older scripts declare
  spell IDs as raw numeric literals instead of an enum - those are flagged
  (`has_inline_numeric_casts`) for manual review rather than guessed at via
  a wide, false-positive-prone numeric regex.

**Damage spells** are rescaled to deal the same % of a player's HP at the
mob's new level as they did at the mob's old level, using `HP_Values.ods`'s
leveling curve (see "Spell nerf math" below). **Healing spells** scale off
the mob's own HP change instead, via `creature_classlevelstats` (same
ratio-cancels-HealthModifier technique used elsewhere in this project) - if
your `creature_classlevelstats` schema doesn't match the assumed column
names, this quietly falls back to no heal scaling (ratio 1.0) rather than
guessing wrong; check this specifically against your first real run, it's
the least-verified piece of this system.

**Same-ID overwrite, no cloning.** `spell_dbc` overrides the native
`Spell.dbc` for that ID everywhere it's referenced - by design, per your
instruction, this tool writes the nerf directly to the same spell ID rather
than cloning to a new one. If `spell_dbc` already has an override row for
that ID, only the changed effect columns are updated (preserving whatever
else that override customized); otherwise a full row is inserted from
`Spell.csv`'s native definition. If a spell is *also* cast by a creature
outside the current target set, that's flagged (`has_outside_caster`) for
visibility but does not block the nerf.

**Duplicate casters** (the same spell ID cast by multiple different
creatures at different levels) resolve via the same "higher wins" rule used
for ambiguous item levels - the highest original level among the casters is
used as the basis for the whole nerf, and the `note` field records what was
overridden.

## Spell nerf math

`spell_nerf_math.py` classifies each spell effect as damage, heal, or
neither (buffs/debuffs/procs/etc. are always left alone), and computes the
nerfed magnitude:

- Damage: `new_magnitude = (old_magnitude / player_hp_at_old_level) *
  player_hp_at_new_level`, using the "green gear, itemlevel-irrelevant"
  leveling curve from `HP_Values.ods` (levels 48-80 - the endgame/heroic-
  gear/pre-raid-BIS/raid-phase rows further down that file are ignored,
  since heroics/raids aren't in scope). Verified against the worked
  example: 1000 damage at level 65 (14062 HP) -> 361 damage at level 55
  (5071 HP).
- Heal: `new_magnitude = old_magnitude * (new_mob_hp / old_mob_hp)`.
- Any roll variance (`EffectDieSides_N`) or per-level scaling
  (`EffectRealPointsPerLevel_N`) the original spell had is scaled by the
  same ratio, not flattened to a fixed number.

## Vendor-sold items

`item_scaler.py` now also finds weapons/armor sold by vendor creatures
(`npc_vendor`) already in `creature_plan.json`, and downscales them the same
way mob-drop items are handled: the item's own current `RequiredLevel` minus
the map's shift (real, player-visible data - not a "pretend" level like
quest rewards). Reuses the exact same budget/stat-scaling machinery as
everything else in `item_scaler.py` - no new scoring logic needed.

**Level-cap gear is deliberately excluded** - vendor items sitting exactly
at RequiredLevel 70 (TBC) or 80 (WotLK) are skipped entirely, not gathered
at all. That's almost always pre-raid BIS, reputation, PvP, or Tier-
equivalent gear that needs its own, more specific formula rather than the
general leveling-content treatment. Adjust `EXPANSION_CAP_LEVEL` in
`config.py` if you want different cutoffs.

Vendor items participate in the same ambiguous-level and shared-usage
handling as everything else: quest sourcing still wins over vendor sourcing
if the same item is somehow both, multiple vendors implying different
levels resolve via the same "higher wins" rule as creature-drop items, and
an item sold by a vendor *outside* the target set gets flagged for cloning
rather than mutated directly - same pattern as loot-table sharing.

## Setup




```
pip install pymysql
```

Credentials default to `127.0.0.1:3306`, user `acore`, password `acore`,
db `acore_world` (as given). Override via env vars if needed:
`AC_DB_HOST`, `AC_DB_PORT`, `AC_DB_USER`, `AC_DB_PASSWORD`, `AC_DB_NAME`.

## Workflow

```
python brain.py
```
This only reads. It writes `plans/creature_plan.json` and
`plans/quest_plan.json` and prints a summary, including any items it couldn't
confidently resolve (flagged, not guessed).

**Read the plan files before executing anything.** Especially:
- `creature_plan.json -> skipped_multi_target`: entries spawning in *both*
  Outland and Northrend under one template ID (shift amount is ambiguous).
- `creature_plan.json -> cloned`: entries also spawned outside Outland/Northrend
  (shared trash models) — these need a template clone rather than a direct edit.
- `quest_plan.json` entries with a non-null `note` — no money curve reference
  was available for that level, so `RewardMoney` was left untouched; review
  manually.

Then:
```
python executor.py                              # dry run — prints every statement, writes nothing
python executor.py --apply                       # applies the "simple" cases (asks for confirmation)
python executor.py --apply --include-clones       # also clones + repoints shared templates
```

The executor creates timestamped backup tables
(`creature_template_backup_YYYYMMDD_HHMMSS`, etc.) before writing anything,
and runs inside a single transaction — any failure rolls everything back.

## Scope of this first pass

- Creatures: open-world spawns (`creature` table) in map 530 (Outland) / 571
  (Northrend), excluding `CreatureType = 8` (Critter). Instance/dungeon
  populations aren't filtered out separately yet — if you want overworld only,
  I can add a zone/instance-map exclusion list next.
- Quests: mapped to a continent via `quest_poi.MapID` first, falling back to
  quest giver location (`creature_questrelation` / `gameobject_questrelation`
  joined to spawn map). Quests only reachable via item-triggered starts
  (`quest_template.StartItem`) aren't spatially resolvable this way and won't
  appear in the plan — flag if you want those handled too.
- Items: explicitly out of scope per your request.

## Item downgrading (weapons/armor)

`item_scaler.py` finds every weapon/armor item awarded by a plan quest or
dropped by a plan creature and works out a downscaled version:

```
python brain.py           # must run first - produces creature/quest plans
python item_scaler.py     # reads those plans, produces plans/item_plan.json
python executor.py --apply --include-clones   # applies everything, including item clones
```

**Target level:**
- Quest reward items have no level of their own, so the quest's already-
  downscaled `new_level` is used directly.
- Creature drop items: the item's own current `RequiredLevel` minus 10
  (Outland-sourced) or minus 20 (Northrend-sourced).

**Budget:** for each item, the brain searches `item_template` for items of
the same `class`/`subclass`/`InventoryType`/`Quality` already sitting at the
target level (widening the level search outward if nothing matches exactly)
and takes the median stat total / armor / DPS / item level from that
reference set. The item being downscaled is then rescaled so its stat total
matches that reference budget, while the *ratio* between its own stats is
preserved (20 STR / 10 STA at scale factor 0.5 becomes 10 STR / 5 STA).
`displayid` and weapon `delay` (swing timer) are never touched.

**Reference matching now falls back progressively** if the exact class/
subclass/slot/quality bucket is empty near the target level (common for
rarer weapon types like staves/wands/fishing poles): first same subclass at
any slot, then same class at any subclass - Quality is always preserved
since it's the single biggest value driver. Each item's plan entry records
which stage actually matched (`reference_match_stage`), so you can see when
a broader fallback was used.

**Known gap: RandomProperty/RandomSuffix bonuses aren't rescaled.** Some
items carry secondary stats (crit/hit/haste/resilience, "of the X" suffixes)
via a pointer into `ItemRandomProperties.dbc`/`ItemRandomSuffix.dbc` rather
than a plain number in `stat_value_N` - this tool only touches the latter.
Any item with `RandomProperty` or `RandomSuffix` set gets flagged
(`has_unhandled_random_bonus: true`, with a note) in the plan so it's not
silently missed, but the bonus itself will be oversized relative to the
item's new level until this is built out further. Doing that properly needs
`ItemRandomProperties.dbc`, `ItemRandomSuffix.dbc`, and
`SpellItemEnchantment.dbc` exported as CSVs - let me know if you want to
tackle that next.

 an item also awarded by a quest, or dropped by a creature,
outside the target set is never mutated directly - it gets flagged for
cloning to a new entry, same as shared creature templates. The executor's
`--include-clones` handles the clone insert, but **does not** yet auto-rewrite
which `RewardItem`/`RewardChoiceItemID` column on the quest or which
`creature_loot_template.Item` row should point at the new entry - it prints
exactly what needs repointing and where, but leaves the repoint SQL as a
manual (or scripted-by-you) step for now, since getting the exact source
column wrong would be worse than flagging it.

**Known gaps in this pass:**
- `creature_loot_template` rows pointing at a `Reference` (shared loot groups
  like "World Loot Level 24") are now expanded via `reference_loot_template`,
  one level deep - a reference group that itself points at another reference
  isn't followed further (rare in practice).
- Shared-usage detection (deciding clone vs. direct edit) now checks both
  direct `creature_loot_template.Item` matches AND reference-group matches -
  if an item is reachable through a reference group that ANY creature outside
  the target set also points at, it's treated as shared and cloned rather
  than mutated. Since reference groups like "World Loot Level 24" tend to be
  used very broadly across the whole game, expect most reference-sourced
  items to end up in the clone bucket - that's the safe, correct outcome.
- Fishing, skinning, disenchanting, and pickpocket loot tables still aren't
  checked for "is this item used elsewhere" - an item could theoretically get
  downscaled even though it's also reachable via one of those. Let me know if
  you want those added too.
- `item_template` schema names vary a lot between AC revisions; column
  resolution follows the same defensive pattern as the rest of the project,
  but this module additionally *requires* matched `stat_type_N`/`stat_value_N`
  pairs (not just `stat_value_N`) to know which attribute it's scaling -
  it'll raise a clear error rather than guess if those aren't present.


- `creature.id1` vs `creature.id` as the template FK (both are checked for automatically).
- `quest_template.RewardXP` vs `RewXP` as the XP column (both checked automatically).
- `quest_poi.MapID` existing and being populated — if your DB dump predates
  POI data, the giver-location fallback carries more weight; check
  `map_source` in the quest plan to see which method resolved each quest.

If brain.py raises a `RuntimeError` about missing columns, paste me the error
and I'll add your revision's naming to the candidate list.
