In the Code_archae folder, create a file called CLAUDE.md with this exact content:

# Code Archaeologist

## What this project does
Reconstructs *why* code exists by mining git history and mapping each function's
identity across renames/refactors, so an LLM can later explain a function's
history grounded in real commit citations — not guesses. Target repo for all
development so far: httpx (github.com/encode/httpx), cloned locally into
Code_archae/httpx.

## Tech decisions already made, and why (don't relitigate these without a real reason)
- **PyDriller** (not raw `git log` parsing, not bare GitPython) for walking commit
  history. It already exposes `modified_files[i].source_code` and
  `.source_code_before` directly — no separate git-content-fetching needed.
- **Python's `ast` module** (not tree-sitter) for finding function boundaries.
  Exact for valid Python, zero dependency. Known limitation: only works on
  syntactically valid Python, only Python. tree-sitter is the documented
  upgrade path if multi-language support is ever needed.
- **Qualified names** (`ClassName.method_name`, tracked via a NodeVisitor that
  pushes/pops class context) instead of bare function names, to disambiguate
  same-named methods on different classes. Known open gap: `@property`
  getter/setter pairs still collide under one qualified name.
- **difflib.SequenceMatcher + scipy.optimize.linear_sum_assignment (Hungarian
  algorithm)** for matching functions across commits — NOT greedy matching.
  We proved with real data (ConnectionSemaphore.acquire/release in httpx
  history) that greedy assignment produces false positives that optimal
  assignment avoids. Similarity threshold: 0.75. Minimum function body length
  to consider: 80 chars (below that, similarity scores are unreliable).
- **SQLite**, not Postgres, since this is a local single-user tool with no
  need for a server. Schema is an **append-only event log**
  (`function_events` table: commit_hash, file_path, qualified_name,
  event_type [added/modified/renamed/deleted], old_qualified_name,
  similarity) rather than a mutable "current state" table — lets us replay
  the whole log if matching logic improves, instead of needing data
  migrations.
- Use `conn.row_factory = sqlite3.Row` for all SQLite access — NOT raw tuple
  indexing. We hit a real bug from positional indexing (row[3] vs row[4]
  confusion) that named access would have caught immediately.

## Known performance characteristic
Per-commit processing time grows over the course of history (measured: ~0.5s
early on, 3-4s by commit ~180) because the similarity-matrix computation is
O(n×m) in functions-per-file, and files accumulate more functions as the
codebase matures. Currently capped at 150 commits per run for reliability.
Scaling fix if needed later: incremental indexing (track last-processed
commit hash, only process new ones on subsequent runs) rather than
re-walking full history each time.

## Files built so far
- explore.py — first PyDriller proof-of-concept, walks commits, prints hash/author/date/message
- explore_ast.py — ast-based function extraction with qualified names (NodeVisitor)
- match_functions.py — similarity-based function matching across two versions (Hungarian algorithm)
- pipeline.py — full integration: mines httpx history, persists to archaeologist.db (SQLite),
  includes get_lifeline() which follows rename chains backward to assemble full function history

## What's NOT built yet
- Issue/PR grounding (linking commits to GitHub issues they closed)
- Embeddings + retrieval layer
- LLM explanation layer (must stay "thin" — only narrate structured facts we've already
  verified, never free-form guess)
- Any CLI or UI
- Full-history run (currently only tested on first 150 of httpx's 1,523 commits)

## Working conventions
- Run scripts from Code_archae/ (one level above httpx/), not from inside httpx/ —
  Repository('httpx') is a relative path.
- Every new algorithmic decision should be demonstrated against real httpx history
  before being treated as correct — we've caught two real bugs this way already.