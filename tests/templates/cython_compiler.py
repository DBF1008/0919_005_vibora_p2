import json
import os
import tempfile

from vibora.templates.compilers.cython import CythonTemplateCompiler
from vibora.templates.exceptions import TemplateRenderError
from vibora.templates.nodes import Node, TextNode
from vibora.templates.template import ParsedTemplate
from vibora.templates.utils import CompilationResult, TemplateMeta
from vibora.tests import TestSuite


def build_meta(source_map=None, template_name=None):
    return TemplateMeta(
        entry_point='render', version='0.0.1', template_hash='hash',
        created_at='now', compiler='cython', architecture='arch',
        compilation_time=0, source_map=source_map, template_name=template_name
    )


class SourceMapInjectionSuite(TestSuite):

    def setUp(self):
        self.compiler = CythonTemplateCompiler()

    def test_comment_is_embedded_and_statement_is_mapped(self):
        self.compiler.add_comment('{{ user.name }}')
        self.compiler.add_statement('__content__.append(str(user.name))')
        lines = self.compiler.content.splitlines()
        self.assertEqual('# {{ user.name }}', lines[0].strip())
        self.assertEqual({2: (1, '{{ user.name }}')}, self.compiler.source_map)

    def test_template_line_is_tracked_across_text_nodes(self):
        self.compiler.add_text('line1\nline2\nline3\n')
        self.compiler.add_comment('{% if x %}')
        self.compiler.add_statement('if x:')
        mapped_line, raw = self.compiler.source_map[3]
        self.assertEqual(4, mapped_line)
        self.assertEqual('{% if x %}', raw)

    def test_source_map_offsets_helper_functions(self):
        self.compiler.functions.append('def helper():\n    pass')
        self.compiler.add_comment('{{ x }}')
        self.compiler.add_statement('pass')
        # The helper occupies 3 lines (2 lines + trailing blank line) in the
        # final .pyx file, so generated lines must be shifted accordingly.
        self.assertEqual({2 + 3: (1, '{{ x }}')}, self.compiler.get_source_map())

    def test_consume_maps_nodes_back_to_template_lines(self):
        root = Node()
        root.children.append(TextNode('hello\nworld\n'))
        root.children.append(Node(raw='{{ user }}'))
        template = ParsedTemplate(content='hello\nworld\n{{ user }}', ast=root, name='tpl.html')
        self.compiler.consume(template)
        self.assertIn('# {{ user }}', self.compiler.content)
        mapped = [value for value in self.compiler.source_map.values() if value[1] == '{{ user }}']
        self.assertEqual([(3, '{{ user }}')], mapped)

    def test_clean_resets_source_mapping_state(self):
        self.compiler.add_text('some\ntext\n')
        self.compiler.add_comment('{{ x }}')
        self.compiler.add_statement('pass')
        self.compiler.clean()
        self.assertEqual({}, self.compiler.source_map)
        self.assertEqual(1, self.compiler.template_line)
        self.assertIsNone(self.compiler.pending_comment)


class SourceMapMetaSuite(TestSuite):

    def test_meta_roundtrip_preserves_source_map_and_template_name(self):
        meta = build_meta(source_map={2: (7, '{{ x.boom() }}')}, template_name='templates/index.html')
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'meta.json')
            meta.store(path)
            loaded = TemplateMeta.load_from_path(path)
        # JSON turns int keys into strings and tuples into lists.
        self.assertEqual({'2': [7, '{{ x.boom() }}']}, loaded.source_map)
        self.assertEqual('templates/index.html', loaded.template_name)

    def test_old_meta_files_without_source_map_still_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'meta.json')
            with open(path, 'w') as f:
                json.dump({
                    'entry_point': 'render', 'version': '0.0.1', 'template_hash': 'h',
                    'created_at': 'now', 'compiler': 'cython', 'architecture': 'arch',
                    'compilation_time': 0, 'dependencies': []
                }, f)
            loaded = TemplateMeta.load_from_path(path)
        self.assertEqual({}, loaded.source_map)
        self.assertIsNone(loaded.template_name)


class SourceMapExceptionSuite(TestSuite):

    @staticmethod
    def raise_from_generated_code():
        # Simulates an exception raised inside a compiled template module:
        # the frame filename mimics a generated .pyx file, line 2 raises.
        code = compile('def render():\n    raise ValueError("boom")\nrender()\n', '/tmp/vt_abc.pyx', 'exec')
        try:
            exec(code, {})
        except ValueError as error:
            return error

    def test_exception_is_translated_to_template_position(self):
        meta = build_meta(source_map={2: (7, '{{ x.boom() }}')}, template_name='templates/index.html')
        result = CompilationResult(template=None, meta=meta, render_function=None, code=b'')
        error = self.raise_from_generated_code()
        with self.assertRaises(TemplateRenderError) as context:
            result.render_exception(error)
        exc = context.exception
        self.assertEqual('{{ x.boom() }}', exc.template_line)
        self.assertEqual(7, exc.template_line_number)
        self.assertEqual('templates/index.html', exc.template_file)
        self.assertIsInstance(exc.original_exception, ValueError)

    def test_exception_translation_works_with_json_loaded_meta(self):
        # Metas loaded from disk have string keys and list values.
        meta = build_meta(source_map={'2': [7, '{{ x.boom() }}']}, template_name='tpl.html')
        result = CompilationResult(template=None, meta=meta, render_function=None, code=b'')
        error = self.raise_from_generated_code()
        with self.assertRaises(TemplateRenderError) as context:
            result.render_exception(error)
        self.assertEqual('{{ x.boom() }}', context.exception.template_line)
        self.assertEqual(7, context.exception.template_line_number)

    def test_explicit_name_overrides_meta_template_name(self):
        meta = build_meta(source_map={2: (7, '{{ x }}')}, template_name='from_meta.html')
        result = CompilationResult(template=None, meta=meta, render_function=None, code=b'')
        error = self.raise_from_generated_code()
        with self.assertRaises(TemplateRenderError) as context:
            result.render_exception(error, name='explicit.html')
        self.assertEqual('explicit.html', context.exception.template_file)
