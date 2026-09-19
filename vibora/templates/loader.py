import sys
import time
import os
import threading
from .engine import TemplateEngine
from .template import Template
from .utils import get_import_names


class TemplateLoader(threading.Thread):
    def __init__(self, directories: list, engine: TemplateEngine, supported_files: list=None, interval: int=0.5):
        super().__init__()
        self.directories = directories
        self.engine = engine
        self.supported_files = supported_files or ('.html', '.vib')
        self.cache = {}
        self.path_index = {}
        self.hash_index = {}
        self.interval = interval
        self.has_to_run = True

    def reload_templates(self, paths: list):
        """
        Reloads the given template paths and recompiles only the affected
        closure (the changed templates plus every template depending on them).

        The whole operation is transactional: a parse/conflict/compile error
        rolls the engine back to its previous state.

        :param paths:
        :return:
        """
        # Parse the new contents up front: a read/parse failure must leave the
        # engine and indexes untouched.
        pending = []
        for root, path in paths:
            with open(path, 'r') as f:
                pending.append((root, path, Template(f.read(), origin=path)))

        # Paths whose content actually changed (different content hash).
        changed_paths = set()
        for root, path, template in pending:
            old_template = self.path_index.get(path)
            if old_template is None or old_template.hash != template.hash:
                changed_paths.add(path)

        # Paths merely touched (mtime changed but content identical): nothing
        # to do, the compiled artifact stays valid.
        touched_paths = {path for root, path, template in pending} - changed_paths

        # Cascading paths: templates depending on a changed template. Their AST
        # embeds the parent content (extends/include are merged at prepare
        # time) so they must be re-read and recompiled too.
        cascade_paths = set()
        for root, path, template in pending:
            if path not in changed_paths:
                continue
            old_template = self.path_index.get(path)
            if old_template is None:
                continue
            for dependent_hash in self.engine.get_dependents(old_template.hash):
                values = self.hash_index.get(dependent_hash)
                if values is not None and values[1] not in changed_paths:
                    cascade_paths.add(values[1])
        for path in cascade_paths:
            old_template = self.path_index[path]
            values = self.hash_index[old_template.hash]
            with open(path, 'r') as f:
                pending.append((values[0], path, Template(f.read(), origin=path)))

        to_reload = changed_paths | cascade_paths
        if not to_reload:
            return

        with self.engine.transaction():
            # Drop old registrations of changed and cascading templates.
            for path in to_reload:
                old_template = self.path_index.get(path)
                if old_template is not None:
                    self.engine.remove_template(old_template)

            # Register the new versions; conflicts raise and roll the whole
            # transaction back.
            for root, path, template in pending:
                if path in to_reload:
                    self.add_to_engine(root, path, template=template)

            # Recompile only the affected closure instead of recompiling all
            # templates on every file change.
            self.engine.sync_cache()
            templates_to_compile = [self.path_index[path] for path in to_reload]
            # Prepare every template first so dependency metadata is populated
            # before we compute the compilation order.
            for compiled_template in templates_to_compile:
                self.engine.prepare_template(compiled_template)
            # Compile dependencies before dependents (Kahn topological sort).
            templates_by_hash = {
                compiled_template.hash: compiled_template for compiled_template in templates_to_compile
            }
            compiled_hashes = set()
            pending = list(templates_to_compile)
            while pending:
                progressed = False
                remaining = []
                for compiled_template in pending:
                    dependencies = templates_by_hash.keys() & compiled_template.dependencies
                    if dependencies <= compiled_hashes:
                        self.engine.compile_template(compiled_template, invalidate=False)
                        compiled_hashes.add(compiled_template.hash)
                        progressed = True
                    else:
                        remaining.append(compiled_template)
                pending = remaining
                if not progressed and pending:
                    # Dependency cycle fallback: compile the rest in order.
                    for compiled_template in pending:
                        self.engine.compile_template(compiled_template, invalidate=False)
                    break

        # Drop stale index entries for templates no longer registered.
        live_hashes = {template.hash for template in self.engine.templates.values()}
        for template_hash in list(self.hash_index.keys()):
            if template_hash not in live_hashes:
                del self.hash_index[template_hash]

    def check_for_modified_templates(self):
        """

        :return:
        """
        to_be_notified = []
        for directory in self.directories:
            for root, dirs, files in os.walk(directory):
                for file in [f for f in files if f.endswith(self.supported_files)]:
                    path = os.path.join(root, file)
                    try:
                        last_modified = os.stat(path).st_mtime
                    except FileNotFoundError:
                        continue
                    if path not in self.cache:
                        # Newly discovered file: record its mtime and reload.
                        self.cache[path] = last_modified
                        to_be_notified.append((root, path))
                    elif self.cache[path] != last_modified:
                        # The mtime is only promoted after a successful reload
                        # so a broken edit is retried on the next poll.
                        to_be_notified.append((root, path))
        if to_be_notified:
            # Promote the mtimes before reloading. On failure the transaction
            # rolls the engine back and we restore the previous mtimes so the
            # (still broken) file is retried on the next poll instead of being
            # silently accepted.
            previous_mtimes = {}
            for root, path in to_be_notified:
                previous_mtimes[path] = self.cache.get(path)
                try:
                    self.cache[path] = os.stat(path).st_mtime
                except FileNotFoundError:
                    self.cache.pop(path, None)
            try:
                self.reload_templates(to_be_notified)
            except Exception:
                for path, mtime in previous_mtimes.items():
                    if mtime is None:
                        self.cache.pop(path, None)
                    else:
                        self.cache[path] = mtime
                raise

    def add_to_engine(self, root: str, path: str, template: Template=None):
        """

        :param root:
        :param path:
        :param template:
        :return:
        """
        if template is None:
            with open(path, 'r') as f:
                template = Template(f.read(), origin=path)
        else:
            template.origin = path
        names = get_import_names(root, path)
        template = self.engine.add_template(template, names=names)
        self.path_index[path] = template
        self.hash_index[template.hash] = (root, path, template)

    def load(self):
        """

        :return:
        """
        pending_additions = []
        for directory in self.directories:
            for root, dirs, files in os.walk(directory):
                for file in files:
                    if file.endswith(self.supported_files):
                        path = os.path.join(root, file)
                        with open(path, 'r') as f:
                            pending_additions.append(
                                (root, path, Template(f.read(), origin=path))
                            )
                        try:
                            self.cache[path] = os.stat(path).st_mtime
                        except FileNotFoundError:
                            pass
        with self.engine.transaction():
            for root, path, template in pending_additions:
                self.add_to_engine(root, path, template=template)

    def run(self):
        while self.has_to_run:
            try:
                self.check_for_modified_templates()
            except Exception as error:
                # A broken edit must never kill the watcher thread.
                sys.stderr.write(f'Template reloading failed: {error}\n')
            time.sleep(self.interval)
