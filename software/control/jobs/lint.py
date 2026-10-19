"""Static check of a job file while it is typed (no Qt, no hardware, nothing is executed).

lint(source, channels=None, groups=None) -> list of Issue

Finds what can be seen from the text alone, before any test is run:
  * syntax errors (with line and column),
  * no run_job(scope) / wrong arguments of run_job,
  * typos:  scope.captur(...), pt.indx, a name that is never defined,
  * wrong arguments:  scope.capture() without channel, scope.capture("488", sav=False), too many arguments,
  * unknown or ambiguous channel names (when the channel list is known), also in the settings at the top of the file
    (names that contain "CHANNEL", e.g. CHANNELS = [...], AF_CHANNEL = "561"),
  * unknown group names in scope.points("A") (a warning: the positions may differ later),
  * a function used like a property (scope.channels()) or a function without parentheses (scope.capture),
  * scope.timepoints(...) not used as the iterable of a for loop,
  * a loop over positions that images or focuses without ever calling scope.move_to,
  * time.sleep (cannot be aborted; use scope.wait).
The dry run (Test) finds the rest: it really executes the job, with simulated time.
"""
import ast
import builtins
import difflib
import inspect
from dataclasses import dataclass

from control.jobs.api import Scope, JobError, resolve_channel_name
from control.jobs import apidoc


@dataclass
class Issue:
    line: int            # 1-based
    col: int             # 0-based, in characters
    end_col: int
    severity: str        # 'error' | 'warning' | 'info'
    message: str

    def __str__(self):
        return f'line {self.line}: {self.severity}: {self.message}'


PUBLIC_SCOPE = {n for n in dir(Scope) if not n.startswith('_')}
METHODS = {n for n in PUBLIC_SCOPE if callable(getattr(Scope, n))}
PROPERTIES = PUBLIC_SCOPE - METHODS


def _suggest(name, choices):
    m = difflib.get_close_matches(name, list(choices), n=1, cutoff=0.6)
    return f" Did you mean '{m[0]}'?" if m else ''


