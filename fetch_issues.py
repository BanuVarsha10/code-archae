import os, re, sqlite3, time
import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ.get('GITHUB_TOKEN')
if not TOKEN:
    raise SystemExit("GITHUB_TOKEN not found — check your .env file")

HEADERS = {'Authorization': f'token {TOKEN}', 'Accept': 'application/vnd.github+json'}
REPO = 'encode/httpx'

conn = sqlite3.connect('archaeologist.db')
conn.row_factory = sqlite3.Row
conn.execute('''CREATE TABLE IF NOT EXISTS issues (
    number INTEGER PRIMARY KEY, title TEXT, state TEXT, is_pr INTEGER, body TEXT)''')

pattern = re.compile(r'#(\d+)')
rows = conn.execute('SELECT message FROM commits').fetchall()
numbers = set()
for row in rows:
    numbers.update(int(n) for n in pattern.findall(row['message']))

print(f'Found {len(numbers)} unique issue/PR numbers referenced across all commit messages')

fetched, cached, failed = 0, 0, 0
for num in sorted(numbers):
    existing = conn.execute('SELECT number FROM issues WHERE number = ?', (num,)).fetchone()
    if existing:
        cached += 1
        continue
    resp = requests.get(f'https://api.github.com/repos/{REPO}/issues/{num}', headers=HEADERS)
    remaining = resp.headers.get('x-ratelimit-remaining', '?')
    if resp.status_code == 200:
        data = resp.json()
        conn.execute('INSERT INTO issues VALUES (?, ?, ?, ?, ?)',
                      (num, data.get('title'), data.get('state'),
                       1 if 'pull_request' in data else 0, (data.get('body') or '')[:2000]))
        fetched += 1
        print(f'#{num}: {data.get("title")}  (rate limit remaining: {remaining})')
    else:
        failed += 1
        print(f'#{num}: FAILED ({resp.status_code}, remaining: {remaining})')
    time.sleep(0.3)

conn.commit()
print(f'--- fetched {fetched}, already cached {cached}, failed {failed} ---')
n_issues = conn.execute('SELECT COUNT(*) FROM issues').fetchone()[0]
print(f'Total issues/PRs now stored: {n_issues}')
