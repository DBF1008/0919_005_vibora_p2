import os

from vibora.templates import Template, TemplateParser
from vibora.templates.compilers.cython import (
    CythonTemplateCompiler, SourceMap, annotate_template_lines
)
from vibora.templates.exceptions import TemplateRenderError
from vibora.tests import TestSuite


def parse(content, origin='<string>'):
    return TemplateParser().parse(Template(content, origin=origin))


class AnnotateTemplateLinesSuite(TestSuite):

    def test_nodes_annotated_with_template_line_numbers(self):
        template = parse("header\n{% for x in items %}\n{{ x }}\n{% endfor %}\n",
                         origin='views/a.html')
        annotate_template_lines(template)
        raw_lines = {}

        def collect(node):
            if getattr(node, 'raw', ''):
                raw_lines[node.raw.strip()] = (
                    getattr(node, 'source_line', None),
                    getattr(node, 'source_origin', None)
                )
            for child in node.children:
                collect(child)

        collect(template.ast)
        self.assertEqual(raw_lines['{% for x in items %}'], (2, 'views/a.html'))
        self.assertEqual(raw_lines['{{ x }}'], (3, 'views/a.html'))

    def test_multiple_nodes_on_different_lines(self):
        template = parse("{% if a %}\nx\n{% endif %}\n{% if b %}\ny\n{% endif %}")
        annotate_template_lines(template)
        if_nodes = [child for child in template.ast.children
                    if child.__class__.__name__ == 'IfNode']
        self.assertEqual([node.source_line for node in if_nodes], [1, 4])


class SourceMapSuite(TestSuite):

    def test_lookup_returns_last_mapping_before_line(self):
        source_map = SourceMap('views/a.html')
        source_map.add(10, 2)
        source_map.add(20, 5)
        self.assertEqual(source_map.lookup(1), ('views/a.html', 1))
        self.assertEqual(source_map.lookup(10), ('views/a.html', 2))
        self.assertEqual(source_map.lookup(19), ('views/a.html', 2))
        self.assertEqual(source_map.lookup(20), ('views/a.html', 5))
        self.assertEqual(source_map.lookup(999), ('views/a.html', 5))

    def test_shift_offsets_generated_lines(self):
        source_map = SourceMap('a.html')
        source_map.add(1, 1)
        source_map.add(5, 3)
        source_map.shift(100)
        self.assertEqual(source_map.lookup(101), ('a.html', 1))
        self.assertEqual(source_map.lookup(105), ('a.html', 3))
        self.assertEqual(source_map.lookup(102), ('a.html', 1))

    def test_empty_map_defaults_to_first_line(self):
        source_map = SourceMap('a.html')
        self.assertEqual(source_map.lookup(42), ('a.html', 1))


class GeneratedSourceMarkersSuite(TestSuite):

    def test_for_loop_emits_line_directive_and_runtime_position(self):
        template = parse("header\n{% for x in items %}\nbody\n{% endfor %}\n",
                         origin='views/a.html')
        compiler = CythonTemplateCompiler()
        compiler.consume(template)
        self.assertIn('#line 2 "views/a.html"', compiler.content)
        self.assertIn('__template_line__ = 2', compiler.content)
        self.assertIn("__template_origin__ = 'views/a.html'", compiler.content)

    def test_if_node_emits_line_directive(self):
        template = parse("a\n{% if flag %}\nyes\n{% endif %}", origin='b.html')
        compiler = CythonTemplateCompiler()
        compiler.consume(template)
        self.assertIn('#line 2 "b.html"', compiler.content)

    def test_preamble_exposes_position_getters(self):
        template = parse("{% for x in items %}\n{{ x }}\n{% endfor %}",
                         origin='a.html')
        compiler = CythonTemplateCompiler()
        compiler.consume(template)
        self.assertIn('__template_line__ = 1', compiler.preamble)
        self.assertIn('def __vibora_template_line__():', compiler.preamble)
        self.assertIn('def __vibora_template_origin__():', compiler.preamble)
        self.assertIn("__template_origin__ = 'a.html'", compiler.preamble)

    def test_source_map_matches_generated_lines(self):
        template = parse("a\n{% for x in items %}\nb\n{% endfor %}", origin='a.html')
        compiler = CythonTemplateCompiler()
        compiler.consume(template)
        generated_lines = compiler.content.splitlines()
        # Each mapping points at the line-assignment line inside the
        # three-line marker block (#line directive, line, origin).
        for generated_line, template_line in compiler.source_map.entries:
            self.assertEqual(generated_lines[generated_line - 1].strip(),
                             f'#line {template_line} "a.html"')
            self.assertEqual(generated_lines[generated_line].strip(),
                             f'__template_line__ = {template_line}')
            self.assertIn('__template_origin__', generated_lines[generated_line + 1])

    def test_macros_do_not_receive_template_markers(self):
        template = parse(
            '{% macro greet(name) %}hi {{ name }}{% endmacro %}', origin='a.html'
        )
        compiler = CythonTemplateCompiler()
        compiler.consume(template)
        # Macro sub-trees compile through a separate compiler instance with no
        # source map; emitting markers there would reference undefined globals.
        self.assertNotIn('__template_line__', '\n'.join(compiler.functions))

    def test_clean_resets_source_map_state(self):
        template = parse("{% for x in items %}x{% endfor %}", origin='a.html')
        compiler = CythonTemplateCompiler()
        compiler.consume(template)
        self.assertIsNotNone(compiler.source_map)
        compiler.clean()
        self.assertIsNone(compiler.source_map)
        self.assertEqual(compiler.content, '')
        self.assertEqual(compiler.preamble, '')


