import ast, difflib, sqlite3
import numpy as np
from scipy.optimize import linear_sum_assignment
from pydriller import Repository

class FF(ast.NodeVisitor):
    def __init__(self, source):
        self.source = source; self.functions = []; self.stack = []
    def visit_ClassDef(self, n):
        self.stack.append(n.name); self.generic_visit(n); self.stack.pop()
    def visit_FunctionDef(self, n):
        self.functions.append(('.'.join(self.stack + [n.name]), ast.get_source_segment(self.source, n)))
        self.generic_visit(n)
    visit_AsyncFunctionDef = visit_FunctionDef

def extract(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    ff = FF(source); ff.visit(tree); return ff.functions

def classify_events(before, after, threshold=0.75, min_len=80):
    before_f = [(n, b) for n, b in before if b and len(b) >= min_len]
    after_f  = [(n, b) for n, b in after if b and len(b) >= min_len]
    events = []
    if before_f and after_f:
        sim = np.zeros((len(before_f), len(after_f)))
        for i, (_, ob) in enumerate(before_f):
            for j, (_, nb) in enumerate(after_f):
                sim[i][j] = difflib.SequenceMatcher(None, ob, nb).ratio()
        row_ind, col_ind = linear_sum_assignment(1 - sim)
        matched_old, matched_new = set(), set()
        for r, c in zip(row_ind, col_ind):
            if sim[r][c] >= threshold:
                old_name, new_name = before_f[r][0], after_f[c][0]
                matched_old.add(r); matched_new.add(c)
                if old_name == new_name:
                    events.append((new_name, 'modified', None, sim[r][c]))
                else:
                    events.append((new_name, 'renamed', old_name, sim[r][c]))
        for i, (name, _) in enumerate(before_f):
            if i not in matched_old:
                events.append((name, 'deleted', None, None))
        for j, (name, _) in enumerate(after_f):
            if j not in matched_new:
                events.append((name, 'added', None, None))
    else:
        events += [(n, 'deleted', None, None) for n, _ in before_f]
        events += [(n, 'added', None, None) for n, _ in after_f]
    return events

conn = sqlite3.connect('archaeologist.db')
conn.row_factory = sqlite3.Row
conn.execute('''CREATE TABLE IF NOT EXISTS commits (
    hash TEXT PRIMARY KEY, author TEXT, date TEXT, message TEXT)''')
conn.execute('''CREATE TABLE IF NOT EXISTS function_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_hash TEXT, file_path TEXT, qualified_name TEXT,
    event_type TEXT, old_qualified_name TEXT, similarity REAL)''')
conn.execute('CREATE INDEX IF NOT EXISTS idx_events_name ON function_events(qualified_name)')

count = 0
for commit in Repository('httpx').traverse_commits():
    count += 1
    if count > 150:
        break
    conn.execute('INSERT OR IGNORE INTO commits VALUES (?, ?, ?, ?)',
                  (commit.hash, commit.author.name, str(commit.author_date), commit.msg.splitlines()[0]))
    for mf in commit.modified_files:
        if not mf.filename.endswith('.py') or not mf.source_code or not mf.source_code_before:
            continue
        before, after = extract(mf.source_code_before), extract(mf.source_code)
        for name, event_type, old_name, score in classify_events(before, after):
            conn.execute('INSERT INTO function_events (commit_hash, file_path, qualified_name, event_type, old_qualified_name, similarity) VALUES (?, ?, ?, ?, ?, ?)',
                         (commit.hash, mf.filename, name, event_type, old_name, score))
conn.commit()

n_commits = conn.execute('SELECT COUNT(*) FROM commits').fetchone()[0]
n_events = conn.execute('SELECT COUNT(*) FROM function_events').fetchone()[0]
print(f'Stored {n_commits} commits, {n_events} function events in archaeologist.db')

def get_lifeline(name, conn):
    names_to_check = [name]
    seen_names = set()
    all_events = []
    while names_to_check:
        current = names_to_check.pop()
        if current in seen_names:
            continue
        seen_names.add(current)
        rows = conn.execute('''SELECT function_events.*, commits.date, commits.message
                                FROM function_events JOIN commits ON function_events.commit_hash = commits.hash
                                WHERE qualified_name = ? ORDER BY commits.date''', (current,)).fetchall()
        all_events.extend(rows)
        for row in rows:
            if row['event_type'] == 'renamed' and row['old_qualified_name']:
                names_to_check.append(row['old_qualified_name'])
    all_events.sort(key=lambda r: r['date'])
    return all_events

print()
print("Full lifeline for 'HTTP11Connection.close':")
for row in get_lifeline('HTTP11Connection.close', conn):
    extra = f" (was {row['old_qualified_name']})" if row['old_qualified_name'] else ''
    print(f"  {row['commit_hash'][:8]}  {row['date'][:10]}  {row['event_type']:10}  {row['qualified_name']}{extra}  -- {row['message'][:40]}")
