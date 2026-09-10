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
- DECISION: replaced the LLM judge entirely with deterministic checks (see
  cli.py) — this was an evidence-based pivot away from LLM judgment for a
  task needing more nuance than local models reliably give, NOT a default
  choice made for convenience.

## Deterministic grounding checks (current approach, in cli.py)
Four checks run on every freshly-generated (non-cached) explanation:
1. **Hash citation check**: extracts 8-char hex tokens, verifies each
   against real commit hashes. Handles malformed-but-real citations (e.g. a
   dropped leading zero, via zero-padding) separately from genuinely
   fabricated ones.
2. **Issue/PR citation check**: extracts #N references, verifies each
   against issue numbers that were ACTUALLY fetched and cached (not just
   referenced in a commit message). Found via manual spot-check, not by any
   automated test: the model cited "#317" as a real linked issue when #317
   was never fetched into the issues table at all — a genuine hallucinated
   citation that passed every other check silently. This is the most
   important bug this project has found: it shows a "PASSED" result from
   the other three checks is not sufficient evidence of full groundedness.
3. **Red-flag phrase scan**: lexical substring match against known
   causal/evaluative phrases (in response to, likely, improved, aimed to,
   etc.), now COUNT-based with severity tiers (minor/moderate/severe), not
   just presence/absence — a context-heavy run on Response.__init__ (101
   events) produced ~45 repetitions of "which likely aimed to improve
   overall accuracy," a qualitatively worse failure than occasional hedge
   language, invisible under presence-only reporting.
4. **Event-type consistency check**: cross-references cited hashes against
   real event_type in the database. Fixed a real false-positive: quoted PR
   titles (e.g. a title literally containing the word "added") were being
   scanned as if they were the model's own claims. Fix: strip quoted spans
   before keyword-matching, still scan full sentence for hashes.

## Context length is itself a failure mode, not just a phrasing problem
Found via evidence, not assumption: functions with very large event counts
(100+) caused generation to degrade into repetitive templated filler
("PR #N did X, which likely aimed to improve overall accuracy") for nearly
every line, rather than occasional hedging. Root cause diagnosed as context
size, not prompt wording, by testing HTTP11Connection.close (29 events, fine)
against Response.__init__ (101 events, degenerate) with an IDENTICAL prompt.
FIX: format_lifeline_context now caps detailed events at 15 for any function
exceeding that, always keeping renames + first 5 + last 5, with an explicit
context note telling the model not to speculate about omitted events. This
measurably reduced both invented content AND hedge-language severity in the
same run — evidence the two problems share a root cause.

## A THIRD failure category, not yet caught by anything: narrative fabrication
Distinct from a wrong citation (cites something unreal) and a wrong event-type
label (mislabels something real) is fabricating a plausible-sounding CAUSAL
SEQUENCE between two real, correctly-cited events that never actually
occurred. Real example: given real events "6a4376b2: deleted" followed by
"39b57c93: modified", the model wrote "the function was deleted, then a NEW
function was CREATED to replace it" -- a coherent narrative connecting two
real facts that doesn't correspond to what actually happened (39b57c93 was
a plain modification, not a recreation). This got caught ONLY because the
narrative happened to also produce a wrong keyword ("created") that the
event-type checker could flag -- if the model had woven the same false
narrative using only correctly-typed keywords, nothing would have caught it.
STATUS: known gap, not yet fixed. Worth a dedicated check if this project
continues: something that flags claimed causal/sequential relationships
between events and verifies no such relationship is stated in the source
data (similar in spirit to the red-flag scanner, but for inter-event claims
rather than single-event claims).

## Explanation caching
explanations table (qualified_name PRIMARY KEY, explanation, generated_at)
caches generated text to avoid re-running local inference for repeat
queries. KNOWN GAP: not yet invalidated when function_events changes (e.g.
after processing more commit history past the current 450) -- pipeline.py's
existing wipe-and-rebuild step should also clear this table, but doesn't
yet.

## Multi-repo hardening (this session)

### Cross-process race condition in repo_registry.py -- found and fixed
threading.Lock() only serializes threads within ONE Python process. Every
indexing job runs as a SEPARATE OS process (subprocess.Popen), so the lock
was providing zero real protection -- confirmed by reproducing the exact
crash ("Expecting value: line 1 column 1 (char 0)") from two plain OS
processes racing on repos.json, with no git/pipeline complexity involved
at all. Worse than a crash: one process's data was silently lost entirely
when the other's write raced past it (a "lost update", not just corruption).
FIX: replaced threading.Lock with the `filelock` library (real cross-process
locking) plus atomic writes (write to a .tmp file, then os.replace() onto
the real path, so a reader can never observe a half-written file). Verified
with 5 repeated concurrent-process test runs, zero crashes, zero lost
updates, before trusting it.

