import sqlite3, re, sys, os, datetime, json, ast
import ollama

conn = sqlite3.connect('archaeologist.db')
conn.row_factory = sqlite3.Row
pattern = re.compile(r'#(\d+)')

conn.execute('''CREATE TABLE IF NOT EXISTS explanations (
    qualified_name TEXT PRIMARY KEY, explanation TEXT, generated_at TEXT)''')
conn.commit()

RED_FLAG_PHRASES = [
    'in response to', 'aimed to', 'in order to', 'to address',
    'likely', 'probably', 'improved', 'enhanced', 'better',
    'because', 'so that', 'to fix', 'to support', 'to ensure',
    'which allowed', 'enabling', 'this suggests', 'suggest that',
]

def get_lifeline(name, conn):
    names_to_check = [name]; seen_names = set(); all_events = []
    while names_to_check:
        current = names_to_check.pop()
        if current in seen_names: continue
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

def format_lifeline_context(name, events, conn, max_full_events=15):
    lines = [f'Function: {name}', f'Total tracked events: {len(events)}', '']
    valid_hashes = set()
    valid_issue_numbers = set()

    def format_event_line(row):
        valid_hashes.add(row['commit_hash'][:8])
        issue_num = None
        m = pattern.search(row['message'])
        if m: issue_num = int(m.group(1))
        issue_title = None
        if issue_num:
            issue = conn.execute('SELECT title FROM issues WHERE number = ?', (issue_num,)).fetchone()
            if issue:
                issue_title = issue['title']
                valid_issue_numbers.add(issue_num)
        line = f"- {row['date'][:10]} ({row['commit_hash'][:8]}): {row['event_type']}"
        if row['old_qualified_name']:
            line += f" (renamed from {row['old_qualified_name']})"
        if issue_title:
            line += f' -- PR #{issue_num}: "{issue_title}"'
        return line

    if len(events) <= max_full_events:
        for row in events:
            lines.append(format_event_line(row))
    else:
        renames = [e for e in events if e['event_type'] == 'renamed']
        candidates = list(events[:5]) + renames + list(events[-5:])
        seen_hashes = set()
        important = []
        for e in candidates:
            if e['commit_hash'] not in seen_hashes:
                important.append(e)
                seen_hashes.add(e['commit_hash'])
        important.sort(key=lambda r: r['date'])
        omitted = len(events) - len(important)
        lines.append(
            f"(Showing {len(important)} of {len(events)} tracked events: the "
            f"earliest and most recent activity, plus any renames. "
            f"{omitted} additional modifications exist but are omitted here "
            f"for brevity -- do NOT speculate about what they contained or "
            f"why they happened, and do not claim the shown events are the "
            f"only changes that occurred.)"
        )
        lines.append("")
        for row in important:
            lines.append(format_event_line(row))

    return '\n'.join(lines), valid_hashes, valid_issue_numbers

def scan_red_flags(explanation):
    lowered = explanation.lower()
    counts = {}
    for phrase in RED_FLAG_PHRASES:
        c = lowered.count(phrase)
        if c > 0:
            counts[phrase] = c
    return counts

def check_event_type_consistency(explanation, events):
    event_type_by_hash = {row['commit_hash'][:8]: row['event_type'] for row in events}
    keyword_to_type = {
        'added': 'added', 'addition': 'added', 'created': 'added', 'creation': 'added',
        'modified': 'modified', 'modification': 'modified', 'changed': 'modified', 'updated': 'modified',
        'deleted': 'deleted', 'deletion': 'deleted', 'removed': 'deleted', 'removal': 'deleted',
        'renamed': 'renamed', 'rename': 'renamed', 'renaming': 'renamed',
    }
    sentences = re.split(r'(?<=[.!?])\s+', explanation)
    mismatches = []
    for sentence in sentences:
        # Split further into clauses so a keyword in one clause of a sentence
        # doesn't get cross-checked against a hash that only appears in a
        # different clause of the same sentence -- this was the exact real
        # bug that let a fabricated claim about e9c0f742 slip through.
        clauses = re.split(r',\s*(?:and|but)\s+|;\s*', sentence)
        for clause in clauses:
            clause_for_keywords = re.sub(r'"[^"]*"', '', clause)
            hashes_in_clause = re.findall(r'\b[0-9a-f]{6,8}\b', clause)
            for h in hashes_in_clause:
                true_type = event_type_by_hash.get(h) or event_type_by_hash.get(h.zfill(8))
                if not true_type:
                    continue
                for keyword, claimed_type in keyword_to_type.items():
                    if keyword in clause_for_keywords.lower() and claimed_type != true_type:
                        mismatches.append((h, keyword, claimed_type, true_type, clause.strip()))
    return mismatches