class FakeCompiledModule:
    """
    Mimics the globals injected into a compiled Cython module.
    """

    def __init__(self, line, origin):
        self.line = line
        self.origin = origin

    def __vibora_template_line__(self):
        return self.line

    def __vibora_template_origin__(self):
        return self.origin


class ExceptionTranslationSuite(TestSuite):

    def setUp(self):
        self.template = parse(
            "header\n{% for x in items %}\n{{ x.missing }}\n{% endfor %}\n",
            origin='views/a.html'
        )

    def test_wrapped_render_passes_through_result(self):
        module = FakeCompiledModule(1, 'views/a.html')

        def render(context):
            return 'rendered:' + str(context['value'])

        wrapped = CythonTemplateCompiler.wrap_render_function(
            render, module, SourceMap('views/a.html'), self.template
        )
        self.assertEqual(wrapped({'value': 'ok'}), 'rendered:ok')

    def test_wrapped_render_translates_exception_to_template_position(self):
        module = FakeCompiledModule(3, 'views/a.html')

        def render(context):
            raise AttributeError("'int' object has no attribute 'missing'")

        wrapped = CythonTemplateCompiler.wrap_render_function(
            render, module, SourceMap('views/a.html'), self.template
        )
        with self.assertRaises(TemplateRenderError) as ctx:
            wrapped({})
        error = ctx.exception
        self.assertEqual(error.template_name, 'views/a.html')
        self.assertIn('{{ x.missing }}', error.template_line)
        self.assertIsInstance(error.original_exception, AttributeError)

    def test_translate_exception_falls_back_to_source_map(self):
        class ModuleWithoutGlobals:
            pass

        def failing_render(context):
            raise ValueError('boom')

        source_map = SourceMap('views/a.html')
        source_map.add(1, 1)
        source_map.add(8, 3)
        wrapped = CythonTemplateCompiler.wrap_render_function(
            failing_render, ModuleWithoutGlobals(), source_map, self.template
        )
        with self.assertRaises(TemplateRenderError) as ctx:
            wrapped({})
        self.assertEqual(ctx.exception.template_name, 'views/a.html')
        self.assertIn('{{ x.missing }}', ctx.exception.template_line)
        self.assertIsInstance(ctx.exception.original_exception, ValueError)

    def test_translate_exception_does_not_double_wrap(self):
        module = FakeCompiledModule(3, 'views/a.html')
        original = TemplateRenderError(
            template=self.template, template_line='x', exception=RuntimeError('x'),
            template_name='views/a.html'
        )

        def render(context):
            raise original

        wrapped = CythonTemplateCompiler.wrap_render_function(
            render, module, SourceMap('views/a.html'), self.template
        )
        try:
            wrapped({})
            self.fail('expected exception')
        except TemplateRenderError as error:
            self.assertIs(error, original)

    def test_template_render_error_carries_template_name_attribute(self):
        error = TemplateRenderError(
            template=self.template, template_line='line', exception=RuntimeError('x'),
            template_name='views/a.html'
        )
        self.assertEqual(error.template_name, 'views/a.html')
        self.assertEqual(error.template_line, 'line')
        self.assertIsInstance(error.original_exception, RuntimeError)
