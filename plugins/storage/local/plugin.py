"""v2 storage plugin: local. The reference backend behind the v2 contract.

No logic here - LocalBackend is the real object, imported unchanged. The plugin
tree is what the host discovers; nothing above host/pluginhost.py imports a
plugin by name.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from storage.local import LocalBackend as _Impl


class Storage(_Impl):
    pass