### Windows path-separator bug -- found by real Windows testing, not by me
mf.new_path / mf.old_path return backslash-separated paths on Windows
(e.g. 'httpcore\api.py'), but /tree splits on '/', so nothing nested --
every file landed flat with a literal backslash in its name. This was
invisible in Linux-only testing (backslash paths can't occur there) and
was caught via direct raw-DB inspection (repr() on stored file_path values)
on the actual Windows dev machine. FIX: normalize backslash to forward
slash before splitting, in api_tree (app.py).
IMPORTANT, separately: some repos genuinely DO have the same filename at
two different real paths across their history (e.g. Fin-rag_genai's
agents.py -> finsage/agents.py, confirmed via commit trace: added at
root, deleted, re-added under finsage/ in a commit literally titled
"Fixed finsage folder (removed submodule)"). That's correct, expected
tree behavior, not a bug -- don't conflate the two issues if this comes
up again.

## File tree view
New /api/repos/{repo_key}/tree endpoint: groups function_events by
file_path (nested by directory) and qualified_name. Frontend: a
"File tree" / "Most excavated" tab toggle in the sidebar, recursive
expand-on-click rendering. .tree-children needs `padding-left` in CSS --
without it, nesting is structurally correct but doesn't LOOK nested
(this was shipped once without the padding, caught in review, fixed with
one CSS line since each depth level already wraps in its own container
and the indentation compounds naturally).

## Code-explanation feature: "What it does" + "History"
generate_explanation now also fetches the function's CURRENT source (via
get_current_source: finds the most recent non-deleted event's file_path,
reads that file from the local clone in repos/{repo_key}/, extracts the
function's source via the same AST logic used everywhere else) and feeds
it to the model as additional grounded context. System prompt now asks
for two explicitly separate sections -- "What it does" (grounded in the
literal current code, no speculation about how other code calls this
function, since we don't track that) and "History" (grounded in the
lifeline, as before). Verified accurate against real code (cosine_similarity
in Fin-rag_genai: model correctly described the dot-product computation
and unit-normalization assumption from the actual source and docstring).
KNOWN LIMITATION: the legacy standalone archaeologist.db / cli.py main()
path has pre-path-fix data (bare filenames), so get_current_source
silently returns None there. Not a crash, just an inert feature on that
one legacy code path. app.py is the real entry point; this wasn't worth
fixing on the legacy path.

## Checker precision fix -- a real fabrication caught in the wild, root-caused precisely
Real example: given real events (cd169d6c=added, e9c0f742=deleted,
66e1d5f2=added), the model generated: "...marks the deletion of the
function, and a second commit hash e9c0f742 marks the addition of the
function's implementation." -- fabricating that e9c0f742 was an addition
when it's actually the deletion.
check_event_type_consistency did NOT catch this on first pass. Root
cause, verified precisely (not guessed): (1) "addition" was never in the
keyword dictionary, so the checker couldn't compare it against anything,
and (2) the checker matched "any keyword in the sentence" against "any
hash in the sentence" without binding a keyword to its actual clause --
so it found "deletion" elsewhere in the same sentence, which happened to
be e9c0f742's correct type BY COINCIDENCE, and reported a false clean
pass.
FIX: (1) expanded the keyword dictionary with noun forms (addition,
creation, removal, renaming, etc.), (2) split sentences into clauses
(on ", and " / ", but " / ";") before cross-referencing, so a keyword in
one clause can't be matched against a hash that only appears in a
different clause. Verified by feeding the EXACT original fabricated
sentence through the fixed checker directly (not a fresh non-deterministic
regeneration, which would have been an invalid test either way it came
out) -- confirmed it now correctly flags the mismatch.
RESIDUAL, DISTINCT GAP: a clause that refers to a commit via pronoun
("the same commit hash") instead of repeating the literal hex hash still
can't be checked -- there's no hash-token for the checker to key off.
This is coreference resolution, a genuinely different problem from the
clause-boundary bug just fixed, not a sign the fix is incomplete. Would
need actual NLP-style reference resolution to close, not a bigger keyword
dictionary.
STANDING LIMITATION, stated plainly: this checker catches specific,
now-broadened failure patterns found through real evidence. It does not
solve narrative fabrication in general. A claim using a word-form still
missing from the dictionary, or no recognizable keyword at all, still
slips through undetected.

## Files built so far
- explore.py, explore_ast.py — early proof-of-concept scripts (Phase 1-2)
- match_functions.py — standalone matcher demo
- pipeline.py — full mining + matching + SQLite persistence (the real pipeline)
- fetch_issues.py — GitHub issue/PR fetching + caching
- show_lifeline.py — prints a function's full lifeline with linked issues
- explain.py — LLM explanation layer + grounding checks (the current state
  of Phase 5)
- cli.py — persistent interactive CLI: ranked/searchable function listing,
  explanation generation with caching, all four deterministic grounding
  checks, get_current_source (reads a function's current source from its
  repo clone for source-grounded "What it does" explanations). This is
  the current, most complete entry point to the project.
- app.py — FastAPI backend serving the web UI: repo management, function
  listing/search, lifeline + explain endpoints, and
  /api/repos/{repo_key}/tree (file-tree view grouping function_events by
  file_path and qualified_name).

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
- When debugging a claim about real data, always query the CURRENT
  archaeologist.db directly rather than trusting memory of a prior session.
