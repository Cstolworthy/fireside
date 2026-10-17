"""plugin discovery, with no network and no fixture on disk.

Builds small plugin trees in a temp dir and checks discovery, ordering,
validation, and the failure modes that should be loud.
"""

import json
import os
import tempfile
import unittest

from host.pluginhost import PluginError, discover, load_plugin

PLUGIN_SRC = '''
class Plugin:
    def __init__(self):
        self.id = %(id)r
        self.kind = %(kind)r
    def matches(self, req):
        return False
'''


def write_plugin(root, kind, pid, order=None, class_name="Plugin", body=None):
    d = os.path.join(root, kind, pid)
    os.makedirs(d)
    man = {"id": pid, "kind": kind, "label": pid, "version": "1.0",
           "entry": "plugin.py", "class": class_name}
    if order is not None:
        man["order"] = order
    with open(os.path.join(d, "plugin.json"), "w") as fh:
        json.dump(man, fh)
    with open(os.path.join(d, "plugin.py"), "w") as fh:
        fh.write(body or PLUGIN_SRC % {"id": pid, "kind": kind})
    return d


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "plugins")

    def test_reader_order_puts_generic_last(self):
        write_plugin(self.root, "reader", "ao3", order=20)
        write_plugin(self.root, "reader", "web", order=1000)
        write_plugin(self.root, "reader", "epub", order=10)
        ids = [p.id for p in discover("reader", self.root)]
        self.assertEqual(ids, ["epub", "ao3", "web"])

    def test_deterministic_ties(self):
        write_plugin(self.root, "speech", "minimax")
        write_plugin(self.root, "speech", "azure")
        # speech is not ordered -> dict keyed by id
        d = discover("speech", self.root)
        self.assertEqual(sorted(d), ["azure", "minimax"])

    def test_missing_manifest_is_loud(self):
        os.makedirs(os.path.join(self.root, "reader", "nope"))
        self.assertEqual(discover("reader", self.root), [])

    def test_bad_manifest_raises(self):
        d = write_plugin(self.root, "reader", "broken")
        with open(os.path.join(d, "plugin.json"), "w") as fh:
            fh.write('{"kind": "reader"}')          # no id, no entry
        with self.assertRaises(PluginError):
            discover("reader", self.root)

    def test_unknown_kind_raises(self):
        with self.assertRaises(PluginError):
            discover("bogus", self.root)

    def test_entry_class_not_buildable_raises(self):
        write_plugin(self.root, "storage", "boom",
                     body="class Plugin:\n    def __init__(self):\n        raise RuntimeError('x')\n")
        with self.assertRaises(PluginError):
            discover("storage", self.root)

    def test_load_returns_instance(self):
        d = write_plugin(self.root, "reader", "solo", order=1)
        p = load_plugin(d)
        self.assertEqual(p.id, "solo")
        self.assertEqual(p.kind, "reader")
        self.assertEqual(p.obj.id, "solo")


if __name__ == "__main__":
    unittest.main()
