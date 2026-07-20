# AzerothCore Level 60 Scaler

A database-only mod for [AzerothCore](https://www.azerothcore.org/) (WotLK
3.3.5) that downscales TBC and WotLK content so it plays as sub-60 leveling
content instead of endgame content: **-10 levels for Outland/TBC**, **-20
levels for Northrend/WotLK**, applied consistently across creatures, quests,
items, and mob-cast spells. Pure SQL - no C++, no client patches, no core
recompile.

> **⚠️ Heads up: this project is "vibecoded."** I'm not a programmer and
> don't claim any coding skill or experience. Every script in this repo was
> written by an AI (Claude) based on my instructions, iterated over a long
> back-and-forth conversation, and tested against real exports from my own
> database along the way - but I can't personally vouch for the code the
> way an experienced developer could. Read the [Known Limitations &
> Roadmap](#known-limitations--roadmap) section, **always run a dry run and
> review the plan files before applying anything**, and take backups
> seriously. Use at your own risk, on a test server first.

---

## Table of Contents

- [What this actually does](#what-this-actually-does)
- [Feature status map](#feature-status-map)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Workflow](#workflow)
- [Script reference](#script-reference)
- [Design notes](#design-notes)
- [Safety net & recovery](#safety-net--recovery)
- [Known limitations & roadmap](#known-limitations--roadmap)
- [Troubleshooting](#troubleshooting)

---

## What this actually does

The idea: keep TBC and WotLK's much better itemization, spell design, and
zone/quest design, but let a level-48-60 character experience Outland and
Northrend content as *leveling* content instead of endgame content. Instead
of hitting level 58 and going to Hellfire Peninsula, this pushes that
content down to roughly level 48, and Northrend down to roughly level 40-60
depending on the zone/dungeon, so the whole 1-60 experience uses the
better-designed expansion content instead of vanilla's.

Everything is applied as plain SQL `UPDATE`/`INSERT` statements against your
`acore_world` database. `spell_dbc` already works as an override table (it
overwrites the native `Spell.dbc` file for any spell ID present in it), so
even spell rebalancing needs no client-side or C++ changes.

## Feature status map

| Category | Status | Notes |
|---|---|---|
| Open-world creatures (Outland & Northrend) | ✅ Working | Level, HP, damage all auto-scale via the engine once level changes |
| Leveling dungeons (5-mans, normal mode) | ✅ Working | 31 TBC/WotLK dungeons covered |
| Quests (level, XP, money rewards) | ✅ Working | Uses `MinLevel`, not the more commonly-inflated `QuestLevel` |
| Quest reward items | ✅ Working | Stats/armor/weapon damage scaled, `RequiredLevel` deliberately left untouched |
| Mob-drop items | ✅ Working | Includes reference/shared loot groups |
| Vendor-sold items | ✅ Working | Excludes level-cap (70/80) gear - see below |
| Mob-cast spells (damage/heal) | ✅ Working | Both SmartAI (trash) and hardcoded boss `.cpp` scripts |
| **Heroic dungeons** | 🚧 Not started | Deliberately excluded so far - needs a harder-hitting, still-challenging-at-the-new-level formula, not a straight port of the normal-mode nerf |
| **Raids** | 🚧 Not started | Same reasoning as heroics, at larger scale |
| **Professions** (recipes, crafted gear, trainers) | 🚧 Not started | Not addressed at all yet |
| **World objects** (chests, herb/ore nodes, non-creature loot sources) | 🚧 Not started | Only creature-sourced and vendor-sourced items are handled right now |
| **Reputation gear** | 🚧 Not started | Vendor items sitting at the level cap (70 TBC / 80 WotLK) are explicitly *skipped*, not scaled - this is exactly where most rep gear lives, and it needs its own formula |
| **Enchants** | 🚧 Not started | Not addressed |
| **Gems** | 🚧 Not started | Not addressed |

Everything in the "not started" list is either explicitly excluded by the
current scripts (heroics, raids, level-cap vendor gear) or just hasn't been
built yet (professions, world objects, enchants, gems). Nothing in that list
gets silently mangled by what exists today - it's just untouched.

## How it works

Two engine-level insights make this a pure-DB mod:

1. **Creature stats aren't stored as flat numbers.** AzerothCore computes a
   creature's HP/mana/armor/damage at runtime from `creature_classlevelstats`
   (base stats per level per class) times `HealthModifier`/`ManaModifier`/
   `ArmorModifier`/`DamageModifier` on `creature_template`. Changing
   `minlevel`/`maxlevel` alone gets you correctly-scaled combat stats for
   free - a mob that hit hard for its level keeps hitting proportionally
   hard after the shift.
2. **`spell_dbc` is an override table.** Any spell ID present in it
   overrides the native `Spell.dbc` everywhere that ID is referenced. So
   rebalancing a mob's spell damage is just writing new values to that
   table for the spell IDs in question - no client patch needed.

Everything else (items, quest XP, budgets) is built on top of these two
facts, using real data from your own database as the source of truth rather
than hardcoded formulas wherever possible - see [Design
notes](#design-notes).

## Requirements

- **Python 3.9+**
- **A MySQL/MariaDB connection** to your `acore_world` database (default
  assumed: `127.0.0.1:3306`, user/pass `acore`/`acore` - override via env
  vars, see [Setup](#setup))
- **Python packages** (`pip install -r requirements.txt`):
  - `PyMySQL` - database connectivity
  - `joblib` - loads the pre-trained item-budget model
  - `pandas` + `odfpy` - reads the HP-by-level spreadsheet
- **Local files you provide**, placed in the same folder as the scripts:
  - `Spell.csv` - a flat CSV export of `Spell.dbc` (comma-delimited).
    Needed for on-equip item spell conversion and mob spell nerfing.
  - `HP_Values.ods` - average player HP by level (leveling gear, levels
    48-80). Needed for damage spell nerfing. See the "Values for Spell
    Damage" sheet format in the file itself.
  - `scripts/` folder - a **flat** folder of boss AI `.cpp` files (however
    many you want covered), copied from your server's source tree. Needed
    to discover which spells dungeon bosses cast, since that logic lives in
    compiled C++, not the database.
  - `blizzlike_master_brain.pkl` - *optional*, produced by running
    `train_brain.py` once against your live DB. Makes item scaling
    significantly more robust; everything still works without it, just
    with lower-confidence fallbacks on rarer item categories.

## Setup

```bash
pip install -r requirements.txt
```

Credentials default to `127.0.0.1:3306`, user `acore`, password `acore`, db
`acore_world`. Override with environment variables if needed:
`AC_DB_HOST`, `AC_DB_PORT`, `AC_DB_USER`, `AC_DB_PASSWORD`, `AC_DB_NAME`.

Drop `Spell.csv`, `HP_Values.ods`, and a `scripts/` folder of boss `.cpp`
files in the same directory as the Python scripts. Then, optionally:

```bash
python train_brain.py   # one-time (or occasional) - builds blizzlike_master_brain.pkl
```

## Workflow

```
1. python brain.py
   Read-only. Analyzes creatures + quests across Outland, Northrend, and
   every leveling dungeon. Writes plans/creature_plan.json + quest_plan.json.

2. python item_scaler.py
   Read-only. Requires step 1's output. Writes plans/item_plan.json.
   Review the "cloned" list (needs --include-clones later) and anything
   flagged low-confidence.

3. python spell_nerf.py
   Read-only. Requires step 1's output. Writes plans/spell_plan.json.
   Review anything flagged has_outside_caster.

4. python executor.py
   Dry run by default - writes a reviewable .sql file, changes nothing.
   Sanity-check everything from steps 1-3 together before applying.

5. python executor.py --apply --include-clones
   Actually writes to the DB (asks for a typed "YES" confirmation).
   Creates backup tables first. Applies creatures, quests, items, and
   spells together in one pass.
```

Steps 1→2→3→4→5 must run in that order every time - each reads the
previous step's output. Useful variations:

```bash
# Much faster than --apply for large runs: write SQL, then bulk-import it
python executor.py --apply --include-clones --export-sql changes.sql
mysql -h <host> -u <user> -p <db> < changes.sql

# Apply only spell nerfs, skip everything else
python executor.py --apply --skip-creatures --skip-quests --skip-items

# See what's already been touched, or undo everything
python restore.py --list
python restore.py --apply
```

## Script reference

| Script | Role |
|---|---|
| `config.py` | All settings: DB credentials, the map registry (which continents/dungeons are in scope and their level shift), file paths, state table names |
| `db.py` | Shared DB helpers: connections, defensive column-name resolution, idempotency state-table helpers |
| `brain.py` | Read-only. Finds and plans creature/quest changes |
| `item_scaler.py` | Read-only. Finds and plans item changes (quest rewards, mob drops, vendor stock) |
| `spell_nerf.py` | Read-only. Finds and plans mob-cast spell changes |
| `executor.py` | Applies any/all of the above plans to the live DB, or exports them as a standalone `.sql` file. Dry-run by default |
| `restore.py` | Restores tables to their pre-tool state from the backups `executor.py` creates |
| `spell_lookup.py` | Parses `Spell.csv`, classifies on-equip item spells (e.g. hidden Attack Power), builds `spell_dbc`-ready rows with correct column names/types |
| `spell_nerf_math.py` | Pure math: reads `HP_Values.ods`, classifies spell effects as damage/heal/neither, computes nerfed magnitudes |
| `boss_script_parser.py` | Parses boss `.cpp` files for spell IDs (from `enum Spells {...}` blocks) and the AI struct name (→ `creature_template.ScriptName`) |
| `blizzlike_brain.py` | Loads the pre-trained item-budget model (`blizzlike_master_brain.pkl`) and does curve interpolation |
| `train_brain.py` | Builds `blizzlike_master_brain.pkl` from a full scan of your live `item_template` table. Run standalone, occasionally, not part of the regular workflow |

## Design notes

Brief explanations of the non-obvious decisions baked into this project -
useful if something looks wrong and you want to know if it's intentional.

**Creatures & quests**
- Creatures/quests below level 57 (Outland) or 67 (Northrend) are left
  completely untouched - very likely low-level outlier content, not core
  zone content.
- Heroic-mode creatures need no special exclusion: they live in a
  completely separate `creature_template` row (via `difficulty_entry_1/2/3`)
  that's never itself placed in the `creature` spawn table, so they're
  structurally unreachable by this tool already.
- Quest reward items use the quest's `MinLevel`, not `QuestLevel` -
  `QuestLevel` is the content's *design* level and is frequently inflated
  above the level actually required to get the quest (e.g.
  `QuestLevel=60`/`MinLevel=58` is common), which systematically overstated
  reward power before this was caught.
- When a quest/item/spell has multiple, disagreeing sources (e.g. two
  quests reward the same item at different levels), the resolution rule is
  consistent everywhere: **quest sourcing beats creature/vendor sourcing,
  and the highest level among agreeing sources wins.**

**Items**
- Item budgets/stats/DPS/armor are sourced from a chain of fallbacks, each
  used only if the previous one has nothing for that specific item: a
  pre-trained model (`blizzlike_master_brain.pkl`, built from your whole
  item table, interpolated so it essentially always has an answer) → a live
  narrow database search → a crude proportional-scaling fallback. An item is
  essentially never left completely untouched, but every plan entry records
  which tier supplied its numbers (`budget_source`, `item_level_source`,
  etc.) and flags `low_confidence` when a weaker fallback was used.
- Quest reward items never get their `RequiredLevel` written - it's used
  internally to pick the right stat budget, but writing it would add an
  artificial equip-lock to a reward the player just earned. Mob-drop and
  vendor items are different: `RequiredLevel` is real, player-visible data
  there, and does get scaled.
- `RandomProperty` (crit/hit/haste/resilience via `ItemRandomProperties.dbc`
  - a *fixed*, non-scaling bonus) gets swapped for a `RandomSuffix` borrowed
  from a same-category reference item at the target level, since
  `RandomSuffix` bonuses scale automatically with `ItemLevel` at runtime.
  Simple, not stat-exact.
- On-equip "invisible enchant" spells (e.g. an item granting Attack Power
  via an attached spell instead of a literal stat, like Rage Reaver) are
  read from `Spell.csv`, classified, folded into the item's stat budget so
  they scale the same as everything else, converted to a literal stat, and
  the spell reference is cleared from that item's row.
- Vendor items sitting exactly at the expansion's level cap (70 TBC, 80
  WotLK) are skipped entirely - this is almost always pre-raid BIS,
  reputation, PvP, or Tier-equivalent gear that needs its own formula, not
  the general leveling treatment.
- An item shared with something outside the current target set (another
  creature's loot, a vendor, a quest) is never mutated directly - it's
  flagged for cloning (`needs_clone: true`, with a `shared_with` field
  naming exactly what the outside reference is) and only touched if you run
  `executor.py --include-clones`.

**Spells**
- Damage spells are rescaled to deal the same % of a player's HP at the
  mob's new level as they did at its old level, using `HP_Values.ods`'s
  leveling curve. Healing spells scale off the mob's own HP change instead
  (via `creature_classlevelstats`), not the player curve.
- Same-ID overwrite in `spell_dbc`, no cloning - a spell used by a creature
  outside the target set gets flagged (`has_outside_caster`) for visibility
  but is still nerfed, since `spell_dbc` overrides that ID everywhere it's
  used and there's no way to scope an override to just one caster.
- When the same spell is cast by multiple creatures at different levels,
  the highest original level is used as the basis (same rule as items).

## Safety net & recovery

- **Every `--apply` run backs up the tables it's about to touch first**
  (`<table>_backup_<timestamp>`).
- **Idempotent by default** - every applied change is logged to a small
  state table, so re-running the pipeline won't re-shift something already
  shifted. This only protects you going forward; if you ran an early
  version of this tool before this tracking existed, restore from backups
  first.
- **`restore.py`** finds every backup and restores each table to its
  *earliest* backup by default - genuinely back to before this tool ever
  touched it, not just undoing the most recent run.
- **`--export-sql`** (automatic on a dry run) gives you a full, reviewable,
  standalone `.sql` file before anything touches the live database.

## Known limitations & roadmap

Beyond the categories in the [feature status map](#feature-status-map):

- **`creature_classlevelstats` heal-scaling lookup is the least-verified
  piece of the spell system** - the exact HP column name hasn't been
  confirmed against a live export the way every other table in this project
  has been. It falls back to no scaling rather than guessing wrong, but
  check it against your first real run.
- **Boss scripts using raw numeric spell IDs instead of an `enum Spells`
  block** aren't parsed (flagged as `has_inline_numeric_casts` for manual
  review, not silently missed).
- **`Spell.csv`/DB exports produced on non-English-locale machines** (comma
  as decimal separator) are handled defensively, but if you hit a new
  "data truncated" style SQL error, it's worth checking whether a similar
  locale-formatting issue slipped through somewhere new.
- Only creature-sourced and vendor-sourced items are scanned for sharing;
  fishing/skinning/disenchant/pickpocket loot tables aren't checked.

If you're extending this yourself (or asking an AI to), the pattern used
throughout is: **verify schema against a real export before writing SQL
that touches it**, prefer a reviewable JSON plan step before any DB write,
and default every destructive operation to a dry run.

## Troubleshooting

- **`ImportError` / `ModuleNotFoundError` for a local module**: you likely
  have a stale/incomplete copy of the scripts. Re-download the full set
  rather than patching one file.
- **"Data truncated for column X" SQL errors**: almost always a
  locale-formatting (decimal comma) issue in a source CSV export - see
  Known Limitations above.
- **An item/spell you expected to change didn't**: check the relevant plan
  JSON file first. Items/spells route into `simple` (applied automatically),
  `cloned` (needs `--include-clones`), or a `skipped_*` bucket with a
  reason - nothing is silently dropped without a recorded reason somewhere
  in the plan.
