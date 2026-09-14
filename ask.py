import re, json
import cli
import build_embeddings as be

DISCLAIMER_TEMPLATE = (
    "\n\n---\n*Note: this answer is based on the {top_k} functions retrieval "
    "judged most relevant out of {total} tracked in this repo. Absence of "
    "something from these {top_k} results does not mean it does not exist "
    "elsewhere in the codebase -- only that it was not among the closest "
    "semantic matches to this question.*"
)


def build_function_block(qualified_name, sim_score, conn, repo_key):
    source = be.get_current_source(qualified_name, conn, f'repos/{repo_key}')
    source_type = 'current_code' if source else 'historical_metadata'
    lines = [f'### {qualified_name}  (relevance: {sim_score:.2f}, type: {source_type})']
    if source_type == 'current_code':
        lines.append(f'Current source:\n```python\n{source}\n```')
    else:
        lines.append('(This function no longer exists in the current codebase -- historical only. No live code to show.)')

    events = cli.get_lifeline(qualified_name, conn)
    context_text, valid_hashes, valid_issue_numbers = cli.format_lifeline_context(qualified_name, events, conn)
    lines.append('History:')
    lines.append(context_text)
    return '\n'.join(lines), valid_hashes, valid_issue_numbers


def check_cross_function_attribution(explanation, function_hash_map):
    hash_to_valid_functions = {}
    for qname, hashes in function_hash_map.items():
        for h in hashes:
            hash_to_valid_functions.setdefault(h, set()).add(qname)

    all_known_names = list(function_hash_map.keys())
    sections = re.split(r'(?=###\s)', explanation)
    structured_sections = [s for s in sections if re.match(r'###\s*\S+', s)]
    if not structured_sections:
        return None, False, []

    misattributions = []
    unrecognized_headers = []
    for section in structured_sections:
        header_line = section.split('\n', 1)[0]
        # Match by substring containment, not exact equality -- tolerates
        # the model decorating/prefixing the header (e.g.
        # 'FunctionPoolManager.__init__' still contains the real name
        # 'PoolManager.__init__'). A prior exact-match version produced a
        # real false positive on any header formatted this way.
        matching_names = [name for name in all_known_names if name in header_line]
        if not matching_names:
            unrecognized_headers.append(header_line.strip())
            continue
        hashes_in_section = re.findall(r'\b[0-9a-f]{6,8}\b', section)
        for h in hashes_in_section:
            valid_functions = hash_to_valid_functions.get(h)
            if valid_functions and not any(name in valid_functions for name in matching_names):
                misattributions.append((h, header_line.strip(), sorted(valid_functions)))
    return misattributions, True, unrecognized_headers


def check_retrieval_overreach(explanation, retrieved_function_names):
    explanation = re.sub(r'(###[^\n]*)\n', r'\1.\n', explanation)
    overreach_patterns = [
        re.compile(r'not\s+(?:\w+\s+){0,3}(?:implemented|exist|present|available|found|supported)\s+(?:\w+\s+){0,3}(?:in\s+)?(?:the\s+)?(?:current\s+)?codebase', re.IGNORECASE),
        re.compile(r'do(?:es)?\s+not\s+(?:\w+\s+){0,3}(?:provide|answer)\s+(?:\w+\s+){0,3}(?:information|understanding|question)', re.IGNORECASE),
        re.compile(r'no\s+(?:\w+\s+){0,3}information\s+(?:on|about|regarding)', re.IGNORECASE),
    ]
    sentences = re.split(r'(?<=[.!?])\s+', explanation)
    flagged = []
    for sentence in sentences:
        if any(name in sentence for name in retrieved_function_names):
            continue
        for pattern in overreach_patterns:
            if pattern.search(sentence):
                flagged.append(sentence.strip())
                break
    return flagged


