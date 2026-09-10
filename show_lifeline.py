import sqlite3, re

conn = sqlite3.connect('archaeologist.db')
conn.row_factory = sqlite3.Row
pattern = re.compile(r'#(\d+)')

def get_lifeline_with_issues(name, conn):
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

    for row in all_events:
        issue_num = None
        m = pattern.search(row['message'])
        if m:
            issue_num = int(m.group(1))
        issue = None
        if issue_num:
            issue = conn.execute('SELECT title, is_pr FROM issues WHERE number = ?', (issue_num,)).fetchone()
        extra = f" (was {row['old_qualified_name']})" if row['old_qualified_name'] else ''
        issue_str = f"  [PR #{issue_num}: {issue['title']}]" if issue else ''
        print(f"  {row['commit_hash'][:8]}  {row['date'][:10]}  {row['event_type']:10}  {row['qualified_name']}{extra}{issue_str}")

print("Lifeline for 'HTTP11Connection.close', now with linked issues:")
get_lifeline_with_issues('HTTP11Connection.close', conn)
