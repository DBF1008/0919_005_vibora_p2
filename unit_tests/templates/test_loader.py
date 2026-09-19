import asyncio
import os
import shutil
import tempfile
import time

from vibora.templates import TemplateEngine
from vibora.templates.compilers.python import PythonTemplateCompiler
from vibora.templates.loader import TemplateLoader
from vibora.tests import TestSuite


class CountingCompiler(PythonTemplateCompiler):
    """
    Python compiler recording every file (by origin path) it compiles.
    """

    def __init__(self):
        super().__init__()
        self.compiled_origins = []

    def compile(self, template, verbose=False):
        self.compiled_origins.append(template.origin)
        return super().compile(template, verbose)


class LoaderSuite(TestSuite):

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.compiler = CountingCompiler()
        self.engine = TemplateEngine(compiler=self.compiler)
        self.loader = TemplateLoader([self.directory], self.engine)

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def write(self, name, content, future=0):
        path = os.path.join(self.directory, name)
        with open(path, 'w') as f:
            f.write(content)
        if future:
            mtime = time.time() + future
            os.utime(path, (mtime, mtime))
        return path

    def render(self, name):
        return asyncio.get_event_loop().run_until_complete(self.engine.render(name))

    def compiled_basenames(self):
        return sorted(os.path.basename(path) for path in self.compiler.compiled_origins)

    def initial_project(self):
        self.write('base.html', 'BASE{% block c %}d{% endblock %}')
        self.write('child.html', '{% extends "base.html" %}{% block c %}C{% endblock %}')
        self.write('solo.html', 'SOLO')
        self.loader.load()
        self.engine.compile_templates()
        self.compiler.compiled_origins.clear()

    def test_initial_load_registers_and_compiles_all(self):
        self.write('a.html', 'A')
        self.write('b.html', 'B')
        self.loader.load()
        self.engine.compile_templates()
        self.assertEqual(self.render('a.html'), 'A')
        self.assertEqual(self.render('b.html'), 'B')
        self.assertEqual(self.compiled_basenames(), ['a.html', 'b.html'])

    def test_changing_parent_recompiles_only_affected_closure(self):
        self.initial_project()
        self.write('base.html', 'BASE2{% block c %}d{% endblock %}', future=10)
        self.loader.check_for_modified_templates()
        self.assertEqual(self.compiled_basenames(), ['base.html', 'child.html'])
        self.assertEqual(self.render('child.html'), 'BASE2C')
        self.assertEqual(self.render('solo.html'), 'SOLO')

    def test_changing_child_recompiles_only_child(self):
        self.initial_project()
        self.write('child.html', '{% extends "base.html" %}{% block c %}CCC{% endblock %}',
                   future=10)
        self.loader.check_for_modified_templates()
        self.assertEqual(self.compiled_basenames(), ['child.html'])
        self.assertEqual(self.render('child.html'), 'BASECCC')

    def test_changing_isolated_template_recompiles_only_it(self):
        self.initial_project()
        self.write('solo.html', 'SOLO2', future=10)
        self.loader.check_for_modified_templates()
        self.assertEqual(self.compiled_basenames(), ['solo.html'])
        self.assertEqual(self.render('solo.html'), 'SOLO2')

    def test_mtime_touch_without_content_change_does_not_recompile(self):
        self.initial_project()
        path = os.path.join(self.directory, 'solo.html')
        mtime = time.time() + 50
        os.utime(path, (mtime, mtime))
        self.loader.check_for_modified_templates()
        self.assertEqual(self.compiled_basenames(), [])
        self.assertEqual(self.render('solo.html'), 'SOLO')

    def test_include_chain_change_recompiles_all_dependents(self):
        self.write('a.html', 'A{% include "b.html" %}')
        self.write('b.html', 'B{% include "c.html" %}')
        self.write('c.html', 'C')
        self.loader.load()
        self.engine.compile_templates()
        self.compiler.compiled_origins.clear()
        self.write('c.html', 'C2', future=10)
        self.loader.check_for_modified_templates()
        self.assertEqual(self.compiled_basenames(), ['a.html', 'b.html', 'c.html'])
        self.assertEqual(self.render('a.html'), 'ABC2')

    def test_broken_edit_rolls_back_and_keeps_old_version(self):
        self.initial_project()
        self.write('solo.html', '{% this_is_not_a_valid_tag %}', future=10)
        with self.assertRaises(Exception):
            self.loader.check_for_modified_templates()
        # No compilation happened with the broken content.
        self.assertEqual(self.compiled_basenames(), [])
        # The old, working version is still served.
        self.assertEqual(self.render('solo.html'), 'SOLO')
        # The mtime wasn't promoted: the broken file is retried next poll.
        path = os.path.join(self.directory, 'solo.html')
        self.assertIn(path, self.loader.cache)

    def test_fixing_broken_template_after_rollback_loads_new_version(self):
        self.initial_project()
        self.write('solo.html', '{% broken %}', future=10)
        with self.assertRaises(Exception):
            self.loader.check_for_modified_templates()
        self.write('solo.html', 'SOLO3', future=20)
        self.loader.check_for_modified_templates()
        self.assertEqual(self.render('solo.html'), 'SOLO3')

    def test_new_file_is_detected_and_compiled(self):
        self.initial_project()
        self.write('new.html', 'NEW!', future=10)
        self.loader.check_for_modified_templates()
        self.assertIn('new.html', self.compiled_basenames())
        self.assertEqual(self.render('new.html'), 'NEW!')

    def test_origin_attribute_is_set_to_file_path(self):
        path = self.write('a.html', 'A')
        self.loader.load()
        template = self.loader.path_index[path]
        self.assertEqual(template.origin, path)

    def test_conflicting_reload_is_transactional(self):
        self.write('a.html', 'A')
        self.loader.load()
        self.engine.compile_templates()
        self.compiler.compiled_origins.clear()

        # Simulate a name conflict during reload by registering the target
        # name against a different template object beforehand.
        self.engine.add_template(
            __import__('vibora.templates.template', fromlist=['Template']).Template('X'),
            ['b.html']
        )
        self.write('a.html', 'A2', future=10)
        self.write('b.html', 'B', future=10)
        with self.assertRaises(Exception):
            self.loader.check_for_modified_templates()
        # 'a.html' must still render its previous content after rollback.
        self.assertEqual(self.render('a.html'), 'A')
