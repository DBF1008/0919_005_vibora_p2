import os
import json
import platform
import typing
from .exceptions import FailedToCompileTemplate, TemplateRenderError


def find_template_binary(path: str):
    name = os.listdir(path)[-1]
    if not name:
        raise FailedToCompileTemplate(path)
    return os.path.join(path, name)


class CompilerFlavor:
    TEMPLATE = 1
    MACRO = 2


def get_scope_by_args(function_def: str):
    open_at = function_def.find('(') + 1
    close_at = function_def.rfind(')')
    args = function_def[open_at:close_at]
    scope = []
    for value in args.split(','):
        if value.find('='):
            scope.append(value.split('=')[0].strip())
        else:
            scope.append(value.strip())
    return scope


def get_function_name(definition: str):
    return definition[:definition.find('(')].strip()


class TemplateMeta:
    def __init__(self, entry_point: str, version: str, template_hash: str,
                 created_at: str, compiler: str, architecture: str, compilation_time: float,
                 dependencies: list=None, source_map: dict=None, template_name: str=None):
        self.entry_point = entry_point
        self.version = version
        self.template_hash = template_hash
        self.created_at = created_at
        self.compiler = compiler
        self.architecture = architecture
        self.compilation_time = compilation_time
        self.dependencies = dependencies or []
        # Maps generated code line numbers to (template_line_number, raw_source)
        # so runtime exceptions can be traced back to the original template.
        self.source_map = source_map or {}
        self.template_name = template_name

    @classmethod
    def load_from_path(cls, path: str):
        with open(path) as f:
            return TemplateMeta(**json.loads(f.read()))

    def store(self, path: str):
        with open(path, 'w') as f:
            values = self.__dict__.copy()
            values['dependencies'] = list(self.dependencies)
            f.write(json.dumps(values))


class CompilationResult:
    def __init__(self, template, meta: TemplateMeta, render_function: typing.Callable,
                 code: bytes):
        self.template = template
        self.meta = meta
        self.render_function = render_function
        self.code = code

    def lookup_template_line(self, line_number: int):
        """Translates a generated code line number back to the original
        template position using the source map injected at compile time.

        :param line_number:
        :return: (template_line_number, raw_source) or None.
        """
        if not self.meta.source_map:
            return None
        # JSON serialization turns int keys into strings, so we accept both.
        return self.meta.source_map.get(line_number) or self.meta.source_map.get(str(line_number))

    def render_exception(self, error: Exception, name: str=None):
        """Builds a TemplateRenderError pointing to the original template
        file and line instead of the generated (compiled) code.

        :param error:
        :param name:
        :return:
        """
        template_line = ''
        template_line_number = None
        tb = error.__traceback__
        while tb is not None:
            filename = tb.tb_frame.f_code.co_filename
            # Only frames belonging to generated template code are translated.
            if filename.endswith(('.pyx', '.py')) or 'vt_' in os.path.basename(filename):
                found = self.lookup_template_line(tb.tb_lineno)
                if found:
                    template_line_number, template_line = found[0], found[1]
            tb = tb.tb_next
        template_name = name or self.meta.template_name or getattr(self.template, 'name', None)
        raise TemplateRenderError(
            template=self.template, template_line=template_line, exception=error,
            template_name=template_name, template_file=template_name,
            template_line_number=template_line_number
        )


def get_architecture_signature() -> str:
    return ''.join(platform.architecture())


def generate_entry_point(template) -> str:
    return 'render_' + template.hash


def get_import_names(root: str, template_path: str):
    names = {os.path.join(root, template_path), os.path.basename(template_path)}
    pieces = template_path.split('/')
    for index in range(1, len(pieces)):
        names.add(os.path.sep.join(pieces[index:]))
    return list(names)
