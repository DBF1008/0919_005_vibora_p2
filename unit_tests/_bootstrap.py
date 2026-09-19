"""
Test bootstrap: makes the pure-Python ``vibora.templates`` package importable
without triggering ``vibora/__init__.py`` (which imports modules depending on
compiled Cython extensions), and applies Python 3.10+ compatibility shims.
"""
import collections
import collections.abc
import importlib.util
import pathlib
import sys
import types

# Python 3.10 removed collections.Callable; the templates package targets 3.6.
if not hasattr(collections, 'Callable'):
    collections.Callable = collections.abc.Callable

if 'vibora' not in sys.modules or not hasattr(sys.modules['vibora'], '__path__'):
    vibora_root = pathlib.Path(__file__).resolve().parent.parent / 'vibora'
    vibora_pkg = types.ModuleType('vibora')
    vibora_pkg.__path__ = [str(vibora_root)]
    sys.modules['vibora'] = vibora_pkg

if 'vibora.tests' not in sys.modules:
    vibora_root = pathlib.Path(__file__).resolve().parent.parent / 'vibora'
    spec = importlib.util.spec_from_file_location('vibora.tests', vibora_root / 'tests.py')
    tests_module = importlib.util.module_from_spec(spec)
    sys.modules['vibora.tests'] = tests_module
    spec.loader.exec_module(tests_module)
