import sqlite3, re, sys
import ollama

conn = sqlite3.connect('archaeologist.db')
conn.row_factory = sqlite3.Row
pattern = re.compile(r'#(\d+)')

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

def explain_function(name, conn):
    events = get_lifeline(name, conn)
    if not events:
        return f"No tracked history found for '{name}'.", set(), set(), [], []
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

    candidates = set(re.findall(r'\b[0-9a-f]{6,8}\b', explanation))
    malformed_but_real = set()
    hallucinated = set()
    for token in candidates:
        if token in valid_hashes:
            continue  # exact match, fine
        elif token.zfill(8) in valid_hashes:
            malformed_but_real.add(token)  # e.g. leading zero dropped
        elif any(h.startswith(token) for h in valid_hashes):
            continue  # valid truncated reference, fine
        else:
            hallucinated.add(token)  # doesn't correspond to anything real

    RED_FLAG_PHRASES = [
        'in response to', 'aimed to', 'in order to', 'to address',
        'likely', 'probably', 'improved', 'enhanced', 'better',
        'because', 'so that', 'to fix', 'to support', 'to ensure',
        'which allowed', 'enabling', 'this suggests', 'suggest that',
    ]

    def scan_red_flags(explanation):
        found = []
        lowered = explanation.lower()
        for phrase in RED_FLAG_PHRASES:
            if phrase in lowered:
                found.append(phrase)
        return found

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

    red_flags = scan_red_flags(explanation)
    event_mismatches = check_event_type_consistency(explanation, events)
    judge_verdict = None  # no LLM judge call anymore

    return explanation, hallucinated, malformed_but_real, red_flags, event_mismatches

if __name__ == '__main__':
    name = sys.argv[1] if len(sys.argv) > 1 else 'HTTP11Connection.close'
    explanation, hallucinated, malformed_but_real, red_flags, event_mismatches = explain_function(name, conn)
    print("=== EXPLANATION ===")
    print(explanation)
    print()
    print("=== HASH CITATION CHECK ===")
    if hallucinated:
        print("FAILED - unverified hashes:", hallucinated)
    elif malformed_but_real:
        print("PASSED (with malformed-but-real citations, e.g. dropped leading zero):", malformed_but_real)
    else:
        print("PASSED")
    print()
    print("=== RED-FLAG PHRASE SCAN ===")
    print("PASSED (no flagged phrases)" if not red_flags else f"FLAGGED: {red_flags}")
    print()
    print("=== EVENT-TYPE CONSISTENCY CHECK ===")
    if event_mismatches:
        for h, keyword, claimed, true, sentence in event_mismatches:
            print(f"MISMATCH: '{h}' described as '{keyword}' (implies {claimed}) but real event_type is '{true}'")
            print(f"  in sentence: {sentence}")
    else:
        print("PASSED (no event-type mismatches found)")