def get_current_source(repo_key, qualified_name, conn):
    row = conn.execute('''
        SELECT file_path FROM function_events fe
        JOIN commits c ON fe.commit_hash = c.hash
        WHERE qualified_name = ? AND event_type != 'deleted'
        ORDER BY c.date DESC LIMIT 1
    ''', (qualified_name,)).fetchone()
    if not row:
        return None, None
    file_path = row['file_path']
    full_path = os.path.join('repos', repo_key, file_path)
    if not os.path.exists(full_path):
        return None, file_path
    with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
        source = f.read()

    class SourceFF(ast.NodeVisitor):
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
        return None, file_path
    ff = SourceFF(source); ff.visit(tree)
    return ff.functions.get(qualified_name), file_path

def generate_explanation(name, conn, repo_key, model='llama3.2:3b'):
    events = get_lifeline(name, conn)
    if not events:
        return f"No tracked history found for '{name}'.", [], [], set()
    context, valid_hashes, valid_issue_numbers = format_lifeline_context(name, events, conn)
    current_source, current_file = get_current_source(repo_key, name, conn)
    if current_source:
        context += f"\n\nCurrent source code (in {current_file}):\n```python\n{current_source}\n```"
    system_prompt = (
        "You have two separate tasks -- keep them clearly apart in your answer.\n\n"
        "TASK 1 -- WHAT IT DOES: given the function's current source code "
        "(provided below, if available), explain what it does mechanically. "
        "Walk through the actual logic, grounded strictly in the literal code "
        "shown. Do NOT speculate about how other parts of the codebase call "
        "or depend on this function -- that information has not been "
        "provided to you, so only describe what is visible in the code "
        "itself.\n\n"
        "TASK 2 -- HISTORY: explain why this function exists and how it "
        "evolved, using ONLY the structured history provided below. Do NOT "
        "use words like 'likely', 'probably', 'this suggests', 'aimed to', "
        "or any language implying motivation not explicitly stated in a PR "
        "title. If a PR title does not explain why a change was made, say "
        "plainly that the reason is not captured, rather than guessing. "
        "Cite specific commit hashes for claims where relevant. Do not "
        "invent commit hashes that are not in the data.\n\n"
        "Structure your response as two sections: 'What it does' and "
        "'History'."
    )
    response = ollama.chat(
        model='llama3.2:3b',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': f"Explain the history of this function:\n\n{context}"}
        ]
    )
    explanation = response['message']['content']
    red_flags = scan_red_flags(explanation)
    cited_issues = set(int(n) for n in re.findall(r'#(\d+)', explanation))
    hallucinated_issues = cited_issues - valid_issue_numbers
    event_mismatches = check_event_type_consistency(explanation, events)
    return explanation, red_flags, event_mismatches, hallucinated_issues

