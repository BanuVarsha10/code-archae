# Code Archaeologist

## What this project does
Reconstructs *why* code exists by mining git history and mapping each function's
identity across renames/refactors, so an LLM can explain a function's history
grounded in real commit citations — not guesses. Target repo: httpx
(github.com/encode/httpx), cloned locally into Code_archae/httpx.

## Current state (as of this session)
- Processed 450 of httpx's 1,523 commits into archaeologist.db (SQLite)
- 17,307 function events tracked (added/modified/renamed/deleted)
- 223 real GitHub issues/PRs fetched and linked to commits
- Working LLM explanation layer (explain.py) using local Ollama models
- Two grounding-verification approaches tried; see "LLM judge" section below

## Tech decisions already made, and why (don't relitigate without a real reason)
- **PyDriller** for git mining — exposes modified_files[i].source_code /
  source_code_before directly, no separate git-content-fetching needed.
- **Python's `ast` module** (not tree-sitter) — exact for valid Python, zero
  dependency. Known limitation: Python-only, breaks on invalid syntax.
- **Qualified names** (ClassName.method_name) via NodeVisitor tracking class
  context. Known open gap: @property getter/setter pairs still collide under
  one qualified name.
- **Two-tier function matching** (upgraded from a single-tier approach after
  finding TWO real bugs via evidence, not assumption):
  - Tier 1: exact-name match on both sides → always 'modified', regardless of
    body similarity. Identity is a stronger, cheaper signal than content
    similarity when unambiguous.
  - Tier 2: everything left over goes through difflib.SequenceMatcher +
    scipy.optimize.linear_sum_assignment (Hungarian algorithm) for optimal
    (not greedy) rename detection. Similarity threshold 0.75, min function
    body length 80 chars.
  - Bug 1 found: greedy assignment produced a false positive
    (ConnectionSemaphore.acquire mismatched to .release) — fixed by switching
    greedy → optimal assignment.
  - Bug 2 found: single-tier similarity matching produced false delete+add
    pairs on functions that kept their name but were heavily edited (e.g.
    HTTP11Connection.close after "Drop unreachable except block", similarity
    0.51, below threshold) — fixed by adding Tier 1. This fix also made the
    pipeline ~5x faster (533s → 105s on 450 commits) since most functions
    never need the expensive similarity computation at all.
- **SQLite** with an **append-only event log** schema (function_events table),
  not a mutable current-state table — replay-from-scratch if logic changes,
  rather than needing data migrations. IMPORTANT: pipeline.py wipes and
  rebuilds function_events and commits at the start of every run
  (DELETE FROM ...) for idempotency — this was itself a bug found and fixed
  (reruns were silently duplicating every event before this fix).
- **Issue/PR grounding**: regex `#(\d+)` on commit messages, GitHub REST API
  (not GraphQL — simpler for one-at-a-time lookups), cached in a local
  `issues` table. Real finding: httpx's early history uses "Merge pull
  request #N" style (PR number only on the empty merge commit, never on
  individual commits); later history (~commit 440+) squash-merges with
  inline "(#386)"-style references on the actual working commit. Both
  eras coexist in the 450 commits processed so far.
- **LLM explanation layer**: Ollama running locally (llama3.2:3b for
  generation), NOT a paid API — deliberate cost-constraint decision, not a
  quality-blind default. GPU-accelerated (RTX 4050 laptop, 6GB VRAM,
  confirmed 100% GPU via `ollama ps`, no CPU spillover with 3b or 8b models).
  System prompt forbids speculative language explicitly.

## LLM judge saga — read before touching this again
We tried three prompt iterations for an LLM-based groundedness judge, across
two model sizes:
- llama3.2:3b as judge: confused source data with the text it was supposed
  to be checking (fixed via explicit <tags> in the prompt), then still
  inverted correct hedge sentences ("reason is not captured" flagged as a
  violation, the opposite of correct).
- llama3.1:8b as judge: better recall (caught a real causal-claim violation
  the 3b judge missed) but still inverted polarity on hedge sentences in a
  later run, and misquoted text it claimed to be judging.
- DECISION: replaced the LLM judge entirely with two deterministic checks
  (see explain.py): a red-flag phrase scanner (lexical substring match
  against known causal/evaluative phrases) and an event-type consistency
  checker (cross-references cited hashes against the real event_type in the
  database — this is what would have caught a real bug we found earlier,
  where generated text mislabeled a 'deleted' event as a 'modification').
  This was a deliberate evidence-based pivot away from LLM judgment for a
  task that turned out to need more nuance than local models reliably give,
  NOT a default choice.
- KNOWN LIMITATIONS of the deterministic approach, not yet stress-tested:
  (1) the phrase scanner will miss causal claims phrased without any listed
  trigger word, and could false-positive on benign uses of a listed word;
  (2) the event-type checker only catches mismatches when the explanation
  both cites a hash AND uses a mapped keyword in the same sentence — a vague
  explanation that avoids both trivially passes without being more accurate.

## Files built so far
- explore.py, explore_ast.py — early proof-of-concept scripts (Phase 1-2)
- match_functions.py — standalone matcher demo
- pipeline.py — full mining + matching + SQLite persistence (the real pipeline)
- fetch_issues.py — GitHub issue/PR fetching + caching
- show_lifeline.py — prints a function's full lifeline with linked issues
- explain.py — LLM explanation layer + grounding checks (the current state
  of Phase 5)

## What's NOT built yet
- Any CLI beyond `python explain.py <function_name>` — no way to list or
  search available functions
- Any web UI (the original goal: browse a function tree, click into a
  function, see its lifeline + explanation)
- Full-history run (still capped at 450 of 1,523 commits)
- Stress-testing the deterministic grounding checks on messier functions
- A formal accuracy evaluation write-up

## Working conventions
- Run scripts from Code_archae/ (one level above httpx/), not from inside
  httpx/ — Repository('httpx') is a relative path.
- Every new algorithmic decision gets demonstrated against real httpx
  history before being treated as correct — we've caught four real bugs
  this way (idempotency, greedy false-positive, similarity false-negative,
  LLM judge polarity inversion) and it keeps paying off.
- GITHUB_TOKEN and ANTHROPIC_API_KEY (if ever added) live in .env, which is
  gitignored. Never hardcode credentials.
