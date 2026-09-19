import contextlib
from typing import Dict
from .ast import merge, raise_nodes, resolve_include_nodes
from .exceptions import TemplateNotFound, ConflictingNames
from .nodes import ExtendsNode, MacroNode
from .template import Template, TemplateParser, ParsedTemplate, CompiledTemplate
from .cache import InMemoryCache
from .compilers.python import PythonTemplateCompiler


class TemplateEngine:

    def __init__(self, cache_engine=None, compiler=None, extensions: list=None,
                 parser: TemplateParser=None):

        # Loaded templates list, caching purposes.
        # {Name: ParsedTemplate}
        self.templates: Dict[str, ParsedTemplate] = {}

        # This is a cache holding the functions responsible for actually rendering the template.
        # {TemplateHash: CompiledTemplate}
        self.compiled_templates: Dict[str, CompiledTemplate] = {}

        # Templates compiler, translate a template AST to a Python callable.
        self.compiler = compiler or PythonTemplateCompiler()

        # Extensions can modify template nodes before compilation.
        self.extensions = extensions if extensions else []

        # Template compilation is an expensive process so we try to cache as much as possible,
        # especially when using Cython compiler.
        self.cache = cache_engine or InMemoryCache()

        # Template parser.
        self.template_parser = parser or TemplateParser()

    def remove_template(self, template: ParsedTemplate):
        """

        :param template:
        :return:
        """
        keys_to_remove = []
        for name, t in self.templates.items():
            if t.hash == template.hash:
                keys_to_remove.append(name)
        for key in keys_to_remove:
            try:
                del self.templates[key]
            except KeyError:
                pass
            try:
                del self.compiled_templates[template.hash]
            except KeyError:
                pass
        self.cache.remove(template.hash)

    def add_template(self, template: Template, names: list) -> ParsedTemplate:
        """

        :param template:
        :param names:
        :return:
        """
        return self.add_templates([(template, names)])[0]

    def add_templates(self, items: list) -> list:
        """
        Registers multiple templates atomically.

        Every item is a tuple of (Template, names). Parsing and conflict
        validation run before any state mutation, so a conflicting name or a
        parse error leaves the engine untouched (all-or-nothing semantics).

        :param items:
        :return:
        """
        parsed_items = []
        for template, names in items:
            parsed_items.append((self.template_parser.parse(template), list(names)))

        for parsed_template, names in parsed_items:
            for name in names:
                if name in self.templates:
                    raise ConflictingNames(
                        'This template needs a unique name because imports are name based.'
                    )

        for parsed_template, names in parsed_items:
            for name in names:
                self.templates[name] = parsed_template
        return [parsed_template for parsed_template, names in parsed_items]

    def _snapshot(self) -> dict:
        """
        Captures the engine mutable state so it can be restored on failure.

        :return:
        """
        return {
            'templates': dict(self.templates),
            'compiled_templates': dict(self.compiled_templates),
            'cache_templates': dict(self.cache.loaded_templates),
            'cache_metas': dict(self.cache.loaded_metas)
        }

    def _restore(self, snapshot: dict):
        """
        Restores a snapshot previously taken with :meth:`_snapshot`.

        :param snapshot:
        :return:
        """
        self.templates = dict(snapshot['templates'])
        self.compiled_templates = dict(snapshot['compiled_templates'])
        self.cache.loaded_templates = dict(snapshot['cache_templates'])
        self.cache.loaded_metas = dict(snapshot['cache_metas'])

    @contextlib.contextmanager
    def transaction(self):
        """
        Context manager providing transactional template loading.

        Any exception raised inside the block rolls the engine back to the
        state captured when the block started.
        """
        snapshot = self._snapshot()
        try:
            yield self
        except Exception:
            self._restore(snapshot)
            raise

    def get_dependents(self, template_hash: str) -> set:
        """
        Returns the hashes of every registered template that depends
        (directly or transitively) on the given one.

        :param template_hash:
        :return:
        """
        dependents = set()
        pending = [template_hash]
        while pending:
            current_hash = pending.pop()
            for template in self.templates.values():
                if current_hash in template.dependencies and template.hash not in dependents:
                    dependents.add(template.hash)
                    pending.append(template.hash)
        return dependents

    def compile_template(self, template: ParsedTemplate, verbose: bool=False, invalidate: bool=True):
        """
        Compiles and caches a single template.

        Previously compiled versions of the template itself and every template
        depending on it are invalidated so a hot reload never serves a stale
        dependency chain.

        :param template:
        :param verbose:
        :return:
        """
        if template.hash in self.compiled_templates and self.cache.get(template.hash) is not None:
            return self.compiled_templates[template.hash]

        hashes_to_invalidate = {template.hash}
        if invalidate:
            hashes_to_invalidate |= self.get_dependents(template.hash)
        for template_hash in hashes_to_invalidate:
            self.compiled_templates.pop(template_hash, None)
            self.cache.remove(template_hash)

        if not template.prepared:
            self.prepare_template(template)

        compiled_template = self.compiler.compile(template, verbose=verbose)
        self.cache.store(compiled_template)
        self.compiled_templates[template.hash] = compiled_template
        return compiled_template

    async def render(self, name: str, streaming: bool=False, **template_vars):
        """

        :param streaming:
        :param name:
        :param template_vars:
        :return:
        """
        try:
            template = self.templates[name]
            try:
                compiled_template = self.compiled_templates[template.hash]
                template_generator = compiled_template.render({**template_vars})
                if streaming:
                    return template_generator
                try:
                    content = ''
                    async for chunk in template_generator:
                        content += chunk
                    return content
                except Exception as error:
                    raise compiled_template.render_exception(error, name=name)
            except KeyError:
                raise Exception('You need to compile your templates first.')
        except KeyError:
            raise TemplateNotFound(name)

    def get_template(self, name: str):
        """

        :param name:
        :return:
        """
        try:
            return self.templates[name]
        except KeyError:
            raise TemplateNotFound(name)

    def get_compiled_template(self, template: Template):
        """

        :param template:
        :return:
        """
        try:
            return self.compiled_templates[template.hash]
        except KeyError:
            raise TemplateNotFound(f'You need to compile this template first.')

    def prepare_template(self, template: ParsedTemplate):
        """

        :param template:
        :return:
        """

        # Checking if this template is already prepared.
        if template.prepared:
            return

        # Calling extensions so they have a chance to modify the template include/extends node.
        for extension in self.extensions:
            extension.before_prepare(self, template)

        # Resolving "include" nodes.
        relationships = resolve_include_nodes(self, template.ast.children)
        for t in relationships:
            template.dependencies.add(t.hash)

        # Resolving "extends" nodes.
        for index, node in enumerate(template.ast.children):
            if isinstance(node, ExtendsNode):
                parent = self.get_template(node.parent)
                template.dependencies.add(parent.hash)
                self.prepare_template(parent)
                template.ast = merge(parent, template)

        # Macro nodes needs to be compiled first.
        raise_nodes(lambda x: isinstance(x, MacroNode), template.ast)

        template.prepared = True

    def sync_cache(self):
        """

        :return:
        """
        updated_hashes = [t.hash for t in self.templates.values()]
        for template_hash, meta in self.cache.loaded_metas.items():
            if any([x for x in meta.dependencies if x not in updated_hashes]):
                self.cache.remove(template_hash)

    def compile_templates(self, verbose=False):
        """

        :param verbose:
        :return:
        """
        # Checking if all dependencies are met
        for template in self.templates.values():

            # Trying to load the compiled version from cache,
            # if not possible then let's call the compiler to build this template.
            compiled_template = self.cache.get(template.hash)
            if compiled_template is None:

                # Optimizing/Replacing nodes so we compile it with the final AST.
                if not template.prepared:
                    self.prepare_template(template)

                compiled_template = self.compiler.compile(template, verbose=verbose)
                self.cache.store(compiled_template)

            # Caching the render function for fast access.
            self.compiled_templates[template.hash] = compiled_template

        # Cleaning old template cache files.
        self.cache.clean(set([t.hash for t in self.templates.values()]))
