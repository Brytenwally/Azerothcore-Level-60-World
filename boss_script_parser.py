"""
BOSS SCRIPT PARSER
==================
Dungeon/raid BOSS casting logic in AzerothCore lives in hand-written C++
script files (src/server/scripts/.../boss_xyz.cpp), not in smart_scripts -
SmartAI generally only covers regular trash mobs. This module treats a
folder of those .cpp files as a local, offline data source (like Spell.csv
or the blizzlike brain pickle - NOT a database table) and extracts:

  - the AI struct name (e.g. "boss_omor_the_unscarred"), which by AzerothCore
    convention is exactly what's stored in creature_template.ScriptName for
    the corresponding creature - this is the join key back to the DB.
  - every spell ID referenced in an "enum ...Spells... { NAME = 12345, ... }"
    block, which is the overwhelmingly dominant convention for how these
    scripts declare the spell IDs they cast.

What this does NOT attempt: understanding the *logic* of when/how often a
spell is cast (cooldowns, phases, chance-based branches) - only which spell
IDs exist in the file. For our purposes (nerfing the spell's own damage/heal
magnitude in spell_dbc) that's all we need; we're not trying to rewrite the
boss's rotation, just make whatever it already casts hit for less.

Known limitation: a handful of older/nonstandard scripts declare spell IDs
as raw numeric literals inline (e.g. DoCastSelf(12345)) instead of through an
enum. Those won't be picked up by this parser. Flagged in the return value
via has_inline_numeric_casts so those files can be reviewed by hand rather
than silently missing spells.
"""
import os
import re

# Matches "struct <Name> : public <SomeBaseClass>" - the AI struct name is
# the ScriptName convention used almost universally in AzerothCore scripts.
STRUCT_RE = re.compile(r"struct\s+(\w+)\s*:\s*public\s+\w+")

# Matches "enum <AnythingContainingSpells> { ... }" (case-insensitive on
# "spell"), non-greedy so it doesn't swallow past the first closing brace.
ENUM_SPELLS_RE = re.compile(r"enum\s+\w*[Ss]pells?\w*\s*\{(.*?)\}", re.DOTALL)

# Within an enum body: NAME = NUMBER (comments after // or /* are stripped
# from each line first).
ENUM_ENTRY_RE = re.compile(r"(\w+)\s*=\s*(\d+)")

# Heuristic detector for inline numeric spell IDs Blizzard/AC authors
# sometimes use instead of an enum constant, e.g. DoCastSelf(12345) or
# DoCast(target, 12345). Flags the file for manual review rather than
# silently trusting a wide numeric-literal regex, which would have a very
# high false-positive rate against health values, timers, etc.
INLINE_CAST_RE = re.compile(r"\bDoCast\w*\s*\([^)]*?(\d{4,6})\s*\)")


def parse_cpp_file(path):
    """Returns {"script_name": str|None, "spell_ids": [int, ...],
    "spell_names": {id: enum_const_name}, "has_inline_numeric_casts": bool}
    for a single .cpp file."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()

    struct_match = STRUCT_RE.search(text)
    script_name = struct_match.group(1) if struct_match else None

    spell_ids = []
    spell_names = {}
    for enum_body in ENUM_SPELLS_RE.findall(text):
        # strip line comments so "SPELL_X = 123, // was 456" doesn't pick up 456
        cleaned = re.sub(r"//.*", "", enum_body)
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
        for name, num in ENUM_ENTRY_RE.findall(cleaned):
            spell_id = int(num)
            spell_ids.append(spell_id)
            spell_names[spell_id] = name

    # Only flag the inline-numeric-cast heuristic when we found NO enum
    # spells at all in the file - if we already got spells from an enum, a
    # DoCast(..., someExpression) match is almost certainly still just
    # referencing an enum constant somewhere in the same expression, not a
    # genuine bare numeric literal we're missing.
    has_inline = (not spell_ids) and bool(INLINE_CAST_RE.search(text))

    return {
        "script_name": script_name,
        "spell_ids": sorted(set(spell_ids)),
        "spell_names": spell_names,
        "has_inline_numeric_casts": has_inline,
        "file": os.path.basename(path),
    }


def parse_cpp_folder(folder_path):
    """Parses every .cpp file in folder_path (not recursive - keep boss
    scripts in one flat folder). Returns a list of parse_cpp_file() results,
    one per file. Files with no recognizable struct name are still included
    (script_name: None) so they show up for manual review rather than
    silently vanishing."""
    results = []
    if not os.path.isdir(folder_path):
        return results
    for fname in sorted(os.listdir(folder_path)):
        if fname.lower().endswith(".cpp"):
            results.append(parse_cpp_file(os.path.join(folder_path, fname)))
    return results
