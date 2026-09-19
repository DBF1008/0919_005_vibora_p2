import os
import tempfile
import time

from vibora.templates import TemplateEngine, Template
from vibora.templates.compilers.python import PythonTemplateCompiler
from vibora.templates.loader import TemplateLoader
from vibora.tests import TestSuite


class CountingCompiler(PythonTemplateCompiler):
    """Records every compilation so tests can assert incremental behavior."""

    def __init__(self):
        super().__init__()
        self.compiled_hashes = []

    def compile(self, template, verbose=False):
        self.compiled_hashes.append(template.hash)
        return super().compile(template, verbose=verbose)


class TemplateLoaderSuite(TestSuite):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.compiler = CountingCompiler()
        self.engine = TemplateEngine(compiler=self.compiler)
        self.loader = TemplateLoader(directories=[self.directory.name], engine=self.engine)

    def _write(self, filename, content):
        path = os.path.join(self.directory.name, filename)
        with open(path, 'w') as f:
            f.write(content)
        return path

    @staticmethod
    def _touch(path):
        future = time.time() + 5
        os.utime(path, (future, future))

    def test_initial_load_compiles_every_template(self):
        self._write('a.html', 'A')
        self._write('b.html', 'B')
        self.loader.load()
        self.engine.compile_templates()
        self.assertEqual(2, len(self.compiler.compiled_hashes))

    def test_single_change_recompiles_only_the_changed_template(self):
        path_a = self._write('a.html', 'A')
        self._write('b.html', 'B')
        self.loader.load()
        self.engine.compile_templates()
        self.compiler.compiled_hashes.clear()
        self._write('a.html', 'A version 2')
        self._touch(path_a)
        self.loader.check_for_modified_templates()
        self.assertEqual([Template('A version 2').hash], self.compiler.compiled_hashes)

    def test_new_file_is_detected_and_compiled_incrementally(self):
        self._write('a.html', 'A')
        self.loader.load()
        self.engine.compile_templates()
        self.compiler.compiled_hashes.clear()
        self._write('c.html', 'C')
        self.loader.check_for_modified_templates()
        self.assertEqual([Template('C').hash], self.compiler.compiled_hashes)

    def test_dependents_are_recompiled_when_a_parent_changes(self):
        parent_path = self._write('parent.html', 'parent v1')
        self._write('child.html', '{% include "parent.html" %}')
        self.loader.load()
        self.engine.compile_templates()
        self.compiler.compiled_hashes.clear()
        self._write('parent.html', 'parent v2')
        self._touch(parent_path)
        self.loader.check_for_modified_templates()
        # Both the changed template and its dependent must be recompiled.
        self.assertEqual(2, len(self.compiler.compiled_hashes))
        self.assertIn(Template('parent v2').hash, self.compiler.compiled_hashes)
        self.assertIn(Template('{% include "parent.html" %}').hash, self.compiler.compiled_hashes)

    def test_unchanged_templates_are_never_recompiled(self):
        self._write('a.html', 'A')
        self._write('b.html', 'B')
        self.loader.load()
        self.engine.compile_templates()
        self.compiler.compiled_hashes.clear()
        self.loader.check_for_modified_templates()
        self.assertEqual([], self.compiler.compiled_hashes)