SYSTEM_PROMPT = (
    "You answer a user's question about a codebase's history using ONLY "
    "the retrieved function data provided below. Each function is labeled "
    "with a relevance score and its type -- current_code (real, live "
    "source is shown) or historical_metadata (this function no longer "
    "exists in the codebase; only its tracked name, file history, and "
    "commit messages are known -- NEVER describe historical_metadata "
    "functions as if you can see their actual code or logic).\n\n"
    "CRITICAL RULES:\n"
    "1. Structure your answer with one '### FunctionName' section per "
    "retrieved function you discuss. Do not mix facts from different "
    "functions into the same section.\n"
    "2. If a retrieved function does not actually seem relevant to the "
    "question, say so explicitly in its section rather than forcing a "
    "connection -- a low relevance score is a signal, not a fact to hide.\n"
    "3. Do not invent a relationship between two functions (e.g. 'these "
    "were changed together', 'A calls B', 'this fixed a bug in the other') "
    "unless it is explicitly stated in the data below.\n"
    "4. Cite specific commit hashes for historical claims, under the "
    "correct function's section only.\n"
    "5. End with a one-line 'Overall' verdict: does the retrieved data "
    "actually answer the user's question well, partially, or not at all?\n"
    "6. NEVER claim something is absent, unimplemented, or nonexistent in "
    "\"the codebase\" or \"the current codebase\" as a whole. You were only "
    "shown a handful of functions out of potentially thousands -- you can "
    "only honestly say a specific named function is absent, never that a "
    "concept or feature is absent from the whole project."
)


def answer_question(query, conn, repo_key, model='llama3.2:3b', top_k=5):
    results = be.search(query, conn, top_k=top_k)
    if not results:
        return "No indexed functions found for this repo.", {}

    context_blocks = []
    function_hash_map = {}
    all_valid_hashes = set()
    all_valid_issues = set()
    for sim, qname, source_type in results:
        block, valid_hashes, valid_issues = build_function_block(qname, sim, conn, repo_key)
        context_blocks.append(block)
        function_hash_map[qname] = valid_hashes
        all_valid_hashes.update(valid_hashes)
        all_valid_issues.update(valid_issues)

    full_context = '\n\n---\n\n'.join(context_blocks)

    import ollama
    response = ollama.chat(
        model=model,
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': f"User's question: {query}\n\nRetrieved functions:\n\n{full_context}"}
        ]
    )
    raw_answer = response['message']['content']

    cited_hashes = set(re.findall(r'\b[0-9a-f]{6,8}\b', raw_answer))
    hallucinated_hashes = cited_hashes - all_valid_hashes

    cited_issues = set(int(n) for n in re.findall(r'#(\d+)', raw_answer))
    hallucinated_issues = cited_issues - all_valid_issues

    red_flags = cli.scan_red_flags(raw_answer)
    misattributions, structure_checked, unrecognized_headers = check_cross_function_attribution(raw_answer, function_hash_map)
    retrieved_names = [qname for _, qname, _ in results]
    overreach = check_retrieval_overreach(raw_answer, retrieved_names)

    checks = {
        'hallucinated_hashes': sorted(hallucinated_hashes),
        'hallucinated_issues': sorted(hallucinated_issues),
        'red_flags': red_flags,
        'misattributions': misattributions,
        'structure_checked': structure_checked,
        'unrecognized_headers': unrecognized_headers,
        'retrieval_overreach': overreach,
        'retrieved_functions': [(qname, round(sim, 3), st) for sim, qname, st in results],
    }

    total_functions = conn.execute('SELECT COUNT(DISTINCT qualified_name) FROM function_events').fetchone()[0]
    answer = raw_answer + DISCLAIMER_TEMPLATE.format(top_k=len(results), total=total_functions)
    return answer, checks


if __name__ == '__main__':
    import sqlite3, sys
    repo_key = sys.argv[1]
    query = ' '.join(sys.argv[2:])
    conn = sqlite3.connect(f'{repo_key}.db')
    conn.row_factory = sqlite3.Row

    answer, checks = answer_question(query, conn, repo_key)
    print("=== ANSWER ===")
    print(answer)
    print()
    print("=== CHECKS ===")
    print(json.dumps(checks, indent=2))
