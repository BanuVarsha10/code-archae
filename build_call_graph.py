import ast, os, json
from collections import defaultdict


def get_current_functions_and_calls(repo_dir):
    functions = {}
    calls = []

    for root, dirs, files in os.walk(repo_dir):
        if '.git' in dirs:
            dirs.remove('.git')
        for fname in files:
            if not fname.endswith('.py'):
                continue
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, repo_dir).replace('\\', '/')
            try:
                with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                    source = f.read()
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError):
                continue

            class CallFinder(ast.NodeVisitor):
                def __init__(self):
                    self.class_stack = []
                    self.function_stack = []

                def visit_ClassDef(self, node):
                    self.class_stack.append(node.name)
                    self.generic_visit(node)
                    self.class_stack.pop()

                def visit_FunctionDef(self, node):
                    qualified = '.'.join(self.class_stack + [node.name])
                    functions[qualified] = rel_path
                    self.function_stack.append(qualified)
                    self.generic_visit(node)
                    self.function_stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Call(self, node):
                    if self.function_stack:
                        caller = self.function_stack[-1]
                        callee = None
                        if isinstance(node.func, ast.Attribute):
                            base = node.func.value
                            if isinstance(base, ast.Name) and base.id in ('self', 'cls'):
                                callee = node.func.attr
                        if callee:
                            calls.append((caller, rel_path, callee))
                    self.generic_visit(node)

            CallFinder().visit(tree)

    return functions, calls


def resolve_calls(functions, calls):
    bare_to_qualified = defaultdict(list)
    for qname in functions:
        bare = qname.split('.')[-1]
        bare_to_qualified[bare].append(qname)

    resolved = []
    for caller, caller_file, callee_bare in calls:
        candidates = bare_to_qualified.get(callee_bare)
        if not candidates:
            continue
        if len(candidates) == 1:
            resolved.append((caller, callee_bare, candidates[0], False, None))
        else:
            resolved.append((caller, callee_bare, None, True, json.dumps(sorted(candidates))))
    return resolved


def build_and_store(repo_key, conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS call_graph (
        caller_qualified_name TEXT,
        callee_bare_name TEXT,
        resolved_qualified_name TEXT,
        is_ambiguous INTEGER,
        candidates TEXT
    )''')
    conn.execute('DELETE FROM call_graph')
    functions, calls = get_current_functions_and_calls(f'repos/{repo_key}')
    resolved = resolve_calls(functions, calls)
    for caller, callee_bare, resolved_qname, is_ambiguous, candidates in resolved:
        conn.execute(
            'INSERT INTO call_graph VALUES (?, ?, ?, ?, ?)',
            (caller, callee_bare, resolved_qname, int(is_ambiguous), candidates)
        )
    conn.commit()
    return len(resolved)


def get_call_context(qualified_name, conn):
    callees = conn.execute(
        'SELECT callee_bare_name, resolved_qualified_name, is_ambiguous, candidates FROM call_graph WHERE caller_qualified_name = ?',
        (qualified_name,)
    ).fetchall()
    callers = conn.execute(
        'SELECT caller_qualified_name, is_ambiguous, candidates FROM call_graph WHERE resolved_qualified_name = ? OR candidates LIKE ?',
        (qualified_name, f'%"{qualified_name}"%')
    ).fetchall()
    return callees, callers


def format_call_context(qualified_name, conn):
    callees, callers = get_call_context(qualified_name, conn)
    lines = [
        "Call relationships (ONLY detected for self./cls. method calls -- "
        "this is a narrow, incomplete view. An empty or short list here "
        "does NOT mean the function calls nothing or is called by nothing "
        "-- it means our detection method (which only tracks self./cls. "
        "calls) found no such cases. Never state that a function 'has no "
        "callers' or 'calls nothing' based on this data alone."
    ]
    if callees:
        lines.append("Calls:")
        for c in callees:
            if c['is_ambiguous']:
                candidates = json.loads(c['candidates'])
                lines.append(f"  - {c['callee_bare_name']}() -- ambiguous, could be: {', '.join(candidates)}")
            else:
                lines.append(f"  - {c['resolved_qualified_name']} (confident)")
    if callers:
        lines.append("Possibly called by:")
        for c in callers:
            if c['is_ambiguous']:
                candidates = json.loads(c['candidates'])
                lines.append(f"  - {c['caller_qualified_name']} (ambiguous -- could be calling any of: {', '.join(candidates)})")
            else:
                lines.append(f"  - {c['caller_qualified_name']} (confident)")
    if not callees and not callers:
        lines.append("(No self./cls. calls detected in either direction for this function.)")
    return '\n'.join(lines)
