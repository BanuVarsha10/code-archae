const listEl = document.getElementById('function-list');
const panelLabel = document.getElementById('panel-label');
const detailEl = document.getElementById('detail-panel');
const searchInput = document.getElementById('search');
const repoSelect = document.getElementById('repo-select');
const addRepoBtn = document.getElementById('add-repo-btn');
const modal = document.getElementById('add-repo-modal');
const cancelBtn = document.getElementById('cancel-add-repo');
const confirmBtn = document.getElementById('confirm-add-repo');
const repoUrlInput = document.getElementById('repo-url-input');
const indexingStatus = document.getElementById('indexing-status');

let currentRepo = null;
let currentName = null;
let debounceTimer = null;
let pollTimer = null;

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}

function formatDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

async function loadRepos(selectKey) {
  const res = await fetch('/api/repos');
  const repos = await res.json();
  repoSelect.innerHTML = '';
  const readyKeys = Object.entries(repos).filter(([k, v]) => v.status === 'done');
  readyKeys.forEach(([key, info]) => {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = key.replace('_', '/');
    repoSelect.appendChild(opt);
  });
  if (readyKeys.length === 0) {
    const opt = document.createElement('option');
    opt.textContent = 'No repos indexed yet';
    repoSelect.appendChild(opt);
    return;
  }
  currentRepo = selectKey && repos[selectKey] ? selectKey : readyKeys[0][0];
  repoSelect.value = currentRepo;
  loadList('');
}

repoSelect.addEventListener('change', () => {
  currentRepo = repoSelect.value;
  currentName = null;
  loadList('');
  detailEl.innerHTML = `
    <div class="empty-state">
      <p>Select a function on the left to begin.</p>
      <p class="empty-sub">Each one carries a buried history of renames, rewrites, and the discussions behind them.</p>
    </div>
  `;
});

addRepoBtn.addEventListener('click', () => {
  modal.classList.remove('hidden');
  repoUrlInput.value = '';
  indexingStatus.innerHTML = '';
});

cancelBtn.addEventListener('click', () => {
  modal.classList.add('hidden');
  if (pollTimer) clearInterval(pollTimer);
});

confirmBtn.addEventListener('click', async () => {
  const url = repoUrlInput.value.trim();
  if (!url) return;
  confirmBtn.disabled = true;
  const res = await fetch('/api/repos', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo_url: url })
  });
  const data = await res.json();
  const repoKey = data.repo_key;
  indexingStatus.innerHTML = `<div class="stage-active">Starting…</div>`;
  pollTimer = setInterval(() => pollStatus(repoKey), 2000);
});

async function pollStatus(repoKey) {
  const res = await fetch(`/api/repos/${encodeURIComponent(repoKey)}/status`);
  const status = await res.json();

  const stageOrder = ['cloning', 'indexing', 'fetching_issues', 'done'];
  const stageLabels = {
    cloning: 'Cloning repository…',
    indexing: `Mining commit history… ${status.processed || 0}/${status.total || '?'} commits`,
    fetching_issues: 'Fetching linked GitHub issues…',
    done: 'Done.',
    failed: 'Failed.'
  };

  if (status.status === 'failed') {
    indexingStatus.innerHTML = `<div class="stage-error">Indexing failed: ${escapeHtml(status.error || 'unknown error')}</div>`;
    clearInterval(pollTimer);
    confirmBtn.disabled = false;
    return;
  }

  indexingStatus.innerHTML = stageOrder
    .filter(s => stageOrder.indexOf(s) <= stageOrder.indexOf(status.status))
    .map(s => {
      const cls = s === status.status ? 'stage-active' : 'stage-done';
      return `<div class="${cls}">${stageLabels[s]}</div>`;
    }).join('');

  if (status.status === 'done') {
    clearInterval(pollTimer);
    confirmBtn.disabled = false;
    if (status.issues_error) {
      indexingStatus.innerHTML += `<div class="stage-error">Note: issue linking skipped (${escapeHtml(status.issues_error)})</div>`;
    }
    setTimeout(() => {
      modal.classList.add('hidden');
      loadRepos(repoKey);
    }, 1200);
  }
}

