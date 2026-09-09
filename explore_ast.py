import ast

class FunctionFinder(ast.NodeVisitor):
    def __init__(self):
        self.functions = []
        self.class_stack = []

    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node):
        qualified = '.'.join(self.class_stack + [node.name])
        self.functions.append((qualified, node.lineno, node.end_lineno))
        self.generic_visit(node)

with open('httpx/httpx/_models.py') as f:
    tree = ast.parse(f.read())

finder = FunctionFinder()
finder.visit(tree)

for name, start, end in finder.functions[:15]:
    print(f'{name:35.35}  lines {start}-{end}')
