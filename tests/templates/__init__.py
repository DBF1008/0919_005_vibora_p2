import os
import sys
import types


def _ensure_vibora_importable():
    """The full vibora package requires compiled C extensions (build.py).
    The template engine is pure Python, so when the extensions are missing
    we register a lightweight package stub pointing at the real source tree,
    allowing `vibora.templates` and `vibora.tests` to be imported directly.
    """
    try:
        import vibora.templates  # noqa
        return
    except ImportError:
        sys.modules.pop('vibora', None)
        sys.modules.pop('vibora.templates', None)
        # The partially executed vibora/__init__.py may have installed a
        # custom event loop policy (uvloop) which does not auto-create
        # event loops and would break the async test runner.
        import asyncio
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    stub = types.ModuleType('vibora')
    stub.__path__ = [os.path.join(repo_root, 'vibora')]
    sys.modules['vibora'] = stub


_ensure_vibora_importable()
