"""Auditable source counting: exclude dependencies, tests, blank lines, comments and docstrings."""
import ast
import io
import json
import tokenize
from pathlib import Path
import os


ROOT = Path(os.path.relpath(Path(__file__).parents[1]))


def count_file(path):
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    docstring_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstring_lines.update(range(first.lineno, first.end_lineno + 1))
    code_lines = set()
    ignored = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
               tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in ignored:
            code_lines.update(range(token.start[0], token.end[0] + 1))
    code_lines -= docstring_lines
    return {
        "file": path.relative_to(ROOT).as_posix(),
        "physical_lines": len(lines),
        "nonblank_noncomment_lines": sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in lines),
        "code_lines_excluding_docstrings": len(code_lines),
    }


def main():
    files = sorted((ROOT / "backend").rglob("*.py"))
    records = [count_file(path) for path in files]
    report = {
        "scope": "backend/**/*.py only; excludes frontend, tests, scripts, run.py and dependencies",
        "files": len(records),
        "physical_lines": sum(item["physical_lines"] for item in records),
        "nonblank_noncomment_lines": sum(item["nonblank_noncomment_lines"] for item in records),
        "code_lines_excluding_docstrings": sum(item["code_lines_excluding_docstrings"] for item in records),
        "modules": records,
    }
    target = ROOT / "docs" / "backend-line-count.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "modules"}, indent=2))
    if report["code_lines_excluding_docstrings"] <= 5000:
        raise SystemExit("Backend must contain more than 5000 nonblank code lines.")


if __name__ == "__main__":
    main()
