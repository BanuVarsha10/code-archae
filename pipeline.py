import sys, os, re, subprocess
import ast, difflib, sqlite3
from collections import Counter
import numpy as np
from scipy.optimize import linear_sum_assignment
from pydriller import Repository
import repo_registry as registry


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
    before_counts = Counter(n for n, _ in before_f)
    after_counts = Counter(n for n, _ in after_f)
    events = []
    matched_old_idx, matched_new_idx = set(), set()

    for i, (name, _) in enumerate(before_f):
        if before_counts[name] == 1 and after_counts.get(name, 0) == 1:
            for j, (name2, _) in enumerate(after_f):
                if name2 == name:
                    events.append((name, 'modified', None, None))
                    matched_old_idx.add(i); matched_new_idx.add(j)
                    break

    remaining_before = [(n, b) for i, (n, b) in enumerate(before_f) if i not in matched_old_idx]
    remaining_after  = [(n, b) for j, (n, b) in enumerate(after_f) if j not in matched_new_idx]

    if remaining_before and remaining_after:
        sim = np.zeros((len(remaining_before), len(remaining_after)))
        for i, (_, ob) in enumerate(remaining_before):
            for j, (_, nb) in enumerate(remaining_after):
                sim[i][j] = difflib.SequenceMatcher(None, ob, nb).ratio()
        row_ind, col_ind = linear_sum_assignment(1 - sim)
        matched_r, matched_c = set(), set()
        for r, c in zip(row_ind, col_ind):
            if sim[r][c] >= threshold:
                old_name, new_name = remaining_before[r][0], remaining_after[c][0]
                matched_r.add(r); matched_c.add(c)
                if old_name == new_name:
                    events.append((new_name, 'modified', None, sim[r][c]))
                else:
                    events.append((new_name, 'renamed', old_name, sim[r][c]))
        for i, (name, _) in enumerate(remaining_before):
            if i not in matched_r:
                events.append((name, 'deleted', None, None))
        for j, (name, _) in enumerate(remaining_after):
            if j not in matched_c:
                events.append((name, 'added', None, None))
    else:
        events += [(n, 'deleted', None, None) for n, _ in remaining_before]
        events += [(n, 'added', None, None) for n, _ in remaining_after]
    return events


def ensure_cloned(repo_url, clone_dir):
    if os.path.exists(clone_dir):
        return
    subprocess.run(['git', 'clone', repo_url, clone_dir], check=True)


def run_pipeline(repo_url, max_commits=450):
    repo_key = registry.repo_key_from_url(repo_url)
    clone_dir = os.path.join('repos', repo_key)
    db_path = f'{repo_key}.db'

    registry.set_status(repo_key, status='cloning', repo_url=repo_url,
                         db_path=db_path, processed=0, total=max_commits)
    try:
        ensure_cloned(repo_url, clone_dir)
    except subprocess.CalledProcessError as e:
        registry.set_status(repo_key, status='failed', error=f'clone failed: {e}')
        return None

    conn = sqlite3.connect(db_path)
    conn.execute('''CREATE TABLE IF NOT EXISTS commits (
        hash TEXT PRIMARY KEY, author TEXT, date TEXT, message TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS function_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        commit_hash TEXT, file_path TEXT, qualified_name TEXT,
        event_type TEXT, old_qualified_name TEXT, similarity REAL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_events_name ON function_events(qualified_name)')
    conn.execute('DELETE FROM function_events')
    conn.execute('DELETE FROM commits')

    registry.set_status(repo_key, status='indexing')

    try:
        count = 0
        for commit in Repository(clone_dir).traverse_commits():
            if count >= max_commits:
                break
            count += 1
            conn.execute('INSERT OR IGNORE INTO commits VALUES (?, ?, ?, ?)',
                         (commit.hash, commit.author.name, str(commit.author_date),
                          commit.msg.splitlines()[0] if commit.msg else ''))
            for mf in commit.modified_files:
                if not mf.filename.endswith('.py'):
                    continue
                file_path = mf.new_path or mf.old_path
                if mf.source_code_before and mf.source_code:
                    before, after = extract(mf.source_code_before), extract(mf.source_code)
                    for name, event_type, old_name, score in classify_events(before, after):
                        conn.execute(
                            'INSERT INTO function_events (commit_hash, file_path, qualified_name, event_type, old_qualified_name, similarity) VALUES (?, ?, ?, ?, ?, ?)',
                            (commit.hash, file_path, name, event_type, old_name, score))
                elif mf.source_code and not mf.source_code_before:
                    # Whole new file (e.g. the repo's first commit, or a file
                    # re-added after history was rewritten) -- every function
                    # in it is genuinely new, not a diff against nothing.
                    for name, body in extract(mf.source_code):
                        if body and len(body) >= 80:
                            conn.execute(
                                'INSERT INTO function_events (commit_hash, file_path, qualified_name, event_type, old_qualified_name, similarity) VALUES (?, ?, ?, ?, ?, ?)',
                                (commit.hash, file_path, name, 'added', None, None))
                elif mf.source_code_before and not mf.source_code:
                    # Whole file deleted -- every function it contained is
                    # genuinely gone, not a diff against nothing.
                    for name, body in extract(mf.source_code_before):
                        if body and len(body) >= 80:
                            conn.execute(
                                'INSERT INTO function_events (commit_hash, file_path, qualified_name, event_type, old_qualified_name, similarity) VALUES (?, ?, ?, ?, ?, ?)',
                                (commit.hash, file_path, name, 'deleted', None, None))
            if count % 20 == 0:
                conn.commit()
                registry.set_status(repo_key, processed=count)
        conn.commit()
    except Exception as e:
        registry.set_status(repo_key, status='failed', error=str(e))
        return None

    registry.set_status(repo_key, status='done', processed=count, total=count)

    registry.set_status(repo_key, status='fetching_issues')
    try:
        import fetch_issues_multi
        fetched, cached, failed = fetch_issues_multi.fetch_issues(db_path, repo_url)
        registry.set_status(repo_key, status='done', issues_fetched=fetched,
                             issues_cached=cached, issues_failed=failed)
    except Exception as e:
        registry.set_status(repo_key, status='done', issues_error=str(e))

    return repo_key, db_path


if __name__ == '__main__':
    repo_url = sys.argv[1]
    max_commits = int(sys.argv[2]) if len(sys.argv) > 2 else 450
    result = run_pipeline(repo_url, max_commits)
    if result:
        repo_key, db_path = result
        print(f'Done: {repo_key} -> {db_path}')
    else:
        print('Pipeline failed -- check repos.json for the error')
