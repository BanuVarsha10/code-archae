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
    const hasIssues = data.red_flag_severity || data.event_mismatches.length || data.hallucinated_issues.length || (data.connections_overclaims && data.connections_overclaims.length);
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
      if (data.connections_overclaims && data.connections_overclaims.length) {
        parts.push(`<div class="grounding-flag">Overclaimed connection(s) detected: ${data.connections_overclaims.join(', ')}</div>`);
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
const askView = document.getElementById('ask-view');
const panelTabs = document.querySelectorAll('.panel-tab');
let currentView = 'ranked';
let treeCache = null;

panelTabs.forEach(tab => {
  tab.addEventListener('click', () => {
    panelTabs.forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    currentView = tab.dataset.view;
    searchInput.value = '';

    listEl.classList.add('hidden');
    treeViewEl.classList.add('hidden');
    askView.classList.add('hidden');

    if (currentView === 'tree') {
      treeViewEl.classList.remove('hidden');
      panelLabel.textContent = '';
      loadTree();
    } else if (currentView === 'ask') {
      askView.classList.remove('hidden');
      panelLabel.textContent = '';
    } else {
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

const askInput = document.getElementById('ask-input');
const askSubmitBtn = document.getElementById('ask-submit-btn');

function renderAnswerMarkdown(text) {
  const escapeHtmlLocal = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const applyInline = (s) => escapeHtmlLocal(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>');

  const blocks = text.split(/\n\n+/);
  const htmlParts = [];
  blocks.forEach(block => {
    let trimmed = block.trim();
    if (!trimmed) return;
    if (trimmed.startsWith('---')) {
      htmlParts.push('<hr class="ask-divider">');
      trimmed = trimmed.replace(/^---\s*\n?/, '').trim();
      if (!trimmed) return;
    }
    if (trimmed.startsWith('### ')) {
      const lines = trimmed.split('\n');
      const headerText = lines[0].slice(4);
      const rest = lines.slice(1).join('\n').trim();
      htmlParts.push(`<h4 class="ask-section-header">${applyInline(headerText)}</h4>`);
      if (rest) htmlParts.push(`<p>${applyInline(rest).replace(/\n/g, '<br>')}</p>`);
      return;
    }
    htmlParts.push(`<p>${applyInline(trimmed).replace(/\n/g, '<br>')}</p>`);
  });
  return htmlParts.join('');
}

async function submitAskQuestion() {
  const query = askInput.value.trim();
  if (!query || !currentRepo) return;
  askSubmitBtn.disabled = true;
  askSubmitBtn.textContent = 'Digging...';

  detailEl.innerHTML = `<div class="empty-state"><p>Searching and synthesizing an answer...</p></div>`;

  try {
    const res = await fetch(`/api/ask?repo=${encodeURIComponent(currentRepo)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query })
    });
    const data = await res.json();

    const retrievedHtml = data.checks.retrieved_functions.map(([name, sim, type]) =>
      `<div class="retrieved-item"><span class="name">${escapeHtml(name)}</span><span class="meta">${sim.toFixed(2)} · ${type}</span></div>`
    ).join('');

    let groundingParts = [];
    if (data.checks.hallucinated_hashes.length) groundingParts.push(`<div class="grounding-flag">Unverified hash citation(s): ${data.checks.hallucinated_hashes.join(', ')}</div>`);
    if (data.checks.hallucinated_issues.length) groundingParts.push(`<div class="grounding-flag">Unverified issue citation(s): ${data.checks.hallucinated_issues.map(n => '#' + n).join(', ')}</div>`);
    if (Object.keys(data.checks.red_flags).length) groundingParts.push(`<div class="grounding-flag">Hedge language detected: ${Object.entries(data.checks.red_flags).map(([p, c]) => `"${p}" ×${c}`).join(', ')}</div>`);
    if (data.checks.structure_checked === false) groundingParts.push(`<div class="grounding-flag">Could not verify cross-function attribution (model did not use the expected section format)</div>`);
    if (data.checks.misattributions && data.checks.misattributions.length) groundingParts.push(`<div class="grounding-flag">${data.checks.misattributions.length} cross-function misattribution(s) detected</div>`);
    if (data.checks.unrecognized_headers && data.checks.unrecognized_headers.length) groundingParts.push(`<div class="grounding-flag">Unrecognized section header(s): ${data.checks.unrecognized_headers.join(', ')}</div>`);
    if (data.checks.retrieval_overreach && data.checks.retrieval_overreach.length) groundingParts.push(`<div class="grounding-flag">Possible overreach detected (secondary signal): ${data.checks.retrieval_overreach.length} instance(s)</div>`);

    const groundingHtml = groundingParts.length
      ? `<div class="grounding-notes">${groundingParts.join('')}</div>`
      : `<div class="grounding-notes"><div class="grounding-clean">Passed all grounding checks.</div></div>`;

    detailEl.innerHTML = `
      <div class="detail-header">
        <h1 class="detail-title">Ask: "${escapeHtml(query)}"</h1>
      </div>
      <div class="retrieved-list">
        <div class="retrieved-list-title">Retrieved functions</div>
        ${retrievedHtml}
      </div>
      <div class="explanation-card">
        <div class="ask-answer">${renderAnswerMarkdown(data.answer)}</div>
        ${groundingHtml}
      </div>
    `;
  } catch (err) {
    detailEl.innerHTML = `<p class="digging-note">Something went wrong reaching the dig site. Is Ollama running?</p>`;
  } finally {
    askSubmitBtn.disabled = false;
    askSubmitBtn.textContent = 'Ask';
  }
}

askSubmitBtn.addEventListener('click', submitAskQuestion);
askInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submitAskQuestion();
});
