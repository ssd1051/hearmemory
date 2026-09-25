"""hearmemory.config -- config.toml load / render / merge.

Reads with the stdlib tomllib (Python >= 3.11). Writes with a tiny renderer that
only needs to support str/int/float/bool/list[str] (interfaces.DEFAULT_CONFIG).
Unknown keys are kept as-is; keys whose type does not match the default fall back
to the default value (both are reported back via `load_config_report` for `doctor`).
"""
from __future__ import annotations

import copy
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple, Union

from .interfaces import HEARMEMORY_DIRNAME, DEFAULT_CONFIG, LAYOUT

PathLike = Union[str, "Path"]


def _deep_copy_config(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(dict(cfg))


def _same_kind(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return True


def deep_merge(default: Mapping[str, Any], override: Mapping[str, Any],
               path: str = "") -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Merge `override` onto `default`. Returns (merged, unknown_keys, type_mismatches)."""
    merged = _deep_copy_config(default)
    unknown: List[str] = []
    mismatched: List[str] = []
    if not isinstance(override, Mapping):
        return merged, unknown, mismatched
    for key, value in override.items():
        full_key = f"{path}.{key}" if path else str(key)
        if key not in merged:
            merged[key] = copy.deepcopy(value)
            unknown.append(full_key)
            continue
        dv = merged[key]
        if isinstance(dv, dict) and isinstance(value, dict):
            sub_merged, sub_unknown, sub_mismatch = deep_merge(dv, value, full_key)
            merged[key] = sub_merged
            unknown.extend(sub_unknown)
            mismatched.extend(sub_mismatch)
        elif _same_kind(dv, value):
            merged[key] = copy.deepcopy(value)
        else:
            mismatched.append(full_key)
    return merged, unknown, mismatched


def config_path(root: PathLike) -> Path:
    return Path(root) / HEARMEMORY_DIRNAME / LAYOUT["config"]


def load_config(root: PathLike) -> Dict[str, Any]:
    merged, _, _ = load_config_report(root)
    return merged


def load_config_report(root: PathLike) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Same as load_config but also returns (unknown_keys, type_mismatches) for `doctor`."""
    path = config_path(root)
    raw: Dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            raw = {}
    merged, unknown, mismatched = deep_merge(DEFAULT_CONFIG, raw)
    project = merged.setdefault("project", {})
    if not project.get("name"):
        project["name"] = Path(root).resolve().name
    return merged, unknown, mismatched


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    raise TypeError(f"unsupported TOML value type: {type(value)!r}")


def render_default_config(config: Mapping[str, Mapping[str, Any]] = None) -> str:
    """Render `config` (default: interfaces.DEFAULT_CONFIG) as commented TOML text."""
    config = config if config is not None else DEFAULT_CONFIG
    lines = [
        "# hearmemory config.toml -- generated defaults, edit freely.",
        "# Unknown keys are kept; keys with the wrong type fall back to their default (see `hearmemory doctor`).",
        "",
    ]
    for section, values in config.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_scalar(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_default_config(root: PathLike) -> Path:
    path = config_path(root)
    path.write_text(render_default_config(), encoding="utf-8")
    return path


def set_config_value(root: PathLike, section: str, key: str, value: Any) -> bool:
    """Set ONE `key = value` in `[section]` of an existing config.toml, keeping every other line (comments,
    unknown keys, user edits) as it is. Returns False (writes nothing) when there is no config.toml or the
    file does not parse afterwards. Used by `hearmemory init` so [hosts] enabled matches what was installed."""
    path = config_path(root)
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    new_line = f"{key} = {_toml_scalar(value)}"
    lines = text.splitlines()
    header = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(?:#.*)?$")
    key_re = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    start = next((i for i, ln in enumerate(lines) if (m := header.match(ln)) and m.group(1) == section), None)
    if start is None:
        lines += ["", f"[{section}]", new_line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines))
        idx = next((i for i in range(start + 1, end) if key_re.match(lines[i])), None)
        if idx is None:
            lines.insert(start + 1, new_line)
        else:
            lines[idx] = new_line
    out = "\n".join(lines).rstrip() + "\n"
    try:
        parsed = tomllib.loads(out)
    except tomllib.TOMLDecodeError:
        return False
    if (parsed.get(section) or {}).get(key) != (list(value) if isinstance(value, tuple) else value):
        return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, path)
    return True