class _Linter:
    def __init__(self, source, channels, groups):
        self.src = source
        self.lines = source.splitlines()
        self.channels = list(channels) if channels else None
        self.groups = list(groups) if groups else None
        self.issues = []

    # --- helpers
    def _char_col(self, lineno, byte_col):
        try:
            line = self.lines[lineno - 1]
            return len(line.encode('utf-8')[:byte_col].decode('utf-8', errors='ignore'))
        except Exception:
            return byte_col

    def add(self, node, severity, message):
        line = getattr(node, 'lineno', 1)
        col = self._char_col(line, getattr(node, 'col_offset', 0))
        end_line = getattr(node, 'end_lineno', line)
        if end_line == line and getattr(node, 'end_col_offset', None) is not None:
            end_col = self._char_col(line, node.end_col_offset)
        else:
            end_col = len(self.lines[line - 1]) if 0 < line <= len(self.lines) else col + 1
        self.issues.append(Issue(line, col, max(end_col, col + 1), severity, message))

    # --- main
    def run(self):
        try:
            tree = ast.parse(self.src)
        except SyntaxError as e:
            line = e.lineno or 1
            col = max(0, (e.offset or 1) - 1)
            end = len(self.lines[line - 1]) if 0 < line <= len(self.lines) else col + 1
            self.issues.append(Issue(line, col, max(end, col + 1), 'error', f'Syntax error: {e.msg}'))
            return self.issues
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child._parent = parent
        fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_job'), None)
        if fn is None:
            self.issues.append(Issue(1, 0, 1, 'error', 'The file must define  def run_job(scope):  - the job starts there.'))
            self._undefined_names(tree)
            return self.issues
        if len(fn.args.args) != 1:
            self.add(fn, 'error', 'run_job must take exactly one argument:  def run_job(scope):')
        scope_name = fn.args.args[0].arg if fn.args.args else 'scope'
        if ast.get_docstring(fn) is None:
            self.add(fn, 'info', 'run_job has no description. Add a docstring: say what the job does and what the user has to provide (it is shown when the job is opened).')
        self._loop_var_kinds = self._find_loop_vars(fn, scope_name)
        self._module_channel_settings(tree)
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute):
                self._attribute(node, scope_name)
            elif isinstance(node, ast.Call):
                self._call(node, scope_name)
            elif isinstance(node, ast.For):
                self._for_loop(node, scope_name)
        self._undefined_names(tree)
        self.issues.sort(key=lambda i: (i.line, i.col))
        return self.issues

    # --- loop variables whose type we know
    def _find_loop_vars(self, fn, scope_name):
        kinds, bad = {}, set()
        for node in ast.walk(fn):
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                kind = None
                it = node.iter
                if isinstance(it, ast.Call) and self._is_scope_attr(it.func, scope_name):
                    kind = {'points': 'Point', 'timepoints': 'TimePoint'}.get(it.func.attr)
                name = node.target.id
                if kind is None or (name in kinds and kinds[name] != kind):
                    bad.add(name)
                else:
                    kinds[name] = kind
        for name in bad:
            kinds.pop(name, None)
        # names assigned in other ways (pt = ...) are not trusted
        for node in ast.walk(fn):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        kinds.pop(t.id, None)
        return kinds

    @staticmethod
    def _is_scope_attr(node, scope_name):
        return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == scope_name

    # --- attributes: scope.xxx and pt.xxx
    def _attribute(self, node, scope_name):
        if self._is_scope_attr(node, scope_name):
            name = node.attr
            if name.startswith('_'):
                self.add(node, 'warning', f"'{name}' is internal; use only the documented scope functions (see Help).")
                return
            if name not in PUBLIC_SCOPE:
                self.add(node, 'error', f"scope has no '{name}'." + _suggest(name, PUBLIC_SCOPE) + ' (Help lists all functions.)')
                return
            parent = getattr(node, '_parent', None)
            called = isinstance(parent, ast.Call) and parent.func is node
            if name in PROPERTIES and called:
                self.add(node, 'error', f"scope.{name} is not a function: remove the parentheses.")
            elif name in METHODS and not called:
                self.add(node, 'warning', f"scope.{name} is a function: add parentheses, e.g. scope.{name}(...).")
        elif isinstance(node.value, ast.Name) and node.value.id in self._loop_var_kinds:
            kind = self._loop_var_kinds[node.value.id]
            allowed = apidoc.POINT_ATTRS if kind == 'Point' else apidoc.TIMEPOINT_ATTRS
            if node.attr not in allowed and not node.attr.startswith('_'):
                self.add(node, 'error', f"a {kind} has no '{node.attr}'." + _suggest(node.attr, allowed) + f" It has: {', '.join(allowed)}.")

    # --- calls
    def _call(self, node, scope_name):
        func = node.func
        # time.sleep
        if isinstance(func, ast.Attribute) and func.attr == 'sleep' and isinstance(func.value, ast.Name) and func.value.id == 'time':
            self.add(node, 'warning', 'time.sleep cannot be interrupted by Abort. Use scope.wait(seconds).')
        if not self._is_scope_attr(func, scope_name) or func.attr not in METHODS:
            return
        name = func.attr
        has_star = any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)
        if not has_star:
            try:
                inspect.signature(getattr(Scope, name)).bind(object(), *[0] * len(node.args), **{k.arg: 0 for k in node.keywords})
            except TypeError as e:
                self.add(node, 'error', f'scope.{name}: {e}. Usage: {apidoc.api_by_name().get(name, {}).get("signature", "")}')
        # arguments with meaning
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if name == 'capture' or name == 'autofocus':
            arg = kw.get('channel', node.args[0] if node.args else None)
            if arg is not None:
                self._check_channel_expr(arg)
        elif name == 'capture_channels':
            arg = kw.get('channels', node.args[0] if node.args else None)
            if isinstance(arg, (ast.List, ast.Tuple)):
                for el in arg.elts:
                    self._check_channel_expr(el)
        elif name == 'points' and self.groups is not None:
            for a in node.args:
                for el in (a.elts if isinstance(a, (ast.List, ast.Tuple)) else [a]):
                    if isinstance(el, ast.Constant) and isinstance(el.value, str) and el.value not in self.groups:
                        self.add(el, 'warning', f"no position of the test positions is in group '{el.value}'" + _suggest(el.value, self.groups) +
                                 f' (groups: {self.groups}). The group is the part of the ID before the first "-".')
        elif name == 'timepoints':
            parent = getattr(node, '_parent', None)
            if not (isinstance(parent, ast.For) and parent.iter is node):
                self.add(node, 'warning', 'scope.timepoints(...) is a loop: write  for t in scope.timepoints(...):  and indent the lines that belong to it.')
        elif name == 'move_to' and node.args and isinstance(node.args[0], ast.Name):
            if self._loop_var_kinds.get(node.args[0].id) == 'TimePoint':
                self.add(node.args[0], 'error', 'move_to needs a position (from scope.points()), not a time point.')

    def _check_channel_expr(self, node):
        if self.channels is None:
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            self._check_channel_name(node, node.value)

    def _check_channel_name(self, node, name):
        try:
            resolve_channel_name(name, self.channels)
        except JobError as e:
            msg = str(e)
            if 'unknown' in msg:
                msg = f"unknown channel '{name}'." + _suggest(name, self.channels) + f' Available: {", ".join(self.channels)}'
            self.add(node, 'error', msg)

    def _module_channel_settings(self, tree):
        """CHANNELS = [...], AF_CHANNEL = "561": the strings are checked like arguments of capture()"""
        if self.channels is None:
            return
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if any('CHANNEL' in n.upper() for n in names):
                    vals = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
                    for v in vals:
                        if isinstance(v, ast.Constant) and isinstance(v.value, str):
                            self._check_channel_name(v, v.value)

    # --- loops
    def _for_loop(self, node, scope_name):
        it = node.iter
        if not (isinstance(it, ast.Call) and self._is_scope_attr(it.func, scope_name) and it.func.attr in ('points',)):
            return
        calls = [c for stmt in node.body for c in ast.walk(stmt) if isinstance(c, ast.Call) and self._is_scope_attr(c.func, scope_name)]
        names = {c.func.attr for c in calls}
        if names & {'capture', 'autofocus', 'capture_channels'} and 'move_to' not in names:
            self.add(node, 'warning', 'This loop over positions images or focuses but never calls scope.move_to(...): every image would be taken at the same place. '
                                      'Add  scope.move_to(pt)  first.')

    # --- names that are used but never defined
    def _undefined_names(self, tree):
        defined = set(dir(builtins)) | {'__file__', '__name__', '__doc__'}
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                defined.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(n.name)
                if not isinstance(n, ast.ClassDef):
                    a = n.args
                    for arg in a.args + a.kwonlyargs + getattr(a, 'posonlyargs', []):
                        defined.add(arg.arg)
                    if a.vararg:
                        defined.add(a.vararg.arg)
                    if a.kwarg:
                        defined.add(a.kwarg.arg)
            elif isinstance(n, ast.Lambda):
                a = n.args
                for arg in a.args + a.kwonlyargs:
                    defined.add(arg.arg)
                if a.vararg:
                    defined.add(a.vararg.arg)
                if a.kwarg:
                    defined.add(a.kwarg.arg)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for al in n.names:
                    defined.add((al.asname or al.name).split('.')[0])
                    if al.name == '*':
                        return                     # cannot know
            elif isinstance(n, ast.ExceptHandler) and n.name:
                defined.add(n.name)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                defined.update(n.names)
            elif hasattr(ast, 'MatchAs') and isinstance(n, ast.MatchAs) and n.name:
                defined.add(n.name)
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in defined:
                self.add(n, 'warning', f"'{n.id}' is not defined (typo?)." + _suggest(n.id, defined - set(dir(builtins))))


def lint(source, channels=None, groups=None):
    """Check job source text. channels / groups: names known from the test setup (None = do not check those)."""
    return _Linter(source, channels, groups).run()


def summary(issues):
    e = sum(1 for i in issues if i.severity == 'error')
    w = sum(1 for i in issues if i.severity == 'warning')
    return e, w