def get_cached_or_generate(name, conn, repo_key):
    existing_cols = {row['name'] for row in conn.execute('PRAGMA table_info(explanations)').fetchall()}
    for col in ['red_flags', 'event_mismatches', 'hallucinated_issues']:
        if col not in existing_cols:
            conn.execute(f'ALTER TABLE explanations ADD COLUMN {col} TEXT')
    conn.commit()

    cached = conn.execute(
        'SELECT explanation, generated_at, red_flags, event_mismatches, hallucinated_issues FROM explanations WHERE qualified_name = ?',
        (name,)
    ).fetchone()
    if cached and cached['red_flags'] is not None:
        red_flags = json.loads(cached['red_flags'])
        event_mismatches = json.loads(cached['event_mismatches'])
        hallucinated_issues = set(json.loads(cached['hallucinated_issues']))
        return cached['explanation'], red_flags, event_mismatches, hallucinated_issues, True, cached['generated_at']

    explanation, red_flags, event_mismatches, hallucinated_issues = generate_explanation(name, conn, repo_key)
    now = datetime.datetime.now().isoformat(timespec='seconds')
    conn.execute(
        '''INSERT OR REPLACE INTO explanations
           (qualified_name, explanation, generated_at, red_flags, event_mismatches, hallucinated_issues)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (name, explanation, now, json.dumps(red_flags), json.dumps(event_mismatches), json.dumps(list(hallucinated_issues)))
    )
    conn.commit()
    return explanation, red_flags, event_mismatches, hallucinated_issues, False, now

def list_top_functions(conn, limit=20):
    return conn.execute('''SELECT qualified_name, COUNT(*) as event_count
                            FROM function_events GROUP BY qualified_name
                            ORDER BY event_count DESC LIMIT ?''', (limit,)).fetchall()

def search_functions(conn, term, limit=20):
    return conn.execute('''SELECT qualified_name, COUNT(*) as event_count
                            FROM function_events WHERE qualified_name LIKE ?
                            GROUP BY qualified_name ORDER BY event_count DESC LIMIT ?''',
                         (f'%{term}%', limit)).fetchall()

def print_function_list(rows):
    for i, row in enumerate(rows, 1):
        print(f"  {i}. {row['qualified_name']}  ({row['event_count']} tracked events)")

def main():
    print("=== Code Archaeologist ===")
    total = conn.execute('SELECT COUNT(DISTINCT qualified_name) FROM function_events').fetchone()[0]
    print(f"{total} functions tracked across httpx's history.\n")
    current_list = list_top_functions(conn)
    print("Top 20 most historically active functions:\n")
    print_function_list(current_list)
    print("\nType a number to explain that function, a search term to filter,")
    print("'list' to see the top 20 again, or 'quit' to exit.\n")

    while True:
        user_input = input("> ").strip()
        if not user_input:
            continue
        if user_input.lower() in ('quit', 'exit', 'q'):
            print("Goodbye.")
            break
        if user_input.lower() == 'list':
            current_list = list_top_functions(conn)
            print_function_list(current_list)
            continue

        if user_input.isdigit():
            idx = int(user_input) - 1
            if 0 <= idx < len(current_list):
                name = current_list[idx]['qualified_name']
            else:
                print("Invalid number, try again.")
                continue
        else:
            matches = search_functions(conn, user_input)
            if not matches:
                print(f"No functions matching '{user_input}'. Try another search.")
                continue
            if len(matches) > 1:
                print(f"Multiple matches for '{user_input}':")
                current_list = matches
                print_function_list(current_list)
                print("Type a number to pick one, or refine your search.")
                continue
            name = matches[0]['qualified_name']

        print(f"\nLooking up {name}...")
        explanation, red_flags, event_mismatches, hallucinated_issues, was_cached, generated_at = get_cached_or_generate(name, conn, 'encode_httpx')
        if was_cached:
            print(f"(cached, generated {generated_at})\n")
        else:
            print()
        print(explanation)
        if not was_cached:
            if red_flags:
                total_flags = sum(red_flags.values())
                severity = "SEVERE" if total_flags > 10 else "moderate" if total_flags > 3 else "minor"
                print(f"\n[{severity} - red-flag phrases detected (with counts): {red_flags}]")
            if event_mismatches:
                print(f"\n[event-type mismatches detected: {len(event_mismatches)}]")
            if hallucinated_issues:
                print(f"\n[hallucinated issue/PR citations detected: {hallucinated_issues}]")
        print()

if __name__ == '__main__':
    main()
