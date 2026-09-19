import asyncio

from vibora.templates import Template, TemplateEngine
from vibora.templates.exceptions import ConflictingNames, TemplateNotFound
from vibora.tests import TestSuite


class AddTemplateSuite(TestSuite):
    """
    Covers atomic template registration (single + batch).
    """

    def setUp(self):
        self.engine = TemplateEngine()

    def test_add_template_registers_under_all_names(self):
        template = Template('hello')
        parsed = self.engine.add_template(template, ['a.html', 'alias.html'])
        self.assertIs(self.engine.templates['a.html'], parsed)
        self.assertIs(self.engine.templates['alias.html'], parsed)

    def test_add_template_conflicting_name_raises(self):
        self.engine.add_template(Template('first'), ['a.html'])
        with self.assertRaises(ConflictingNames):
            self.engine.add_template(Template('second'), ['a.html'])

    def test_add_template_conflict_has_no_partial_success(self):
        """
        A conflict on any name must not register the names that were free.
        """
        self.engine.add_template(Template('first'), ['a.html'])
        before = dict(self.engine.templates)
        with self.assertRaises(ConflictingNames):
            self.engine.add_template(Template('second'), ['b.html', 'a.html'])
        self.assertEqual(self.engine.templates, before)
        self.assertNotIn('b.html', self.engine.templates)

    def test_add_templates_batch_success_registers_everything(self):
        parsed = self.engine.add_templates([
            (Template('one'), ['a.html']),
            (Template('two'), ['b.html', 'b-alias.html'])
        ])
        self.assertEqual(len(parsed), 2)
        self.assertIn('a.html', self.engine.templates)
        self.assertIn('b.html', self.engine.templates)
        self.assertIn('b-alias.html', self.engine.templates)

    def test_add_templates_batch_conflict_rolls_all_back(self):
        self.engine.add_template(Template('existing'), ['a.html'])
        before = dict(self.engine.templates)
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates([
                (Template('one'), ['new-one.html']),
                (Template('two'), ['new-two.html']),
                (Template('three'), ['a.html'])
            ])
        self.assertEqual(self.engine.templates, before)
        self.assertNotIn('new-one.html', self.engine.templates)
        self.assertNotIn('new-two.html', self.engine.templates)

    def test_add_templates_invalid_syntax_rolls_all_back(self):
        with self.assertRaises(Exception):
            self.engine.add_templates([
                (Template('ok'), ['a.html']),
                (Template('{% invalid_tag %}'), ['b.html'])
            ])
        self.assertEqual(self.engine.templates, {})


class TransactionSuite(TestSuite):

    def setUp(self):
        self.engine = TemplateEngine()
        self.engine.add_template(Template('content'), ['a.html'])

    def test_transaction_rolls_back_templates_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.engine.transaction():
                self.engine.add_template(Template('x'), ['b.html'])
                self.assertIn('b.html', self.engine.templates)
                raise RuntimeError('boom')
        self.assertNotIn('b.html', self.engine.templates)
        self.assertIn('a.html', self.engine.templates)

    def test_transaction_commits_when_no_error(self):
        with self.engine.transaction():
            self.engine.add_template(Template('x'), ['b.html'])
        self.assertIn('b.html', self.engine.templates)

    def test_transaction_restores_compiled_templates_and_cache(self):
        self.engine.compile_templates()
        template_hash = self.engine.templates['a.html'].hash
        self.assertIn(template_hash, self.engine.compiled_templates)
        with self.assertRaises(RuntimeError):
            with self.engine.transaction():
                self.engine.compiled_templates.clear()
                self.engine.cache.loaded_templates.clear()
                self.engine.cache.loaded_metas.clear()
                raise RuntimeError('boom')
        self.assertIn(template_hash, self.engine.compiled_templates)
        self.assertIn(template_hash, self.engine.cache.loaded_templates)


