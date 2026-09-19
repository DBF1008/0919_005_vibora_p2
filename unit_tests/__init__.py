"""
Unit tests for the template engine refactoring.

These tests live in their own package because the top-level ``vibora`` package
cannot be imported in environments without its compiled Cython extensions
(server/parser modules). ``_bootstrap`` registers a lightweight ``vibora``
namespace package and the ``collections.Callable`` compatibility shim so the
pure-Python template sub-system can be imported and exercised in isolation.
"""
from . import _bootstrap  # noqa: F401