async function loadList(term) {
  if (!currentRepo) return;
  const url = term
    ? `/api/functions?repo=${encodeURIComponent(currentRepo)}&search=${encodeURIComponent(term)}`
    : `/api/functions?repo=${encodeURIComponent(currentRepo)}`;
  const res = await fetch(url);
  const rows = await res.json();
  panelLabel.textContent = term ? `Matches for "${term}"` : 'Most excavated';
  listEl.innerHTML = '';
  rows.forEach(row => {
    const li = document.createElement('li');
    li.className = 'function-item' + (row.qualified_name === currentName ? ' active' : '');
    li.tabIndex = 0;
    li.innerHTML = `
      <span class="function-name">${escapeHtml(row.qualified_name)}</span>
      <span class="function-count">${row.event_count} layers unearthed</span>
    `;
    li.addEventListener('click', () => selectFunction(row.qualified_name));
    li.addEventListener('keydown', e => { if (e.key === 'Enter') selectFunction(row.qualified_name); });
    listEl.appendChild(li);
  });
}

async function selectFunction(name) {
  currentName = name;
  document.querySelectorAll('.function-item').forEach(el => {
    el.classList.toggle('active', el.querySelector('.function-name').textContent === name);
  });

  const res = await fetch(`/api/functions/${encodeURIComponent(name)}/lifeline?repo=${encodeURIComponent(currentRepo)}`);
  const data = await res.json();

  const timelineHtml = data.events.map(ev => {
    let issueOrRename = '';
    if (ev.issue_title) {
      issueOrRename = `<span class="layer-issue"><span class="num">#${ev.issue_number}</span>${escapeHtml(ev.issue_title)}</span>`;
    } else if (ev.old_qualified_name) {
      issueOrRename = `<span class="layer-issue">was <em>${escapeHtml(ev.old_qualified_name)}</em></span>`;
    }
    return `
      <div class="layer type-${ev.event_type}">
        <span class="layer-date">${ev.date}</span>
        <span class="layer-hash">${ev.hash}</span>
        <span class="layer-type">${ev.event_type}</span>
        ${issueOrRename}
      </div>
    `;
  }).join('');

  detailEl.innerHTML = `
    <div class="detail-header">
      <h1 class="detail-title">${escapeHtml(name)}</h1>
      <p class="detail-subtitle">${data.total_events} tracked events across this function's history</p>
    </div>
    <div class="timeline">${timelineHtml}</div>
    <button class="reveal-btn" id="reveal-btn">Reveal the story</button>
    <div id="explanation-slot"></div>
  `;

  document.getElementById('reveal-btn').addEventListener('click', () => explainFunction(name));
}

