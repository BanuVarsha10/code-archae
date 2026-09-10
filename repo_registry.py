import json, os
from filelock import FileLock

REGISTRY_PATH = 'repos.json'
LOCK_PATH = 'repos.json.lock'


def _load():
    if not os.path.exists(REGISTRY_PATH):
        return {}
    with open(REGISTRY_PATH) as f:
        return json.load(f)


def _save(data):
    tmp_path = REGISTRY_PATH + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, REGISTRY_PATH)


def set_status(repo_key, **kwargs):
    with FileLock(LOCK_PATH, timeout=15):
        data = _load()
        data.setdefault(repo_key, {})
        data[repo_key].update(kwargs)
        _save(data)


def get_status(repo_key):
    with FileLock(LOCK_PATH, timeout=15):
        return _load().get(repo_key)


def list_repos():
    with FileLock(LOCK_PATH, timeout=15):
        return _load()


def repo_key_from_url(url):
    url = url.rstrip('/')
    if url.endswith('.git'):
        url = url[:-4]
    parts = url.split('/')
    return f'{parts[-2]}_{parts[-1]}'.lower()
