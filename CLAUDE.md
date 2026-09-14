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

## Call graph ("Connections" feature)

### Design journey — three iterations, two real bugs caught by testing on real code
1. First attempt: matched ALL function calls (both bare `foo()` and `x.foo()`
   attribute calls) by bare name against the known qualified-name set.
   Found two real false positives by testing on actual httpx code, not by
   inspection:
   - `asyncio.sleep(...)` incorrectly resolved to an internal function also
     named `sleep` (a test helper wrapper in tests/concurrency.py) purely
     because the bare names coincided.
   - `output.splitlines()` (Python's built-in str.splitlines) incorrectly
     resolved to an internal helper function also named `splitlines`.
2. Second attempt: added import-statement tracking to skip calls made
   through a known imported module name (fixes the asyncio.sleep case).
   Did NOT fix the splitlines case -- built-in type methods aren't imports,
   so there's nothing to track them against without real type inference.
3. FINAL, SHIPPED DESIGN: restrict resolution to ONLY `self.method()` /
   `cls.method()` calls. This is the one pattern where we can be reasonably
   confident we're looking at an actual internal method call rather than a
   built-in or an arbitrary local variable's method. Eliminates both false
   positive classes at once. The import-tracking code from attempt 2 was
   abandoned as unnecessary once this shipped -- self/cls calls are never
   made through an imported name anyway.

### The real, honest cost of this scope
On httpx: call sites dropped from 4,120 (matching any call) to 127
(self/cls only) -- a 97% reduction. This is not a bug, it's the deliberate
price of trustworthiness over coverage. We lose visibility into module-level
functions calling each other and any call through a local variable holding
an object of unknown type. Widening this properly would need real type
inference (e.g. via a real type checker), a much bigger undertaking --
explicitly out of scope, not attempted.

### Measured ambiguity rate -- weighted by frequency, not just distinct names
20% of distinct method names in httpx are ambiguous (shared by 2+ classes,
e.g. __init__, close). But weighted by actual call frequency, ~56% of
RESOLVED calls are ambiguous (e.g. `send`, `sync_auth_flow` are both common
AND ambiguous, since httpx has parallel sync/async classes with identical
method names). Naive distinct-name counting significantly understated the
real-world ambiguity rate.

### Ambiguous calls are stored honestly, never resolved arbitrarily
call_graph table: caller_qualified_name, callee_bare_name,
resolved_qualified_name (NULL if ambiguous), is_ambiguous, candidates
(JSON list, populated only if ambiguous). format_call_context() in
build_call_graph.py renders both confident and ambiguous relationships
explicitly, e.g. "calls send() -- ambiguous, could be:
ASGITransport.send, AsyncClient.send, Client.send" -- never picks one.

### Critical caveat, stated in the data AND the system prompt
"No calls detected" must never be read as "calls nothing" -- it means
"nothing detectable via our narrow self/cls-only method." This is baked
into format_call_context()'s output text itself (not just the prompt),
since the prompt instruction alone was NOT sufficient (see below).

### 5th deterministic check: check_connections_overclaim -- a real failure caught on the FIRST live test
The very first real generation with the new three-section prompt directly
violated the explicit "never say no callers/calls nothing" instruction:
generated text included "the function is not called by any other
function... The function calls no other functions." Telling the model the
data was incomplete did not stop it from drawing the forbidden conclusion.
None of the four existing checks caught this (it's a new failure category,
not a hash/issue/event-type/hedge-language problem).
FIX: check_connections_overclaim() scans for a list of overclaim phrases
("not called by any", "calls no other", "has no callers", "is unused",
etc.) and forces confidence to "low" if any appear (this is a direct
contradiction of provided data, not just a hedge risk -- treated more
severely than the red-flag phrase scan).
VERIFIED PROPERLY: a live regeneration came back clean (connections_overclaims:
[]), but this was correctly NOT trusted as proof on its own, since local LLM
generation is non-deterministic and the model might have simply phrased
things differently that run. The check was separately verified by feeding
it the EXACT literal violating sentences from the failed run -- confirmed
it correctly returns all three matched phrases. This is the same "test
against the known-bad case directly, don't trust a clean non-deterministic
rerun" discipline used for the check_event_type_consistency clause-splitting
fix earlier.
Also worth noting: the hasIssues gate in app.js had to be extended to
include connections_overclaims -- without that, an overclaim-only response
would have silently rendered "passed all checks" instead of the flag, which
is worse than having no check at all (confidently wrong AND visibly marked
clean).

## Phase B: embeddings + retrieval

### Environment bugs fixed before this would even run (not code issues)
1. Stale msvcp140.dll (v14.27, ~2020) bundled directly in C:\Anaconda3\
   shadowed the newer, compatible System32 copy (v14.50) due to Windows'
   DLL search order (exe's own directory searched before System32) --
   crashed torch on import with an access violation. FIX: reinstalled
   torch via `conda install pytorch cpuonly -c pytorch`, which pulls a
   matched runtime rather than relying on the stale colocated DLL.
2. That surfaced a second, well-known issue: duplicate OpenMP runtime
   conflict between numpy's MKL and torch's bundled OpenMP. FIX: standard
   `KMP_DUPLICATE_LIB_OK=TRUE` environment variable workaround.
Both confirmed via real evidence (Windows Event Viewer crash logs showing
the exact faulting DLL) before acting, not guessed at.

### Model choice: sentence-transformers (all-MiniLM-L6-v2), not Ollama
Deliberate choice for THIS piece specifically, unlike the rest of the
project: pure Python library, no separate server process, so it could be
verified directly rather than only ever testable on the Windows machine
(the same limitation that applied to every Ollama-dependent feature).
Also the same model family already used in the FinSage project.

### Critical design finding: current-HEAD-only embedding contradicts the project's own premise
First version embedded only each function's CURRENT source (via the same
get_current_source logic as the explanation feature). Measured against
real queries: "authentication" and "redirect handling" worked well
(0.38-0.42 similarity, genuinely on-topic). "Connection pooling" scored
weakly (0.20-0.32, unrelated header tests) -- traced precisely, not
assumed: the real ConnectionPool/ConnectionStore implementation classes
from an earlier httpx architecture era no longer exist at the current
checkout, so they were never embedded at all. This is a coverage gap,
not an embedding-quality problem -- and it directly undermines the
project's actual purpose (explaining history, not just current state) if
left unaddressed.
FIX: for functions with no current source (deleted, refactored away,
superseded), fall back to embedding constructed text: the qualified name
+ every file path it ever lived at + every commit message associated
with its changes. Verified: "connection pooling and reuse" jumped from
0.20-0.32 (wrong functions) to 0.52-0.59 (ConnectionStore.__getitem__,
ConnectionPool.acquire_connection, ConnectionPool.release_connection --
the actual real implementation, correctly tagged [historical_metadata]).
970 of 970 functions now embedded (191 current_code, 779
historical_metadata), versus 191 of 970 before the fix.

### Real migration bug hit and fixed (schema drift between sessions)
function_embeddings already existed from an earlier test run with a
2-column schema (no source_type column). CREATE TABLE IF NOT EXISTS is a
no-op against an already-existing table, so the new 3-column INSERT
failed with a column-count mismatch. FIX: ALTER TABLE ADD COLUMN
migration check, mirroring the identical pattern already used in cli.py's
get_cached_or_generate for the explanations table -- same category of
bug, same established fix pattern, correctly recognized and reapplied
rather than treated as new.

### Non-obvious finding, deliberately NOT patched -- documented instead
historical_metadata text is keyword-dense (bare name + file paths + real
commit messages, no code noise) and can OUT-SCORE current_code embeddings
even for queries where current code exists and works fine (e.g. auth,
redirect test functions scored lower than historical_metadata hits on
the same topics). This is NOT a bug -- similarity score answers "is this
topically relevant" and is answering it correctly either way.
DELIBERATELY NOT FIXED: a naive "prefer current_code when both exist"
tie-breaker would override a working signal to serve a concern (richer
explanations) that belongs to a different layer entirely.
WHERE THIS ACTUALLY NEEDS HANDLING: Phase C's explanation-generation step,
not retrieval. A retrieved current_code hit can get a real "what it does"
description; a historical_metadata hit has no live code to describe --
the honest response is "no longer exists in the current codebase, here is
what's tracked about its history," not a fabricated code description.
Revisit the retrieval-level question only if real usage in Phase C shows
this actually causing problems -- don't preemptively patch a working
signal based on a single test session's queries.

## Status: Phase B complete
Retrieval works, verified against real, deliberately adversarial test
queries (not just easy ones) across both current-code and historical-only
functions. NEXT: Phase C -- the chatbot itself. Needs its own grounding
discipline from day one: retrieved historical_metadata hits must never be
described as if they were live code, and the same five-check grounding
discipline from the single-function explanation feature applies here too,
likely needing extension for multi-function context.

## Phase C: free-form Q&A (ask.py) -- in progress

### Architecture: built entirely from existing infrastructure, nothing reinvented
ask.py combines, in order: build_embeddings.search() (Phase B retrieval,
top-K functions for a query) -> a context-assembly step per retrieved
function reusing cli.py's get_lifeline/format_lifeline_context and
build_embeddings.get_current_source unchanged -> one LLM synthesis call
across all retrieved functions -> six grounding checks (three reused
directly from the single-function explainer, three new to this phase).

### Three NEW failure modes identified before writing any code, specific to multi-function synthesis
Single-function explanation never had to guard against these:
1. Cross-function misattribution -- a fact true about function A gets
   stated as if about function B, now that both share one prompt.
2. Forced relevance -- weakly-relevant retrieved functions get force-
   connected to the question instead of honestly flagged as unrelated.
3. Invented relationships -- claiming two functions were "changed
   together" or "A calls B" with nothing in the data supporting it.

### check_cross_function_attribution: THREE real bugs found across three rounds, each a genuinely different lesson
1. **1:1 hash-ownership assumption (wrong data model).** First version
   built hash_to_true_function as a dict -- one hash, one "true" owner.
   Real DB verification (not assumption) showed this is false by this
   project's own design: a rename commit legitimately produces an event
   for BOTH the old and new qualified name, and an ordinary commit
   touching multiple functions at once is completely normal. Any
   legitimate second citation of a shared hash got falsely flagged.
   FIX: hash_to_valid_functions as hash -> SET of legitimate owners: a
   citation is only flagged if the claimed function is NOT in that set.
   Verified: the exact real false-positive case (Connection._release /
   Connection._body_iter sharing rename-commit hashes) now returns
   empty, while a synthetic genuine cross-function error (a hash cited
   under a function with zero real relationship to it) is still caught.
2. **Exact-match header parsing (fragile to model formatting drift).**
   Checker required the section header text to exactly equal a known
   qualified name. A live run produced headers like
   "### FunctionPoolManager.__init__" (model concatenated "Function"
   directly onto the real name, no space) -- this never exactly equals
   "PoolManager.__init__", so ANY hash cited under that header would be
   compared against the wrong "true" owner and falsely flagged, even a
   100% correct citation.
   FIX: match by substring containment instead of exact equality -- a
   mangled header still CONTAINS the real name. Verified two ways: (a)
   confirmed the real mangled-header run had zero hash citations, so it
   alone couldn't prove anything either way; (b) directly injected a
   known-real hash under the exact mangled header text and ran BOTH the
   old and new logic side by side -- old logic: "WOULD FALSELY FLAG"
   (confirmed), new logic: correctly clean. Proved the fix rather than
   waiting to get lucky on a live rerun.
3. **Silent no-op when the model skips the section-header format
   entirely.** If the model writes plain prose instead of "###
   FunctionName" sections, the original checker had nothing to split on
   and silently returned an empty list -- indistinguishable from "checked
   and clean." FIX: return a third value (or in the earliest version, a
   tuple) explicitly signaling structure_checked=False when no headers
   are found at all, so "couldn't verify" is visibly different from
   "verified and clean." Also extended: a header that doesn't match ANY
   retrieved function name at all now surfaces as its own
   unrecognized_headers signal, rather than being silently absorbed
   either as a pass or a guessed match.
