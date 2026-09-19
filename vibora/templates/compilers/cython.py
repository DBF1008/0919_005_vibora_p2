import hashlib
import importlib.util
import os
import tempfile
import time
import datetime
import sys
from ..compilers.base import TemplateCompiler
from ..utils import (
    find_template_binary, CompilerFlavor, TemplateMeta, get_architecture_signature, CompilationResult
)
from ..exceptions import TemplateRenderError


class SourceMap:
    """
    Maps generated Cython source line numbers back to the original template
    location (file name + line number).
    """

    def __init__(self, origin: str):
        self.origin = origin
        # List of (generated_line, template_line) ordered by generated line.
        self.entries = []

    def add(self, generated_line: int, template_line: int):
        self.entries.append((generated_line, template_line))

    def shift(self, offset: int):
        """
        Shifts all generated line numbers. Used when the generated source is
        prepended with a preamble after the mapping was recorded.

        :param offset:
        :return:
        """
        self.entries = [(line + offset, template_line) for line, template_line in self.entries]

    def lookup(self, generated_line: int):
        """
        Returns (origin, template_line) active at a given generated line.

        :param generated_line:
        :return:
        """
        template_line = 1
        for mapped_line, original_line in self.entries:
            if mapped_line > generated_line:
                break
            template_line = original_line
        return self.origin, template_line


def annotate_template_lines(template):
    """
    Annotates every AST node with the line it occupies in the original
    template source. Nodes carry their raw tag/expression text, so the source
    is scanned in document order (parent then children), matching the order
    in which the parser appended the nodes.

    :param template:
    :return:
    """
    source_lines = template.content.splitlines()
    origin = getattr(template, 'origin', '<string>')

    pending = [template.ast]
    cursor = 0
    while pending:
        node = pending.pop(0)
        raw = getattr(node, 'raw', '')
        if raw:
            needle = raw.strip()
            for line_number in range(cursor, len(source_lines)):
                if needle and needle in source_lines[line_number]:
                    node.source_line = line_number + 1
                    node.source_origin = origin
                    cursor = line_number + 1
                    break
        children = list(getattr(node, 'children', ()))
        for child in reversed(children):
            pending.insert(0, child)


# TODO: Remove 'render' hardcoded name.


