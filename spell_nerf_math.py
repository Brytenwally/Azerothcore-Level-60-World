"""
SPELL NERF MATH
===============
Pure calculation module for nerfing mob-cast damage/heal spells to match a
downscaled mob's new level. No DB access here - just the HP curve loader and
the classification/magnitude formulas, so all of it is directly testable.

Damage spells: assumed to have been tuned against a player of the mob's OLD
level. Rescaled to deal the same % of a player's HP at the mob's NEW level,
using the average-HP-by-level curve from HP_Values.ods (the "green gear,
itemlevel-irrelevant" leveling curve specifically - endgame/heroic/raid rows
in that file are irrelevant here since heroics/raids aren't in scope).

Healing spells: rescaled off the MOB'S OWN HP change (old creature HP -> new
creature HP), not the player curve - a self/ally heal's size should track the
caster's own power level, not an assumed player target.

Spells that don't do direct damage or healing (buffs, debuffs, procs, mind
control, etc.) are left completely untouched - classify_spell_effects simply
won't return anything for those effect slots.
"""
import os

# Verified against AzerothCore's own source (SharedDefines.h / SpellAuraDefines.h) -
# see the effort earlier in this project pulling these directly from
# https://github.com/azerothcore/azerothcore-wotlk
SPELL_EFFECT_SCHOOL_DAMAGE = 2
SPELL_EFFECT_APPLY_AURA = 6
SPELL_EFFECT_HEAL = 10
SPELL_AURA_PERIODIC_DAMAGE = 3
SPELL_AURA_PERIODIC_HEAL = 8


def load_hp_curve(path):
    """Reads the 'green gear, itemlevel-irrelevant' leveling HP table from
    HP_Values.ods. Returns a sorted list of {"level": X, "hp": Y} or None if
    the file/pandas+odfpy aren't available - callers should treat that as
    'skip spell nerfing entirely', not crash."""
    if not os.path.exists(path):
        print(f"  [HP_CURVE] File does not exist at: {path}")
        return None
    try:
        import pandas as pd
    except ImportError as e:
        print(f"  [HP_CURVE] Could not import pandas: {e}. Run: pip install pandas odfpy")
        return None

    try:
        df = pd.read_excel(path, engine="odf", sheet_name=0)
    except ImportError as e:
        print(f"  [HP_CURVE] Could not read the .ods file - odfpy is likely missing: {e}. "
              f"Run: pip install odfpy")
        return None
    except Exception as e:
        print(f"  [HP_CURVE] Failed to read {path}: {type(e).__name__}: {e}")
        return None

    # The sheet has several stacked tables (leveling curve, then endgame/
    # heroic-gear/pre-raid-BIS/raid-phase reference blocks further down,
    # separated by blank rows). Only the FIRST block - rows whose
    # "Itemlevel comment" says gear is irrelevant - is the leveling curve we
    # want; the rest is endgame-only and not relevant while heroics/raids are
    # out of scope. Stop at the first blank/non-matching row.
    nodes = []
    for _, row in df.iterrows():
        comment = str(row.get("Itemlevel comment", ""))
        level = row.get("Level")
        hp = row.get("HP", row.get("HP "))  # tolerate the trailing-space column name seen in the real file
        if pd.isna(level) or "irrelevant" not in comment.lower():
            if nodes:
                break  # already collected the leveling block, hit the next section
            continue  # skip any leading blank/header noise
        nodes.append({"level": int(_safe_float(level)), "hp": _safe_float(hp)})

    if not nodes:
        print(f"  [HP_CURVE] Read {path} successfully but found zero matching rows - the sheet layout "
              f"or column names may not match what's expected ('Level', 'Itemlevel comment', 'HP'). "
              f"Columns found: {list(df.columns)}")
        return None

    nodes.sort(key=lambda n: n["level"])
    return nodes or None


def interpolate_hp(hp_curve, level):
    """Linear interpolation between the two bracketing level nodes; clamps
    to the nearest endpoint if level is outside the sampled range."""
    if not hp_curve:
        return None
    if level <= hp_curve[0]["level"]:
        return hp_curve[0]["hp"]
    if level >= hp_curve[-1]["level"]:
        return hp_curve[-1]["hp"]
    for i in range(len(hp_curve) - 1):
        lo, hi = hp_curve[i], hp_curve[i + 1]
        if lo["level"] <= level <= hi["level"]:
            if hi["level"] == lo["level"]:
                return lo["hp"]
            frac = (level - lo["level"]) / (hi["level"] - lo["level"])
            return lo["hp"] + frac * (hi["hp"] - lo["hp"])
    return hp_curve[-1]["hp"]  # safety net, unreachable in practice


def _safe_float(v, default=0.0):
    """Parses a float defensively, tolerating European-locale decimal commas
    (e.g. "2,5" meaning 2.5, not a thousands separator - single-value DBC
    fields are never big enough to need real thousands separators)."""
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


def _as_int(v, default=0):
    try:
        return int(_safe_float(v, default))
    except (TypeError, ValueError):
        return default


def classify_spell_effects(spell_row):
    """Returns a list of (effect_slot, kind, magnitude) for every effect slot
    that's a direct damage/heal or periodic damage/heal effect - kind is
    "damage" or "heal". magnitude is EffectBasePoints+1 (DBC convention: the
    stored value is the real value minus 1). Effects that are anything else
    (buffs, debuffs, procs, dummy effects, etc.) are simply not included -
    this is not a partial/unhandled distinction like the item on-equip-spell
    classifier; a spell can freely have some damage/heal slots and some
    unrelated slots, and only the former are ever touched."""
    results = []
    for k in (1, 2, 3):
        effect = _as_int(spell_row.get(f"Effect_{k}", 0))
        base_points = _as_int(spell_row.get(f"EffectBasePoints_{k}", 0))
        magnitude = base_points + 1

        if effect == SPELL_EFFECT_SCHOOL_DAMAGE:
            results.append((k, "damage", magnitude))
        elif effect == SPELL_EFFECT_HEAL:
            results.append((k, "heal", magnitude))
        elif effect == SPELL_EFFECT_APPLY_AURA:
            aura = _as_int(spell_row.get(f"EffectAura_{k}", 0))
            if aura == SPELL_AURA_PERIODIC_DAMAGE:
                results.append((k, "damage", magnitude))
            elif aura == SPELL_AURA_PERIODIC_HEAL:
                results.append((k, "heal", magnitude))
    return results


def nerf_damage_magnitude(old_magnitude, old_mob_level, new_mob_level, hp_curve):
    """Rescales a damage magnitude to deal the same % of a player's HP at the
    mob's new level as it did at the mob's old level. Returns None if the HP
    curve has nothing usable (caller should leave the spell untouched, not
    guess)."""
    old_hp = interpolate_hp(hp_curve, old_mob_level)
    new_hp = interpolate_hp(hp_curve, new_mob_level)
    if not old_hp:
        return None
    pct = old_magnitude / old_hp
    return round(pct * new_hp)


def nerf_heal_magnitude(old_magnitude, old_mob_hp, new_mob_hp):
    """Rescales a heal magnitude by the mob's own HP change ratio."""
    if not old_mob_hp:
        return old_magnitude
    ratio = new_mob_hp / old_mob_hp
    return round(old_magnitude * ratio)