Same underlying lesson across all three, worth remembering for future
checks: when a check depends on the model following an exact format or
a clean data model, build in tolerance for drift from day one -- these
were all found live, not anticipated in advance, meaning the first
version of a new check should be treated as a hypothesis to stress-test
against real generations, not a finished tool.

### check_retrieval_overreach: pattern-chasing abandoned in favor of a deterministic disclaimer, mid-project
Round 1: caught 0 of 4 real "Overall, this doesn't answer the question"
verdict sentences across a live batch -- each defeated by a DIFFERENT
real cause (missing vocabulary, wrong verb inflection: "do" vs "does",
apostrophe breaking word-boundary matching). Two rounds of vocabulary
widening improved this to 3 of 4, but the pattern kept finding new ways
to almost work rather than converging -- the space of natural-language
paraphrases for "this doesn't tell us that" is effectively unbounded,
making this structurally a harder problem than the concrete-fact checks
(hash exists, event type matches) that work well elsewhere in this
project. This is the same category of limitation that motivated
abandoning the LLM-judge approach back in Phase A -- except here it
showed up in a DETERMINISTIC keyword check, proving the lesson
generalizes: it's not really "LLM judgment is unreliable," it's
"open-ended semantic pattern-matching in natural language is hard,
regardless of which tool tries to do it."
DECISION: stopped patching the detector and made the underlying fact
structurally unconditional instead. DISCLAIMER_TEMPLATE is appended in
code, not generated by the model, and is NOT conditional on any check
passing or failing -- it always states "this answer is based on N of
TOTAL functions; absence from these N doesn't mean absence from the
codebase." Verified 100% reliable across 12 total live runs (three
separate 4x batches) -- exact counts, every time, because it's plain
string formatting with no model variance to fail. check_retrieval_overreach
is KEPT as a secondary, best-effort signal (now catching 3 of 4 real
cases after widening), but is explicitly no longer the primary safeguard
for this risk.

