import json, os, threading

REGISTRY_PATH = 'repos.json'
_lock = threading.Lock()

def _load():
    if not os.path.exists(REGISTRY_PATH):
        return {}
    with open(REGISTRY_PATH) as f:
        return json.load(f)

def _save(data):
    with open(REGISTRY_PATH, 'w') as f:
        json.dump(data, f, indent=2)

def set_status(repo_key, **kwargs):
    with _lock:
        data = _load()
        data.setdefault(repo_key, {})
        data[repo_key].update(kwargs)
        _save(data)

def get_status(repo_key):
    return _load().get(repo_key)

def list_repos():
    return _load()

def repo_key_from_url(url):
    url = url.rstrip('/')
    if url.endswith('.git'):
        url = url[:-4]
    parts = url.split('/')
    return f'{parts[-2]}_{parts[-1]}'.lower()
