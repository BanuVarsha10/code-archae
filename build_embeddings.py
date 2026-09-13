import sqlite3, os, ast, pickle
import numpy as np
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')


def get_current_source(qualified_name, conn, repo_dir):
    row = conn.execute('''
        SELECT file_path FROM function_events fe
        JOIN commits c ON fe.commit_hash = c.hash
        WHERE qualified_name = ? AND event_type != 'deleted'
        ORDER BY c.date DESC LIMIT 1
    ''', (qualified_name,)).fetchone()
    if not row:
        return None
    file_path = row['file_path']
    full_path = os.path.join(repo_dir, file_path)
    if not os.path.exists(full_path):
        return None
    with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
        source = f.read()

    class FF(ast.NodeVisitor):
        def __init__(self, source):
            self.source = source; self.functions = {}; self.stack = []
        def visit_ClassDef(self, n):
            self.stack.append(n.name); self.generic_visit(n); self.stack.pop()
        def visit_FunctionDef(self, n):
            self.functions['.'.join(self.stack + [n.name])] = ast.get_source_segment(self.source, n)
            self.generic_visit(n)
        visit_AsyncFunctionDef = visit_FunctionDef

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    ff = FF(source); ff.visit(tree)
    return ff.functions.get(qualified_name)


def build_fallback_text(qualified_name, conn):
    """For functions no longer present in current HEAD (deleted, refactored
    away, superseded), build embeddable text from what we still track:
    the name itself, every file path it ever lived at, and the commit
    messages associated with its changes. Not as good as real code, but
    real, and it means a historical-only function stays findable instead
    of invisible -- which matters a lot given this project's whole premise
    is explaining history, not just current state."""
    events = conn.execute('''
        SELECT fe.file_path, c.message
        FROM function_events fe JOIN commits c ON fe.commit_hash = c.hash
        WHERE qualified_name = ? ORDER BY c.date
    ''', (qualified_name,)).fetchall()
    if not events:
        return None
    parts = [qualified_name.replace('.', ' ').replace('_', ' ')]
    file_paths = set(e['file_path'] for e in events if e['file_path'])
    parts.extend(p.replace('/', ' ').replace('.py', '').replace('_', ' ') for p in file_paths)
    messages = set(e['message'] for e in events if e['message'])
    parts.extend(messages)
    return ' '.join(parts)


def build_embeddings(repo_key, conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS function_embeddings (
        qualified_name TEXT PRIMARY KEY, embedding BLOB, source_type TEXT)''')
    existing_cols = {row[1] for row in conn.execute('PRAGMA table_info(function_embeddings)').fetchall()}
    if 'source_type' not in existing_cols:
        conn.execute('ALTER TABLE function_embeddings ADD COLUMN source_type TEXT')
    conn.execute('DELETE FROM function_embeddings')

    names = [r[0] for r in conn.execute('SELECT DISTINCT qualified_name FROM function_events')]
    source_count, fallback_count, skipped = 0, 0, 0
    for name in names:
        text = get_current_source(name, conn, f'repos/{repo_key}')
        source_type = 'current_code'
        if not text:
            text = build_fallback_text(name, conn)
            source_type = 'historical_metadata'
        if not text:
            skipped += 1
            continue
        embedding = model.encode(text)
        conn.execute('INSERT INTO function_embeddings VALUES (?, ?, ?)',
                      (name, pickle.dumps(embedding), source_type))
        if source_type == 'current_code':
            source_count += 1
        else:
            fallback_count += 1
    conn.commit()
    return source_count, fallback_count, skipped, len(names)


def search(query, conn, top_k=5):
    query_vec = model.encode(query)
    rows = conn.execute('SELECT qualified_name, embedding, source_type FROM function_embeddings').fetchall()
    results = []
    for qname, blob, source_type in rows:
        vec = pickle.loads(blob)
        sim = float(np.dot(query_vec, vec) / (np.linalg.norm(query_vec) * np.linalg.norm(vec)))
        results.append((sim, qname, source_type))
    results.sort(reverse=True)
    return results[:top_k]


if __name__ == '__main__':
    import sys
    repo_key = sys.argv[1]
    conn = sqlite3.connect(f'{repo_key}.db')
    conn.row_factory = sqlite3.Row
    source_count, fallback_count, skipped, total = build_embeddings(repo_key, conn)
    print(f'{total} distinct functions: {source_count} embedded from current code, '
          f'{fallback_count} embedded from historical metadata (fallback), {skipped} skipped entirely')

    print()
    print('Test queries:')
    for q in ['how does authentication work', 'connection pooling and reuse', 'redirect handling']:
        print(f'\nQuery: {q}')
        for sim, name, source_type in search(q, conn):
            print(f'  {sim:.3f}  [{source_type}]  {name}')
