"""
selection.json: the choices made in the dashboard (`clean ui`) for one
`clean dupes` run, kept in that run's folder. `clean remove` acts on them
when it is given no rules of its own.

    {"format": 1,
     "rules": {"base": "rules", "keep_in": [], "remove_from": [], "rank": null,
               "rank_folders": false, "merge": false, "conflicts": "both",
               "per_group": "all", "exts": []},
     "choices": {"d12": "keep", "f345": "remove"},
     "updated": "2026-10-08T14:03:11"}

"rules" are the auto-select settings (see cleanup/selection.py; a null rank
means the setting remove.rank). "choices" are the boxes ticked or cleared by
hand, by folder row ("d") or file number ("f") of the run's manifest, so they
stay right when the drive is mounted somewhere else. keep_in and remove_from
hold paths as the scan saw them.
"""

import json
import os
import time

from cleanup import selection
from cleanup.actions.remove import CONFLICTS
from cleanup.fsutil import atomic_write, is_within

NAME = "selection.json"
FORMAT = 1

DEFAULT_RULES = {"base": "rules", "keep_in": [], "remove_from": [], "rank": None,
                 "rank_folders": False, "merge": False, "conflicts": "both",
                 "per_group": "all", "exts": []}


class Saved:
    def __init__(self, rules=None, choices=None, updated=None):
        self.rules = dict(DEFAULT_RULES, **(rules or {}))
        self.choices = dict(choices or {})
        self.updated = updated

    # ------------------------------------------------------------ reading

    @classmethod
    def from_obj(cls, obj):
        """Check what a file holds; raises ValueError saying what is wrong."""
        if not isinstance(obj, dict) or obj.get("format") != FORMAT:
            raise ValueError("it is not a selection file this version of clean can read")
        rules = obj.get("rules", {})
        if not isinstance(rules, dict):
            raise ValueError("its rules are not a JSON object")
        out = {}
        for key, value in rules.items():
            if key not in DEFAULT_RULES:
                continue                # from a newer clean: ignored
            out[key] = check_rule(key, value)
        choices = obj.get("choices", {})
        if not isinstance(choices, dict):
            raise ValueError("its choices are not a JSON object")
        selection.parse_choices(choices)
        updated = obj.get("updated")
        return cls(out, choices, updated if isinstance(updated, str) else None)

    def to_obj(self):
        return {"format": FORMAT, "rules": self.rules,
                "choices": dict(sorted(self.choices.items(), key=lambda kv: (kv[0][0],
                                                                             int(kv[0][1:])))),
                "updated": self.updated}

    def is_default(self):
        return self.rules == DEFAULT_RULES and not self.choices

    # ------------------------------------------------------------ use

    def to_rules(self, rank_setting, mounts=(), moves=None):
        """selection.Rules for these choices. `moves` {old root: new root}:
        where the scanned folders are now, if their drive moved."""
        r = self.rules
        rank = r["rank"] if r["rank"] is not None else rank_setting
        return selection.Rules(
            keep_in=[moved(p, moves) for p in r["keep_in"]],
            remove_from=[moved(p, moves) for p in r["remove_from"]],
            rank=selection.parse_rank(rank), family_rank=r["rank_folders"],
            merge=r["merge"], mounts=mounts, base=r["base"], per_group=r["per_group"],
            exts=r["exts"], choices=self.choices)


def check_rule(key, value):
    """A rule's value, normalised, or ValueError."""
    if key in ("keep_in", "remove_from"):
        if not isinstance(value, list) or not all(
                isinstance(p, str) and os.path.isabs(p) for p in value):
            raise ValueError(f"its {key} is not a list of full paths")
        return list(dict.fromkeys(p.rstrip("/") or "/" for p in value))
    if key == "rank":
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError("its rank is not a list of rank rules")
        return selection.check_rank(value)
    if key in ("rank_folders", "merge"):
        if not isinstance(value, bool):
            raise ValueError(f"its {key} is not true or false")
        return value
    if key == "exts":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError("its exts is not a list of extensions")
        return sorted({x.strip().lower().lstrip(".") for x in value if x.strip(" .")})
    allowed = {"base": selection.BASES, "per_group": selection.PER_GROUP,
               "conflicts": CONFLICTS}[key]
    if value not in allowed:
        raise ValueError(f"its {key} is {value!r}, not one of {', '.join(allowed)}")
    return value


def moved(path, moves):
    for old, new in (moves or {}).items():
        if is_within(path, old):
            return new.rstrip("/") + path[len(old.rstrip("/")):] if path != old else new
    return path


def load(run):
    """The choices saved for `run`, or None if there are none. Raises
    ValueError if the file can't be used."""
    try:
        with open(run.file(NAME), encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"{NAME} can't be read ({e})")
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{NAME} is not valid JSON ({e})")
    try:
        return Saved.from_obj(obj)
    except ValueError as e:
        raise ValueError(f"{NAME}: {e}")


def save(run, saved):
    saved.updated = time.strftime("%Y-%m-%dT%H:%M:%S")
    text = json.dumps(saved.to_obj(), ensure_ascii=False, separators=(",", ":"))
    atomic_write(run.file(NAME), (text + "\n").encode("utf-8", "surrogateescape"))


def clear(run):
    try:
        os.unlink(run.file(NAME))
    except FileNotFoundError:
        pass