class CompileTemplateSuite(TestSuite):

    def setUp(self):
        self.engine = TemplateEngine()

    async def test_compile_single_template_renders(self):
        self.engine.add_template(Template('value={{ who }}'), ['a.html'])
        template = self.engine.templates['a.html']
        self.engine.compile_template(template)
        result = await self.engine.render('a.html', who='vibora')
        self.assertEqual(result, 'value=vibora')

    async def test_compile_template_skips_when_already_compiled(self):
        self.engine.add_template(Template('x'), ['a.html'])
        template = self.engine.templates['a.html']

        class CountingCompiler:
            NAME = 'counting'
            VERSION = '0.0.1'

            def __init__(self):
                self.calls = 0

            def compile(self, compiled_template, verbose=False):
                self.calls += 1
                return self.calls

        compiler = CountingCompiler()
        self.engine.compiler = compiler
        self.engine.compiled_templates[template.hash] = object()
        self.engine.cache.loaded_templates[template.hash] = object()
        self.engine.compile_template(template)
        self.assertEqual(compiler.calls, 0)

    async def test_incremental_compile_does_not_invalidate_untouched_templates(self):
        self.engine.add_template(Template('A'), ['a.html'])
        self.engine.add_template(Template('B'), ['b.html'])
        self.engine.compile_templates()
        a_hash = self.engine.templates['a.html'].hash
        b_hash = self.engine.templates['b.html'].hash

        self.engine.compile_template(self.engine.templates['b.html'])
        self.assertIn(a_hash, self.engine.compiled_templates)
        self.assertIn(b_hash, self.engine.compiled_templates)
        self.assertEqual(await self.engine.render('a.html'), 'A')
        self.assertEqual(await self.engine.render('b.html'), 'B')


class DependentsSuite(TestSuite):

    def setUp(self):
        self.engine = TemplateEngine()

    def test_get_dependents_transitive(self):
        self.engine.add_template(
            Template('P{% block c %}default{% endblock %}'), ['base.html']
        )
        self.engine.add_template(
            Template('{% extends "base.html" %}{% block c %}mid{% endblock %}'), ['mid.html']
        )
        self.engine.add_template(
            Template('{% extends "mid.html" %}{% block c %}leaf{% endblock %}'), ['leaf.html']
        )
        self.engine.compile_templates()
        base_hash = self.engine.templates['base.html'].hash
        dependents = self.engine.get_dependents(base_hash)
        self.assertEqual(dependents, {
            self.engine.templates['mid.html'].hash,
            self.engine.templates['leaf.html'].hash
        })

    def test_get_dependents_include_chain(self):
        self.engine.add_template(Template('A{% include "b.html" %}'), ['a.html'])
        self.engine.add_template(Template('B{% include "c.html" %}'), ['b.html'])
        self.engine.add_template(Template('C'), ['c.html'])
        self.engine.compile_templates()
        c_hash = self.engine.templates['c.html'].hash
        dependents = self.engine.get_dependents(c_hash)
        self.assertEqual(dependents, {
            self.engine.templates['a.html'].hash,
            self.engine.templates['b.html'].hash
        })

    def test_get_dependents_empty_for_isolated_template(self):
        self.engine.add_template(Template('A'), ['a.html'])
        self.engine.compile_templates()
        self.assertEqual(self.engine.get_dependents(self.engine.templates['a.html'].hash), set())


class RenderSuite(TestSuite):

    def setUp(self):
        self.engine = TemplateEngine()

    async def test_render_missing_template_raises_template_not_found(self):
        with self.assertRaises(TemplateNotFound):
            await self.engine.render('missing.html')

    async def test_render_without_compilation_raises(self):
        self.engine.add_template(Template('x'), ['a.html'])
        with self.assertRaises(Exception):
            await self.engine.render('a.html')


class RemoveTemplateSuite(TestSuite):

    def setUp(self):
        self.engine = TemplateEngine()

    async def test_remove_template_clears_every_alias(self):
        self.engine.add_template(Template('x'), ['a.html', 'alias.html'])
        self.engine.compile_templates()
        template = self.engine.templates['a.html']
        self.engine.remove_template(template)
        self.assertNotIn('a.html', self.engine.templates)
        self.assertNotIn('alias.html', self.engine.templates)
        self.assertNotIn(template.hash, self.engine.compiled_templates)
