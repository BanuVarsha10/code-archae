import sqlite3, re, sys, datetime
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

def format_lifeline_context(name, events, conn):
    lines = [f'Function: {name}', f'Total tracked events: {len(events)}', '']
    valid_hashes = set()
    for row in events:
        valid_hashes.add(row['commit_hash'][:8])
        issue_num = None
        m = pattern.search(row['message'])
        if m: issue_num = int(m.group(1))
        issue_title = None
        if issue_num:
            issue = conn.execute('SELECT title FROM issues WHERE number = ?', (issue_num,)).fetchone()
            if issue: issue_title = issue['title']
        line = f"- {row['date'][:10]} ({row['commit_hash'][:8]}): {row['event_type']}"
        if row['old_qualified_name']:
            line += f" (renamed from {row['old_qualified_name']})"
        if issue_title:
            line += f' -- PR #{issue_num}: "{issue_title}"'
        lines.append(line)
    return '\n'.join(lines), valid_hashes

def scan_red_flags(explanation):
    lowered = explanation.lower()
    return [p for p in RED_FLAG_PHRASES if p in lowered]

def check_event_type_consistency(explanation, events):
    event_type_by_hash = {row['commit_hash'][:8]: row['event_type'] for row in events}
    keyword_to_type = {
        'added': 'added', 'created': 'added',
        'modified': 'modified', 'modification': 'modified', 'changed': 'modified', 'updated': 'modified',
        'deleted': 'deleted', 'deletion': 'deleted', 'removed': 'deleted',
        'renamed': 'renamed', 'rename': 'renamed',
    }
    sentences = re.split(r'(?<=[.!?])\s+', explanation)
    mismatches = []
    for sentence in sentences:
        hashes_in_sentence = re.findall(r'\b[0-9a-f]{6,8}\b', sentence)
        for h in hashes_in_sentence:
            true_type = event_type_by_hash.get(h) or event_type_by_hash.get(h.zfill(8))
            if not true_type:
                continue
            for keyword, claimed_type in keyword_to_type.items():
                if keyword in sentence.lower() and claimed_type != true_type:
                    mismatches.append((h, keyword, claimed_type, true_type, sentence.strip()))
    return mismatches

def generate_explanation(name, conn):
    events = get_lifeline(name, conn)
    if not events:
        return f"No tracked history found for '{name}'.", [], []
    context, valid_hashes = format_lifeline_context(name, events, conn)
    system_prompt = (
        "You explain why a function exists and how it evolved, using ONLY the "
        "structured history provided below. Do NOT use words like 'likely', "
        "'probably', 'this suggests', 'aimed to', or any language implying "
        "motivation or purpose that is not explicitly stated in a PR title. "
        "If a PR title does not explain why a change was made, say plainly "
        "that the reason is not captured in the available data, rather than "
        "guessing. Cite specific commit hashes (the 8-character codes) for "
        "claims where relevant. If the history is sparse, keep your "
        "explanation proportionately short rather than padding it. Do not "
        "invent commit hashes that are not in the data."
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
    event_mismatches = check_event_type_consistency(explanation, events)
    return explanation, red_flags, event_mismatches

def get_cached_or_generate(name, conn):
    cached = conn.execute('SELECT explanation, generated_at FROM explanations WHERE qualified_name = ?', (name,)).fetchone()
    if cached:
        return cached['explanation'], [], [], True, cached['generated_at']
    explanation, red_flags, event_mismatches = generate_explanation(name, conn)
    now = datetime.datetime.now().isoformat(timespec='seconds')
    conn.execute('INSERT OR REPLACE INTO explanations VALUES (?, ?, ?)', (name, explanation, now))
    conn.commit()
    return explanation, red_flags, event_mismatches, False, now

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
        explanation, red_flags, event_mismatches, was_cached, generated_at = get_cached_or_generate(name, conn)
        if was_cached:
            print(f"(cached, generated {generated_at})\n")
        else:
            print()
        print(explanation)
        if not was_cached:
            if red_flags:
                print(f"\n[red-flag phrases detected: {red_flags}]")
            if event_mismatches:
                print(f"\n[event-type mismatches detected: {len(event_mismatches)}]")
        print()

if __name__ == '__main__':
    main()
