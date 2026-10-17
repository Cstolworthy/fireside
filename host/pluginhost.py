"""Filesystem plugin discovery. See docs/V2-ARCHITECTURE.md.

One plugin = one directory: `plugins/<kind>/<id>/` holding a `plugin.json`
manifest and the entry module it names. Discovery is the only place the host
learns a plugin's identity; nothing above this file imports a plugin by name.

Manifest (plugin.json):
    {"id": "ao3", "kind": "reader", "label": "Archive of Our Own",
     "version": "1.0", "entry": "plugin.py", "class": "Ao3", "order": 20}

`order` decides match precedence for ordered kinds (reader): lower runs first,
and the generic fallback declares a high order so it runs last. Ties keep
directory-name order so discovery is deterministic.
"""

import importlib.util
import json
import os

KINDS = ("reader", "speech", "storage")
_ORDERED = ("reader",)          # kinds where match order is meaningful
_REQUIRED = ("id", "kind", "entry")


class PluginError(Exception):
    """A malformed or unusable plugin directory."""


class LoadedPlugin:
    __slots__ = ("id", "kind", "label", "version", "order", "path", "obj")

    def __init__(self, id, kind, label, version, order, path, obj):
        self.id = id
        self.kind = kind
        self.label = label
        self.version = version
        self.order = order
        self.path = path
        self.obj = obj            # the instance the entry class built

    def __repr__(self):
        return f"<LoadedPlugin {self.kind}:{self.id} v{self.version}>"


def load_plugin(plugin_dir):
    """Read one plugin directory and return a LoadedPlugin, or raise."""
    manifest_path = os.path.join(plugin_dir, "plugin.json")
    if not os.path.isfile(manifest_path):
        raise PluginError(f"no plugin.json in {plugin_dir}")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            man = json.load(fh)
    except (OSError, ValueError) as e:
        raise PluginError(f"bad plugin.json in {plugin_dir}: {e}")

    missing = [k for k in _REQUIRED if not man.get(k)]
    if missing:
        raise PluginError(f"{plugin_dir}: manifest missing {', '.join(missing)}")
    if man["kind"] not in KINDS:
        raise PluginError(f"{plugin_dir}: unknown kind {man['kind']!r}")

    entry = os.path.join(plugin_dir, man["entry"])
    if not os.path.isfile(entry):
        raise PluginError(f"{plugin_dir}: entry {man['entry']!r} not found")

    class_name = man.get("class", "Plugin")
    mod = _load_module(entry, f"fireside_plugin_{man['kind']}_{man['id']}")
    cls = getattr(mod, class_name, None)
    if cls is None:
        raise PluginError(f"{entry}: no class {class_name!r}")
    try:
        obj = cls()
    except Exception as e:                      # a plugin that cannot build is not loadable
        raise PluginError(f"{entry}: {class_name}() failed: {e}")

    return LoadedPlugin(
        id=man["id"], kind=man["kind"], label=man.get("label", man["id"]),
        version=str(man.get("version", "0")), order=int(man.get("order", 100)),
        path=plugin_dir, obj=obj,
    )


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def discover(kind, root):
    """Return the plugins of one kind found under `root/<kind>/`.

    For ordered kinds this is a list sorted by (order, id); for the rest it is a
    dict keyed by id. A directory that fails to load raises - a broken plugin is
    a broken deployment, not something to skip silently.
    """
    if kind not in KINDS:
        raise PluginError(f"unknown kind {kind!r}")
    base = os.path.join(root, kind)
    found = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            if os.path.isfile(os.path.join(d, "plugin.json")):
                found.append(load_plugin(d))
    if kind in _ORDERED:
        return sorted(found, key=lambda p: (p.order, p.id))
    return {p.id: p for p in found}