### Real ordering bug found and fixed: the disclaimer was contaminating its own grounding check
After adding the disclaimer, it was appended to `answer` BEFORE the
grounding checks ran -- so check_retrieval_overreach scanned its own
disclaimer text ("does not mean it does not exist... in the codebase")
and flagged it as a violation on every single run, since it shares
vocabulary with the real violation pattern despite saying the opposite
(the correct caveat, not an overreach). FIX: checks now run against
raw_answer (the model's untouched output); the disclaimer is appended
only to the copy returned to the caller, after all checks complete.
Verified: re-ran the same 4x batch, confirmed the disclaimer's own
sentence no longer appears in retrieval_overreach in any run, while
genuine model-generated overreach claims are still caught correctly.

### Current check inventory for ask.py (6 total, 3 reused + 3 new)
Reused unchanged from the single-function explainer: hash citation
check, issue citation check, red-flag phrase scan.
New to Phase C: check_cross_function_attribution (3 real bugs fixed, see
above), check_retrieval_overreach (demoted to secondary signal, see
above), plus the always-on deterministic disclaimer (not a "check" in
the traditional sense -- a structural guarantee that needs no detection
logic at all).

## Status: Phase C in progress
ask.py works end-to-end against real repos with real, verified grounding.
Extensively tested against ONE deliberately adversarial query
("how does connection pooling work" -- chosen because the honest answer
is "the real implementation is historical, not current," making it a
good stress test for exactly the failure modes this phase needed to
catch). NOT yet tested against a different question shape, and NOT yet
wired into the web UI -- still CLI-only (`python ask.py <repo_key>
<question>`). Diminishing returns are visible on further iteration of
this same query; the next real learning will likely come from testing a
structurally different kind of question, not a fifth round on this one.

## Commit attribution hardening
~/.claude/settings.json now sets "attribution": {"commit": "", "pr": ""}
to suppress Claude Code's default commit trailers. This alone was not
fully trusted, since Anthropic's own docs note inconsistent enforcement
across invocation paths -- added a belt-and-suspenders git hook as a
guaranteed fallback: .githooks/commit-msg (wired via core.hooksPath),
strips both the Co-Authored-By line and the "Generated with Claude Code"
line regardless of what the settings.json does. REAL BUG caught during
setup: the hook's original pattern matched the emoji-prefixed line via
the literal 🤖 byte sequence embedded in the shell script -- this worked
when the script was run manually but silently failed when git itself
spawned the hook (the Co-Authored-By line was stripped correctly, the
emoji line was not) -- some encoding/locale difference in how the
process was invoked. FIX: match on the ASCII trailer text instead of the
emoji, sidestepping the encoding dependency entirely. Verified against
real `git commit` invocations (not just manual script runs) before
trusting it, since manual testing was exactly what had been giving a
false sense of confidence.
Full history cleanup (removing Claude co-authorship from PAST commits,
before this fix existed) is still deliberately deferred to end-of-project,
per an earlier explicit decision -- this fix only stops the problem from
growing further, it doesn't retroactively fix already-pushed commits.

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
- build_call_graph.py -- AST-based call-graph extraction (self/cls calls
  only) and resolution, with honest handling of ambiguous matches

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

## Status: Phase A complete
Everything through multi-repo support, the file tree view, the
code-explanation feature (What it does / Connections / History), and all
five deterministic grounding checks is built and verified against real
data, not just written and assumed correct. NEXT: Phase B (embeddings +
retrieval) -- the actual prerequisite for free-form Q&A, since the current
system only ever explains ONE function you already know the name of; a
real chatbot needs to find WHICH functions are relevant to an arbitrary
question first.
