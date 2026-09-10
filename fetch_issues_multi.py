import os, re, sqlite3, sys, time
import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ.get('GITHUB_TOKEN')
pattern = re.compile(r'#(\d+)')


def owner_repo_from_url(url):
    url = url.rstrip('/')
    if url.endswith('.git'):
        url = url[:-4]
    parts = url.split('/')
    return f'{parts[-2]}/{parts[-1]}'


def fetch_issues(db_path, repo_url):
    if not TOKEN:
        raise RuntimeError("GITHUB_TOKEN not found -- check your .env file")
    headers = {'Authorization': f'token {TOKEN}', 'Accept': 'application/vnd.github+json'}
    owner_repo = owner_repo_from_url(repo_url)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS issues (
        number INTEGER PRIMARY KEY, title TEXT, state TEXT, is_pr INTEGER, body TEXT)''')

    rows = conn.execute('SELECT message FROM commits').fetchall()
    numbers = set()
    for row in rows:
        numbers.update(int(n) for n in pattern.findall(row['message']))

    fetched, cached, failed = 0, 0, 0
    for num in sorted(numbers):
        existing = conn.execute('SELECT number FROM issues WHERE number = ?', (num,)).fetchone()
        if existing:
            cached += 1
            continue
        resp = requests.get(f'https://api.github.com/repos/{owner_repo}/issues/{num}', headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            conn.execute('INSERT INTO issues VALUES (?, ?, ?, ?, ?)',
                         (num, data.get('title'), data.get('state'),
                          1 if 'pull_request' in data else 0, (data.get('body') or '')[:2000]))
            fetched += 1
        else:
            failed += 1
        time.sleep(0.3)

    conn.commit()
    return fetched, cached, failed


if __name__ == '__main__':
    db_path = sys.argv[1]
    repo_url = sys.argv[2]
    try:
        fetched, cached, failed = fetch_issues(db_path, repo_url)
        print(f'fetched={fetched} cached={cached} failed={failed}')
    except RuntimeError as e:
        print(f'Error: {e}')
        sys.exit(1)
