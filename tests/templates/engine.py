from vibora.templates import TemplateEngine, Template
from vibora.templates.exceptions import ConflictingNames
from vibora.tests import TestSuite


class AddTemplateTransactionSuite(TestSuite):

    def setUp(self):
        self.engine = TemplateEngine()

    def test_add_template_success_registers_all_names(self):
        parsed = self.engine.add_template(Template('content'), ['a', 'b'])
        self.assertIs(self.engine.templates['a'], parsed)
        self.assertIs(self.engine.templates['b'], parsed)

    def test_conflicting_name_registers_nothing(self):
        self.engine.add_template(Template('original'), ['existing'])
        snapshot = dict(self.engine.templates)
        with self.assertRaises(ConflictingNames):
            # 'brand_new' is free but 'existing' is taken: nothing may be registered.
            self.engine.add_template(Template('intruder'), ['brand_new', 'existing'])
        self.assertEqual(snapshot, self.engine.templates)
        self.assertNotIn('brand_new', self.engine.templates)
        self.assertEqual('original', self.engine.templates['existing'].content)

    def test_template_name_defaults_to_first_registered_name(self):
        parsed = self.engine.add_template(Template('content'), ['pages/index.html'])
        self.assertEqual('pages/index.html', parsed.name)


class BatchLoadingTransactionSuite(TestSuite):

    def setUp(self):
        self.engine = TemplateEngine()

    def test_batch_success_registers_everything(self):
        parsed = self.engine.add_templates([
            (Template('one'), ['one.html']),
            (Template('two'), ['two.html']),
        ])
        self.assertEqual(2, len(parsed))
        self.assertIn('one.html', self.engine.templates)
        self.assertIn('two.html', self.engine.templates)

    def test_batch_rolls_back_when_a_template_conflicts(self):
        self.engine.add_template(Template('original'), ['taken.html'])
        snapshot = dict(self.engine.templates)
        batch = [
            (Template('1'), ['one.html']),
            (Template('2'), ['two.html']),
            (Template('3'), ['taken.html']),
        ]
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates(batch)
        # The first two templates of the batch must have been rolled back.
        self.assertEqual(snapshot, self.engine.templates)
        self.assertNotIn('one.html', self.engine.templates)
        self.assertNotIn('two.html', self.engine.templates)

    def test_batch_rolls_back_on_internal_name_conflict(self):
        batch = [
            (Template('a'), ['duplicated.html']),
            (Template('b'), ['duplicated.html']),
        ]
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates(batch)
        self.assertEqual({}, self.engine.templates)

    def test_large_batch_rolls_back_cleanly(self):
        # Simulates loading 50 templates where the 49th one conflicts:
        # none of the previous 48 registrations may survive.
        batch = [(Template(f'content {i}'), [f'template_{i}.html']) for i in range(50)]
        batch[48] = (Template('conflicting'), ['template_0.html'])
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates(batch)
        self.assertEqual({}, self.engine.templates)

    def test_engine_remains_usable_after_rollback(self):
        batch = [(Template('x'), ['x.html']), (Template('y'), ['x.html'])]
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates(batch)
        parsed = self.engine.add_template(Template('healthy'), ['healthy.html'])
        self.assertIn('healthy.html', self.engine.templates)
        self.assertEqual('healthy', parsed.content)

    async def test_rolled_back_templates_are_not_renderable(self):
        batch = [(Template('x'), ['x.html']), (Template('y'), ['x.html'])]
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates(batch)
        self.engine.compile_templates()
        with self.assertRaises(Exception):
            await self.engine.render('x.html')
