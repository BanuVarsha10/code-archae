import sqlite3, os, ast, pickle, re
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
        qualified_name TEXT PRIMARY KEY, embedding BLOB, source_type TEXT, content TEXT)''')
    existing_cols = {row[1] for row in conn.execute('PRAGMA table_info(function_embeddings)').fetchall()}
    if 'source_type' not in existing_cols:
        conn.execute('ALTER TABLE function_embeddings ADD COLUMN source_type TEXT')
    if 'content' not in existing_cols:
        conn.execute('ALTER TABLE function_embeddings ADD COLUMN content TEXT')
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
        conn.execute('INSERT INTO function_embeddings (qualified_name, embedding, source_type) VALUES (?, ?, ?)',
                      (name, pickle.dumps(embedding), source_type))
        if source_type == 'current_code':
            source_count += 1
        else:
            fallback_count += 1
    conn.commit()
    return source_count, fallback_count, skipped, len(names)


def chunk_readme(repo_dir):
    readme_names = ['README.md', 'README.rst', 'README.txt', 'readme.md']
    readme_path = None
    for name in readme_names:
        candidate = os.path.join(repo_dir, name)
        if os.path.exists(candidate):
            readme_path = candidate
            break
    if not readme_path:
        return []

    with open(readme_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    content = re.sub(r'<[^>]+>', ' ', content)
    content = re.sub(r'```.*?```', ' ', content, flags=re.DOTALL)

    parts = re.split(r'(?=^## )', content, flags=re.MULTILINE)
    chunks = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith('## '):
            title = part.split('\n', 1)[0][3:].strip()
            body = part.split('\n', 1)[1].strip() if '\n' in part else ''
        else:
            title = 'Overview'
            body = part
        body = re.sub(r'\n{2,}', ' ', body).strip()
        body = re.sub(r'\s{2,}', ' ', body).strip()
        if body:
            chunks.append((title, body))
    return chunks


def build_doc_embeddings(repo_key, conn):
    chunks = chunk_readme(f'repos/{repo_key}')
    count = 0
    for title, body in chunks:
        qualified_name = f'README: {title}'
        embedding = model.encode(body)
        conn.execute(
            'INSERT OR REPLACE INTO function_embeddings VALUES (?, ?, ?, ?)',
            (qualified_name, pickle.dumps(embedding), 'documentation', body)
        )
        count += 1
    conn.commit()
    return count


def extract_module_docstrings(repo_dir):
    docstrings = []
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
                doc = ast.get_docstring(ast.parse(source))
                if doc and len(doc.strip()) > 20:
                    docstrings.append((rel_path, doc.strip()))
            except SyntaxError:
                continue
    return docstrings


def build_structural_summary(repo_dir, conn):
    files = []
    for root, dirs, filenames in os.walk(repo_dir):
        if '.git' in dirs:
            dirs.remove('.git')
        for fname in filenames:
            if fname.endswith('.py'):
                files.append(os.path.relpath(os.path.join(root, fname), repo_dir).replace('\\', '/'))

    most_called = conn.execute('''SELECT resolved_qualified_name, COUNT(DISTINCT caller_qualified_name) c
        FROM call_graph WHERE resolved_qualified_name IS NOT NULL
        GROUP BY resolved_qualified_name ORDER BY c DESC LIMIT 5''').fetchall()

    parts = [f'This project contains {len(files)} Python files, including: {", ".join(sorted(files)[:15])}.']
    if most_called:
        desc = ', '.join(f'{r[0]} (called by {r[1]} other function(s))' for r in most_called)
        parts.append(f'The most central functions, by how many other functions call them, are: {desc}.')

    docs = extract_module_docstrings(repo_dir)
    if docs:
        doc_text = ' '.join(f'{path}: "{d[:150]}"' for path, d in docs[:5])
        parts.append(f'Real module docstrings found in the code: {doc_text}')

    return ' '.join(parts)


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

    doc_count = build_doc_embeddings(repo_key, conn)
    if doc_count > 0:
        print(f'Also embedded {doc_count} README sections as documentation')
    else:
        # No README found -- fall back to a deterministic structural
        # summary instead, clearly labeled as auto-generated so it's
        # never mistaken for human-written project documentation.
        summary = build_structural_summary(f'repos/{repo_key}', conn)
        conn.execute(
            'INSERT OR REPLACE INTO function_embeddings VALUES (?, ?, ?, ?)',
            ('Project Structure (auto-generated -- no README found)',
             pickle.dumps(model.encode(summary)), 'structural_summary', summary)
        )
        conn.commit()
        print('No README found -- embedded an auto-generated structural summary instead')

    print()
    print('Test queries:')
    for q in ['how does authentication work', 'connection pooling and reuse', 'redirect handling']:
        print(f'\nQuery: {q}')
        for sim, name, source_type in search(q, conn):
            print(f'  {sim:.3f}  [{source_type}]  {name}')