async function explainFunction(name) {
  const btn = document.getElementById('reveal-btn');
  const slot = document.getElementById('explanation-slot');
  btn.disabled = true;
  btn.textContent = 'Excavating…';
  slot.innerHTML = '';

  try {
    const res = await fetch(`/api/functions/${encodeURIComponent(name)}/explain?repo=${encodeURIComponent(currentRepo)}`, { method: 'POST' });
    const data = await res.json();

    let groundingHtml = '';
    const hasIssues = data.red_flag_severity || data.event_mismatches.length || data.hallucinated_issues.length;
    if (hasIssues) {
      const parts = [];
      if (data.red_flag_severity) {
        parts.push(`<div class="grounding-flag">${data.red_flag_severity} hedge language detected: ${Object.entries(data.red_flags).map(([p, c]) => `"${p}" ×${c}`).join(', ')}</div>`);
      }
      if (data.event_mismatches.length) {
        parts.push(`<div class="grounding-flag">${data.event_mismatches.length} event-type mismatch(es) found</div>`);
      }
      if (data.hallucinated_issues.length) {
        parts.push(`<div class="grounding-flag">Unverified issue citation(s): ${data.hallucinated_issues.map(n => '#' + n).join(', ')}</div>`);
      }
      groundingHtml = `<div class="grounding-notes">${parts.join('')}</div>`;
    } else {
      groundingHtml = `<div class="grounding-notes"><div class="grounding-clean">Passed all grounding checks — no hedge language, no event-type mismatches, no unverified citations.</div></div>`;
    }

    slot.innerHTML = `
      <div class="explanation-card">
        <div class="explanation-label">${data.was_cached ? 'Recovered from a previous dig · ' + formatDate(data.generated_at) : 'Freshly excavated'} · confidence: ${data.confidence.toUpperCase()}</div>
        <div class="explanation-text">${escapeHtml(data.explanation)}</div>
        ${groundingHtml}
      </div>
    `;
  } catch (err) {
    slot.innerHTML = `<p class="digging-note">Something went wrong reaching the dig site. Is Ollama running?</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Reveal the story';
  }
}

searchInput.addEventListener('input', () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => loadList(searchInput.value.trim()), 200);
});

loadRepos();

const treeViewEl = document.getElementById('tree-view');
const panelTabs = document.querySelectorAll('.panel-tab');
let currentView = 'ranked';
let treeCache = null;

panelTabs.forEach(tab => {
  tab.addEventListener('click', () => {
    panelTabs.forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    currentView = tab.dataset.view;
    searchInput.value = '';
    if (currentView === 'tree') {
      listEl.classList.add('hidden');
      treeViewEl.classList.remove('hidden');
      panelLabel.textContent = '';
      loadTree();
    } else {
      treeViewEl.classList.add('hidden');
      listEl.classList.remove('hidden');
      loadList('');
    }
  });
});

async function loadTree() {
  if (!currentRepo) return;
  if (!treeCache) {
    const res = await fetch(`/api/repos/${encodeURIComponent(currentRepo)}/tree`);
    treeCache = await res.json();
  }
  treeViewEl.innerHTML = '';
  treeViewEl.appendChild(renderTreeLevel(treeCache));
}

function renderTreeLevel(node) {
  const container = document.createElement('div');
  const entries = Object.entries(node).sort((a, b) => {
    const aIsDir = a[1].__type__ === 'dir';
    const bIsDir = b[1].__type__ === 'dir';
    if (aIsDir !== bIsDir) return aIsDir ? -1 : 1;
    return a[0].localeCompare(b[0]);
  });

  entries.forEach(([name, value]) => {
    const nodeEl = document.createElement('div');
    nodeEl.className = 'tree-node';

    if (value.__type__ === 'dir') {
      const row = document.createElement('div');
      row.className = 'tree-row';
      row.innerHTML = `<span class="tree-caret">▸</span><span class="tree-dir-name">${escapeHtml(name)}</span>`;
      const childrenEl = document.createElement('div');
      childrenEl.className = 'tree-children';
      childrenEl.appendChild(renderTreeLevel(value.__children__));
      row.addEventListener('click', () => {
        row.querySelector('.tree-caret').classList.toggle('open');
        childrenEl.classList.toggle('open');
      });
      nodeEl.appendChild(row);
      nodeEl.appendChild(childrenEl);
    } else {
      const row = document.createElement('div');
      row.className = 'tree-row';
      row.innerHTML = `<span class="tree-caret">▸</span><span class="tree-file-name">${escapeHtml(name)}</span>`;
      const childrenEl = document.createElement('div');
      childrenEl.className = 'tree-children';
      value.__functions__.forEach(fn => {
        const fnRow = document.createElement('div');
        fnRow.className = 'tree-function-row';
        fnRow.textContent = fn.name;
        const countSpan = document.createElement('span');
        countSpan.className = 'tree-function-count';
        countSpan.textContent = `${fn.event_count}`;
        fnRow.appendChild(countSpan);
        fnRow.addEventListener('click', (e) => {
          e.stopPropagation();
          document.querySelectorAll('.tree-function-row').forEach(el => el.classList.remove('active'));
          fnRow.classList.add('active');
          selectFunction(fn.name);
        });
        childrenEl.appendChild(fnRow);
      });
      row.addEventListener('click', () => {
        row.querySelector('.tree-caret').classList.toggle('open');
        childrenEl.classList.toggle('open');
      });
      nodeEl.appendChild(row);
      nodeEl.appendChild(childrenEl);
    }
    container.appendChild(nodeEl);
  });

  return container;
}

repoSelect.addEventListener('change', () => { treeCache = null; });
