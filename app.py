import subprocess
import sqlite3
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
import cli as core
import repo_registry as registry

app = FastAPI()

_connections = {}


def get_conn(repo_key: str):
    if repo_key not in _connections:
        status = registry.get_status(repo_key)
        if not status or status.get('status') not in ('done',):
            raise HTTPException(status_code=404, detail=f"Repo '{repo_key}' is not ready (not found, or still indexing)")
        conn = sqlite3.connect(status['db_path'], check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('''CREATE TABLE IF NOT EXISTS explanations (
            qualified_name TEXT PRIMARY KEY, explanation TEXT, generated_at TEXT,
            red_flags TEXT, event_mismatches TEXT, hallucinated_issues TEXT)''')
        conn.commit()
        _connections[repo_key] = conn
    return _connections[repo_key]


@app.post("/api/repos")
def api_add_repo(body: dict):
    repo_url = body["repo_url"]
    max_commits = body.get("max_commits", 450)
    repo_key = registry.repo_key_from_url(repo_url)
    existing = registry.get_status(repo_key)
    if existing and existing.get("status") in ("indexing", "cloning", "fetching_issues"):
        return {"repo_key": repo_key, "status": "already_running"}
    subprocess.Popen(["python", "pipeline.py", repo_url, str(max_commits)])
    return {"repo_key": repo_key, "status": "started"}


@app.get("/api/repos")
def api_list_repos():
    return registry.list_repos()


@app.get("/api/repos/{repo_key}/status")
def api_repo_status(repo_key: str):
    status = registry.get_status(repo_key)
    if not status:
        return {"status": "not_found"}
    return status


@app.get("/api/functions")
def api_list_functions(repo: str, search: str = Query(default="")):
    conn = get_conn(repo)
    if search.strip():
        rows = core.search_functions(conn, search.strip())
    else:
        rows = core.list_top_functions(conn)
    return [{"qualified_name": r["qualified_name"], "event_count": r["event_count"]} for r in rows]


@app.get("/api/functions/{name}/lifeline")
def api_lifeline(name: str, repo: str):
    conn = get_conn(repo)
    events = core.get_lifeline(name, conn)
    result = []
    for row in events:
        issue_num = None
        m = core.pattern.search(row["message"])
        if m:
            issue_num = int(m.group(1))
        issue_title = None
        if issue_num:
            issue = conn.execute("SELECT title FROM issues WHERE number = ?", (issue_num,)).fetchone()
            if issue:
                issue_title = issue["title"]
        result.append({
            "hash": row["commit_hash"][:8],
            "date": row["date"][:10],
            "event_type": row["event_type"],
            "old_qualified_name": row["old_qualified_name"],
            "issue_number": issue_num if issue_title else None,
            "issue_title": issue_title,
        })
    return {"qualified_name": name, "total_events": len(events), "events": result}


@app.post("/api/functions/{name}/explain")
def api_explain(name: str, repo: str):
    conn = get_conn(repo)
    explanation, red_flags, event_mismatches, hallucinated_issues, was_cached, generated_at = (
        core.get_cached_or_generate(name, conn, repo)
    )
    total_flags = sum(red_flags.values()) if red_flags else 0
    severity = "severe" if total_flags > 10 else "moderate" if total_flags > 3 else ("minor" if total_flags else None)
    if hallucinated_issues or event_mismatches:
        confidence = "low"
    elif severity == "severe":
        confidence = "low"
    elif severity in ("moderate", "minor"):
        confidence = "medium"
    else:
        confidence = "high"
    return {
        "qualified_name": name,
        "explanation": explanation,
        "was_cached": was_cached,
        "generated_at": generated_at,
        "red_flags": red_flags,
        "red_flag_severity": severity,
        "confidence": confidence,
        "event_mismatches": [
            {"hash": m[0], "claimed_keyword": m[1], "claimed_type": m[2], "true_type": m[3], "sentence": m[4]}
            for m in event_mismatches
        ],
        "hallucinated_issues": sorted(hallucinated_issues),
    }


@app.get("/api/repos/{repo_key}/tree")
def api_tree(repo_key: str):
    conn = get_conn(repo_key)
    rows = conn.execute('''
        SELECT file_path, qualified_name, COUNT(*) as event_count
        FROM function_events
        WHERE file_path IS NOT NULL
        GROUP BY file_path, qualified_name
        ORDER BY file_path, qualified_name
    ''').fetchall()

    tree = {}
    for row in rows:
        parts = row['file_path'].replace('\\', '/').split('/')
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {'__type__': 'dir', '__children__': {}})['__children__']
        filename = parts[-1]
        file_node = node.setdefault(filename, {'__type__': 'file', '__functions__': []})
        file_node['__functions__'].append({'name': row['qualified_name'], 'event_count': row['event_count']})
    return tree


app.mount("/", StaticFiles(directory="static", html=True), name="static")