class CythonTemplateCompiler(TemplateCompiler):

    NAME = 'cython'
    VERSION = '0.0.1'
    EXTENSION_NAME = 'compiled_templates'

    def __init__(self, flavor=CompilerFlavor.TEMPLATE, temporary_dir: str=None):
        super().__init__()
        self.content = ''
        # Preamble emitted before the generated render function. Holds the
        # globals used to expose the current template position at runtime.
        self.preamble = ''
        self.current_scope = list()
        self.accumulated_text = ''
        self.content_var = '__content__'
        self.context_var = '__context__'
        self.functions = []
        self.flavor = flavor
        self.temporary_dir = temporary_dir or tempfile.gettempdir()
        # Source map + bookkeeping of the template being currently compiled.
        self.source_map = None
        self.current_origin = '<string>'
        self.last_emitted_line = None

    def clean(self):
        self._indentation = 0
        self.content = ''
        self.preamble = ''
        self.current_scope = list()
        self.accumulated_text = ''
        self.functions = []
        self.flavor = CompilerFlavor.TEMPLATE
        self.source_map = None
        self.current_origin = '<string>'
        self.last_emitted_line = None

    def add_source_position(self, template_line: int, origin: str=None):
        """
        Injects source mapping information right before the next generated
        statement:

        * A Cython ``#line`` directive so native tracebacks, the C debugger and
          coverage tooling report the original template file/line.
        * Runtime assignments exposing the current position, used to translate
          exceptions even when the directive cannot be honored.

        :param template_line:
        :param origin:
        :return:
        """
        origin = origin or self.current_origin
        if template_line == self.last_emitted_line and origin == self.current_origin:
            return
        self.last_emitted_line = template_line
        self.current_origin = origin
        indentation = ' ' * self._indentation
        marker = f'{indentation}#line {template_line} "{origin}"\n'
        marker += f'{indentation}__template_line__ = {template_line}\n'
        marker += f'{indentation}__template_origin__ = {origin!r}\n'
        if self.source_map is not None:
            self.content += marker
            # The line assignment is the middle line of the three-line
            # marker block.
            generated_line = len(self.content.splitlines()) - 2
            self.source_map.add(generated_line, template_line)
        else:
            self.content += marker

    def add_comment(self, content: str):
        """
        Called by :class:`Node.compile` for every node carrying a raw tag.
        We use it to emit the source position of that node.

        :param content:
        :return:
        """
        # Macros are compiled through a separate compiler instance which has
        # no template context attached; ignore those.
        if self.flavor != CompilerFlavor.TEMPLATE or self.source_map is None:
            return
        line_number = getattr(self, '_current_node_line', None)
        if line_number is not None:
            self.add_source_position(line_number, self.current_origin)
        self._current_node_line = None

    def add_text(self, content: str):
        content = content.replace("\n", "\\n")
        content = content.replace(r'"', r'\"')
        self.accumulated_text += content

    def flush_text(self):
        text = self.accumulated_text
        self.accumulated_text = ''
        stm = f'{self.content_var}.append("{text}")'
        self.add_statement(stm)

    def add_eval(self, statement: str):
        """
        Emits an evaluated expression into the rendered content.

        :param statement:
        :return:
        """
        self.add_statement(f'{self.content_var}.append(str({statement}))')

    def add_statement(self, content: str):
        if self.accumulated_text:
            self.flush_text()
        new_content = (' ' * self._indentation) + content.strip() + '\n'
        self.content += new_content

    def _bind_source_positions(self, node):
        """
        Wraps every node (except macro sub-trees, which are compiled by their
        own compiler instance) so its original template line is exposed right
        before its generated code is emitted.

        :param node:
        :return:
        """
        from ..nodes import MacroNode

        for child in node.children:
            if isinstance(child, MacroNode):
                continue
            source_line = getattr(child, 'source_line', None)
            if source_line is not None:
                original_compile = child.compile

                def compile_with_position(compiler, recursive=True, line=source_line,
                                          bound_compile=original_compile):
                    compiler._current_node_line = line
                    return bound_compile(compiler, recursive)

                child.compile = compile_with_position
            self._bind_source_positions(child)

    def consume(self, template):
        annotate_template_lines(template)
        self.current_origin = getattr(template, 'origin', '<string>')
        self.source_map = SourceMap(self.current_origin)
        self.last_emitted_line = None

        # Globals exposed by the compiled module so runtime error handlers can
        # discover the template position that was executing.
        self.preamble = '__template_line__ = 1\n'
        self.preamble += f'__template_origin__ = {self.current_origin!r}\n'
        self.preamble += 'def __vibora_template_line__():\n'
        self.preamble += '    return __template_line__\n'
        self.preamble += 'def __vibora_template_origin__():\n'
        self.preamble += '    return __template_origin__\n\n'

        self.add_statement(f'cpdef str render(dict {self.context_var}):')
        self._indentation += 4
        self.add_statement(f"cdef list {self.content_var} = []")
        self._bind_source_positions(template.ast)
        template.ast.compile(self)
        self.add_statement(f'return "".join({self.content_var})')
        self._indentation -= 4

    def create_new_macro(self, definition: str):
        new_compiler = self.__class__(flavor=CompilerFlavor.MACRO)
        new_compiler.add_statement('def ' + definition + ':')
        new_compiler.indent()
        new_compiler.add_statement(f"{self.content_var} = []")
        return new_compiler

    @classmethod
    def load_compiled_template(cls, meta: TemplateMeta, content: bytes):
        f = tempfile.NamedTemporaryFile(mode='wb', suffix='.so')
        f.file.write(content)
        f.file.flush()
        spec = importlib.util.spec_from_file_location(cls.EXTENSION_NAME, f.name)
        compiled_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiled_module)
        return getattr(compiled_module, meta.entry_point)

    def compile(self, template, verbose: bool=False) -> CompilationResult:
        """

        :param verbose:
        :param template:
        :return:
        """
        # Tracking compile times
        started_at = time.time()

        # setuptools is only required when actually invoking the C build.
        # Importing it lazily keeps source-map tooling usable in environments
        # without a build toolchain installed.
        from setuptools import Extension, setup

        # Temporary directory for this compilation.
        working_dir = tempfile.TemporaryDirectory(dir=self.temporary_dir)

        # Generating .pyx files.
        self.consume(template)
        template_hash = hashlib.md5(self.content.encode()).hexdigest()
        temp_path = os.path.join(working_dir.name, 'vt_' + template_hash + '.pyx')
        with open(temp_path, 'w') as f:
            f.write(self.preamble)
            for helper_function in self.functions:
                f.write(helper_function + '\n\n')
            f.write(self.content)

        # The preamble shifts every generated line recorded in the map.
        preamble_line_count = len(
            [line for line in (self.preamble + ''.join(f + '\n\n' for f in self.functions)).splitlines()]
        )
        source_map = self.source_map
        if source_map is not None:
            source_map.shift(preamble_line_count)

        # Building optimized binaries.
        ext = Extension(self.EXTENSION_NAME, [temp_path], extra_compile_args=['-O3'], include_dirs=['.'])
        build_path = os.path.join(working_dir.name, template_hash)
        trash_dir = os.path.join(working_dir.name, 'trash')
        args = ['build_ext', '-b', build_path, '-t', trash_dir]
        if not self.verbose:
            args = ['-q'] + args
        setup(ext_modules=[ext], script_args=args)

        # Loading modules.
        compiled_path = find_template_binary(build_path)
        spec = importlib.util.spec_from_file_location(self.EXTENSION_NAME, compiled_path)
        compiled_template = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiled_template)
        wrapped_render = self.wrap_render_function(
            compiled_template.render, compiled_module=compiled_template,
            source_map=source_map, template=template
        )

        # Generating meta data about this compilation so we can correctly
        # cache and load these templates later.
        meta = TemplateMeta(
            entry_point='render',
            version=self.VERSION,
            compiler=self.NAME,
            template_hash=template.hash,
            created_at=datetime.datetime.now().isoformat(),
            architecture=get_architecture_signature(),
            compilation_time=round(time.time() - started_at, 2),
            dependencies=template.dependencies
        )

        # Compilation result contains the meta data and the render function loaded at runtime.
        compilation = CompilationResult(
            template=template,
            meta=meta, render_function=wrapped_render, code=open(compiled_path, 'rb').read()
        )
        compilation.source_map = source_map

        # Clearing state
        self.clean()

        # Binding the render function
        return compilation

    @classmethod
    def generate_template_name(cls, hash_: str):
        return hash_ + '.so'

    @staticmethod
    def translate_exception(error, compiled_module, source_map, template):
        """
        Translates an exception raised inside the compiled module into a
        :class:`TemplateRenderError` pointing at the original template file
        and line.

        The current position is first read from the runtime globals embedded
        in the module (most accurate). If those are unavailable we fall back
        to walking the traceback and consulting the static source map.

        :param error:
        :param compiled_module:
        :param source_map:
        :param template:
        :return:
        """
        origin = getattr(template, 'origin', '<string>')
        template_line = None
        line_getter = getattr(compiled_module, '__vibora_template_line__', None)
        origin_getter = getattr(compiled_module, '__vibora_template_origin__', None)
        if callable(line_getter):
            try:
                template_line = line_getter()
            except Exception:
                template_line = None
        if callable(origin_getter):
            try:
                origin = origin_getter()
            except Exception:
                pass
        generated_line = None
        fallback_line = None
        traceback = getattr(error, '__traceback__', None)
        while traceback is not None:
            fallback_line = traceback.tb_lineno
            if traceback.tb_frame.f_code.co_filename == '<string>':
                generated_line = traceback.tb_lineno
            traceback = traceback.tb_next
        if template_line is None and source_map is not None and generated_line is not None:
            origin, template_line = source_map.lookup(generated_line)
        elif template_line is None and source_map is not None and fallback_line is not None:
            origin, template_line = source_map.lookup(fallback_line)
        template_line = template_line or 1
        template_line_text = ''
        lines = template.content.splitlines()
        if 0 < template_line <= len(lines):
            template_line_text = lines[template_line - 1].strip()
        return TemplateRenderError(
            template=template, template_line=template_line_text, exception=error,
            template_name=origin
        )

    @classmethod
    def wrap_render_function(cls, render_function, compiled_module, source_map, template):
        """
        Wraps the raw compiled render function so exceptions raised while
        rendering are translated to template-aware errors.

        :param render_function:
        :param compiled_module:
        :param source_map:
        :param template:
        :return:
        """
        def render(context, *args, **kwargs):
            try:
                return render_function(context, *args, **kwargs)
            except Exception as error:
                if isinstance(error, TemplateRenderError):
                    raise
                raise cls.translate_exception(
                    error, compiled_module, source_map, template
                ).with_traceback(sys.exc_info()[2])

        render.__wrapped__ = render_function
        return render
