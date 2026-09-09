import ast, difflib
import numpy as np
from scipy.optimize import linear_sum_assignment
from pydriller import Repository

class FF(ast.NodeVisitor):
    def __init__(self, source):
        self.source = source; self.functions = []; self.stack = []
    def visit_ClassDef(self, n):
        self.stack.append(n.name); self.generic_visit(n); self.stack.pop()
    def visit_FunctionDef(self, n):
        self.functions.append(('.'.join(self.stack + [n.name]), ast.get_source_segment(self.source, n)))
        self.generic_visit(n)
    visit_AsyncFunctionDef = visit_FunctionDef

def extract(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    ff = FF(source); ff.visit(tree); return ff.functions

def find_renames_optimal(before, after, threshold=0.75, min_len=80):
    before = [(n, b) for n, b in before if b and len(b) >= min_len]
    after  = [(n, b) for n, b in after if b and len(b) >= min_len]
    if not before or not after:
        return []
    sim = np.zeros((len(before), len(after)))
    for i, (_, ob) in enumerate(before):
        for j, (_, nb) in enumerate(after):
            sim[i][j] = difflib.SequenceMatcher(None, ob, nb).ratio()
    row_ind, col_ind = linear_sum_assignment(1 - sim)
    renames = []
    for r, c in zip(row_ind, col_ind):
        score = sim[r][c]
        old_name, new_name = before[r][0], after[c][0]
        if score >= threshold and old_name != new_name:
            renames.append((old_name, new_name, score))
    return renames

count, found = 0, 0
for commit in Repository('httpx').traverse_commits():
    count += 1
    if count > 400:
        break
    for mf in commit.modified_files:
        if not mf.filename.endswith('.py') or not mf.source_code or not mf.source_code_before:
            continue
        before, after = extract(mf.source_code_before), extract(mf.source_code)
        for old_name, new_name, score in find_renames_optimal(before, after):
            print(f'{commit.hash[:8]}  {mf.filename:20.20}  {old_name} -> {new_name}  ({score:.2f})')
            found += 1
    if found >= 10:
        break
print(f'--- scanned {count} commits, found {found} likely renames (optimal assignment) ---')
