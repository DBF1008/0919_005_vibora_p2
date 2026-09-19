import hashlib
import importlib.util
import os
import tempfile
import time
import datetime
from ..compilers.base import TemplateCompiler
from ..utils import find_template_binary, CompilerFlavor, TemplateMeta, get_architecture_signature, CompilationResult


# TODO: Remove 'render' hardcoded name.


class CythonTemplateCompiler(TemplateCompiler):

    NAME = 'cython'
    VERSION = '0.0.1'
    EXTENSION_NAME = 'compiled_templates'

    def __init__(self, flavor=CompilerFlavor.TEMPLATE, temporary_dir: str=None):
        super().__init__()
        self.content = ''
        self.current_scope = list()
        self.accumulated_text = ''
        self.content_var = '__content__'
        self.context_var = '__context__'
        self.functions = []
        self.flavor = flavor
        self.temporary_dir = temporary_dir or tempfile.gettempdir()
        # Source mapping state: allows runtime exceptions raised by the
        # compiled binary to be traced back to the original template file/line.
        self.pending_comment = None
        self.template_line = 1
        self.source_map = {}

    def clean(self):
        self._indentation = 0
        self.content = ''
        self.current_scope = list()
        self.accumulated_text = ''
        self.functions = []
        self.flavor = CompilerFlavor.TEMPLATE
        self.pending_comment = None
        self.template_line = 1
        self.source_map = {}

    def add_text(self, content: str):
        # Tracking the original template line so comments/statements
        # can be mapped back to their source position.
        self.template_line += content.count('\n')
        content = content.replace("\n", "\\n")
        content = content.replace(r'"', r'\"')
        self.accumulated_text += content

    def flush_text(self):
        text = self.accumulated_text
        self.accumulated_text = ''
        stm = f'{self.content_var}.append("{text}")'
        self.add_statement(stm, flush_comments=False)

    def add_comment(self, content: str):
        self.pending_comment = content.strip()

    def add_statement(self, content: str, flush_comments: bool=True):
        if self.accumulated_text:
            self.flush_text()
        if self.pending_comment and flush_comments:
            raw = ' '.join(self.pending_comment.split())
            self.pending_comment = None
            # The original template statement is embedded as a comment so the
            # generated .pyx file is self-describing when inspected manually.
            self.content += (' ' * self._indentation) + '# ' + raw + '\n'
            # The next generated line maps back to the original template line.
            self.source_map[self.content.count('\n') + 1] = (self.template_line, raw)
        new_content = (' ' * self._indentation) + content.strip() + '\n'
        self.content += new_content

    def get_source_map(self) -> dict:
        """Returns the source map adjusted to the final .pyx file layout
        (helper functions are prepended to the generated content).

        :return: {generated_line: (template_line, raw_source)}
        """
        offset = 0
        for helper_function in self.functions:
            offset += helper_function.count('\n') + 2
        return {line + offset: value for line, value in self.source_map.items()}

    def consume(self, template):
        self.add_statement(f'cpdef str render(dict {self.context_var}):')
        self._indentation += 4
        self.add_statement(f"cdef list {self.content_var} = []")
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
        # Imported lazily so the compiler module remains importable
        # (and its source mapping introspectable) without a build toolchain.
        from setuptools import Extension, setup

        # Tracking compile times
        started_at = time.time()

        # Temporary directory for this compilation.
        working_dir = tempfile.TemporaryDirectory(dir=self.temporary_dir)

        # Generating .pyx files.
        self.consume(template)
        # print(template.hash)
        # print(self.content)
        template_hash = hashlib.md5(self.content.encode()).hexdigest()
        temp_path = os.path.join(working_dir.name, 'vt_' + template_hash + '.pyx')
        with open(temp_path, 'w') as f:
            for helper_function in self.functions:
                f.write(helper_function + '\n\n')
            f.write(self.content)

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
            dependencies=template.dependencies,
            source_map=self.get_source_map(),
            template_name=getattr(template, 'name', None)
        )

        # Compilation result contains the meta data and the render function loaded at runtime.
        compilation = CompilationResult(
            template=template,
            meta=meta, render_function=compiled_template.render, code=open(compiled_path, 'rb').read()
        )

        # Clearing state
        self.clean()

        # Binding the render function
        return compilation

    @classmethod
    def generate_template_name(cls, hash_: str):
        return hash_ + '.so'
