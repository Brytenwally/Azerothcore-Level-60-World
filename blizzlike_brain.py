"""
BLIZZLIKE BRAIN LOADER
======================
Consumes blizzlike_master_brain.pkl - a pre-trained model built by a separate
script (train_brain.py) that regresses real budget/stat-cost/DPS/armor curves
across the ENTIRE item_template table, keyed by (class, subclass,
InventoryType, Quality) and indexed by itemlevel.

Why this replaces the old live narrow-bucket search: the old approach in
item_scaler.py queried for items matching an EXACT (class, subclass,
InventoryType, Quality) combo within +/-10 levels of the target - for
uncommon combos (rare weapon subtypes, rare quality tiers at a given slot)
that search could come up completely empty, leaving the item unresolved and
untouched. These curves are built once across the whole item population and
interpolated continuously across itemlevel, so a lookup essentially always
has an answer - not just for the exact levels sampled in the training data.

This is NOT a database table - it's a local file (a joblib pickle) expected
in the same folder as the other scripts, produced by running train_brain.py
against the live DB separately.
"""
import os

try:
    import joblib
except ImportError:
    joblib = None


def load_brain(path):
    """Returns the master_brain dict, or None if the file/joblib isn't
    available - callers should treat None as 'fall back to the old method',
    not crash."""
    if joblib is None:
        return None
    if not os.path.exists(path):
        return None
    try:
        return joblib.load(path)
    except Exception:
        return None


def _interpolate(nodes, itemlevel, value_key):
    """nodes: sorted list of {"itemlevel": X, value_key: Y}. Linear
    interpolation between the two bracketing nodes; clamps to the nearest
    endpoint if itemlevel is outside the sampled range entirely."""
    if not nodes:
        return None
    if itemlevel <= nodes[0]["itemlevel"]:
        return nodes[0][value_key]
    if itemlevel >= nodes[-1]["itemlevel"]:
        return nodes[-1][value_key]

    for i in range(len(nodes) - 1):
        lo, hi = nodes[i], nodes[i + 1]
        if lo["itemlevel"] <= itemlevel <= hi["itemlevel"]:
            if hi["itemlevel"] == lo["itemlevel"]:
                return lo[value_key]
            frac = (itemlevel - lo["itemlevel"]) / (hi["itemlevel"] - lo["itemlevel"])
            return lo[value_key] + frac * (hi[value_key] - lo[value_key])
    return nodes[-1][value_key]  # unreachable in practice, safety net


def get_budget(brain, item_class, subclass, invtype, quality, itemlevel):
    """Returns (budget, source_label) or (None, None) if the brain has
    nothing usable for this combo at all (caller should fall back further)."""
    if not brain:
        return None, None

    slot_curves = brain.get("slot_budget_curves", {})
    key = (item_class, subclass, invtype, quality)
    nodes = slot_curves.get(key)
    if nodes:
        val = _interpolate(nodes, itemlevel, "avg_budget")
        if val is not None:
            return val, "slot_budget_curve"

    global_curves = brain.get("global_budget_curves", {})
    nodes = global_curves.get((invtype, quality))
    if nodes:
        val = _interpolate(nodes, itemlevel, "avg_budget")
        if val is not None:
            return val, "global_budget_curve"

    return None, None


def get_lookup_stats(brain, item_class, subclass, invtype, quality, itemlevel):
    """Returns a dict with avg_dps/avg_armor/avg_block interpolated at
    itemlevel, or None if nothing usable exists for this combo."""
    if not brain:
        return None
    lookup_db = brain.get("lookup_database", {})
    sheet = lookup_db.get((item_class, subclass, invtype, quality))
    if not sheet:
        return None

    out = {}
    for field in ("avg_dps", "avg_armor", "avg_block", "avg_sell_price"):
        # only interpolate over nodes where this particular field is meaningfully populated
        usable_nodes = [n for n in sheet if n.get(field, 0)]
        if usable_nodes:
            out[field] = _interpolate(usable_nodes, itemlevel, field)
    return out or None


def get_stat_cost(brain, stat_type, itemlevel):
    """Returns the regressed relative cost of one point of this stat type at
    this itemlevel, or 1.0 (neutral/uniform) if the brain has no opinion -
    same default behavior as if no brain were loaded at all."""
    if not brain:
        return 1.0
    brackets = brain.get("ilvl_brackets")
    costs = brain.get("bracket_stat_costs")
    if not brackets or not costs:
        return 1.0

    bracket_idx = len(brackets) - 1
    for idx, (lo, hi) in enumerate(brackets):
        if lo <= itemlevel < hi:
            bracket_idx = idx
            break

    return costs.get(bracket_idx, {}).get(stat_type, 1.0)


def weighted_stat_total(brain, item_row, ic, itemlevel):
    """Sum of stat_value_N * get_stat_cost(stat_type_N, itemlevel) across all
    populated stat slots - the cost-aware equivalent of a flat sum. With no
    brain loaded, get_stat_cost always returns 1.0, so this is identical to a
    flat sum (same behavior as before the brain existed)."""
    total = 0.0
    for st_col, sv_col in zip(ic["stat_types"], ic["stat_values"]):
        sval = item_row.get(sv_col) or 0
        stype = item_row.get(st_col) or 0
        if sval and stype:
            total += sval * get_stat_cost(brain, stype, itemlevel)
    return total
