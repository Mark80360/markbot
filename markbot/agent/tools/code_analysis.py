"""Code analysis tool for understanding code structure and dependencies."""

import ast
import re
from pathlib import Path
from typing import Any

from markbot.agent.tools.base import Tool


class CodeAnalysisTool(Tool):
    """Analyze code structure, dependencies, and complexity."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "analyze_code"

    @property
    def description(self) -> str:
        return "Analyze code structure, dependencies, complexity, or security issues in a file or directory."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory path to analyze"},
                "analysis_type": {
                    "type": "string",
                    "enum": ["dependencies", "structure", "complexity", "security"],
                    "description": "Type of analysis: dependencies (imports/requires), structure (classes/functions), complexity (cyclomatic), security (common issues)"
                }
            },
            "required": ["path", "analysis_type"]
        }

    async def execute(self, path: str, analysis_type: str, **kwargs: Any) -> str:
        p = Path(path)
        if self._workspace and not p.is_absolute():
            p = self._workspace / p

        p = p.resolve()
        if self._allowed_dir:
            try:
                p.relative_to(self._allowed_dir.resolve())
            except ValueError:
                return f"Error: Path {path} is outside allowed directory {self._allowed_dir}"

        if not p.exists():
            return f"Error: Path {path} does not exist"

        if p.is_file():
            return await self._analyze_file(p, analysis_type)
        elif p.is_dir():
            return await self._analyze_directory(p, analysis_type)
        else:
            return f"Error: {path} is not a file or directory"

    async def _analyze_file(self, path: Path, analysis_type: str) -> str:
        ext = path.suffix.lower()
        content = path.read_text(encoding="utf-8", errors="ignore")

        if ext == ".py":
            return self._analyze_python(content, analysis_type, path)
        elif ext in [".js", ".jsx", ".ts", ".tsx"]:
            return self._analyze_javascript(content, analysis_type, path)
        else:
            return f"Error: Unsupported file type {ext}"

    async def _analyze_directory(self, path: Path, analysis_type: str) -> str:
        results = []
        for file in path.rglob("*"):
            if file.is_file() and file.suffix in [".py", ".js", ".jsx", ".ts", ".tsx"]:
                result = await self._analyze_file(file, analysis_type)
                if not result.startswith("Error"):
                    results.append(f"## {file.relative_to(path)}\n{result}")

        return "\n\n".join(results) if results else "No supported files found"

    def _analyze_python(self, content: str, analysis_type: str, path: Path) -> str:
        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            return f"Error: Syntax error at line {e.lineno}: {e.msg}"

        if analysis_type == "dependencies":
            return self._python_dependencies(tree)
        elif analysis_type == "structure":
            return self._python_structure(tree)
        elif analysis_type == "complexity":
            return self._python_complexity(tree)
        elif analysis_type == "security":
            return self._python_security(content, tree)
        return "Error: Unknown analysis type"

    def _python_dependencies(self, tree: ast.AST) -> str:
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imports.append(f"from {module} import {alias.name}")

        if not imports:
            return "No imports found"
        return "**Dependencies:**\n" + "\n".join(f"- {imp}" for imp in sorted(set(imports)))

    def _python_structure(self, tree: ast.AST) -> str:
        classes = []
        functions = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = [m.name for m in node.body if isinstance(m, ast.FunctionDef)]
                classes.append(f"class {node.name}: {len(methods)} methods")
            elif isinstance(node, ast.FunctionDef) and not any(isinstance(p, ast.ClassDef) for p in ast.walk(tree) if node in getattr(p, 'body', [])):
                functions.append(f"def {node.name}()")

        result = []
        if classes:
            result.append("**Classes:**\n" + "\n".join(f"- {c}" for c in classes))
        if functions:
            result.append("**Functions:**\n" + "\n".join(f"- {f}" for f in functions))
        return "\n\n".join(result) if result else "No classes or functions found"

    def _python_complexity(self, tree: ast.AST) -> str:
        complexities = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                complexity = self._calculate_complexity(node)
                level = "low" if complexity <= 5 else "medium" if complexity <= 10 else "high"
                complexities.append(f"{node.name}: {complexity} ({level})")

        if not complexities:
            return "No functions found"
        return "**Cyclomatic Complexity:**\n" + "\n".join(f"- {c}" for c in complexities)

    def _calculate_complexity(self, node: ast.FunctionDef) -> int:
        complexity = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
        return complexity

    def _python_security(self, content: str, tree: ast.AST) -> str:
        issues = []

        # Check for eval/exec
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ["eval", "exec"]:
                    issues.append(f"[WARNING] Dangerous function: {node.func.id}() at line {node.lineno}")

        # Check for SQL injection patterns
        if re.search(r'execute\s*\([^)]*%s[^)]*\)', content):
            issues.append("[WARNING] Possible SQL injection: string formatting in SQL query")

        # Check for hardcoded secrets
        if re.search(r'(password|secret|api_key)\s*=\s*["\'][^"\']+["\']', content, re.IGNORECASE):
            issues.append("[WARNING] Possible hardcoded secret")

        return "**Security Issues:**\n" + "\n".join(issues) if issues else "[OK] No obvious security issues found"

    def _analyze_javascript(self, content: str, analysis_type: str, path: Path) -> str:
        if analysis_type == "dependencies":
            return self._js_dependencies(content)
        elif analysis_type == "structure":
            return self._js_structure(content)
        elif analysis_type == "security":
            return self._js_security(content)
        return "Error: Complexity analysis not yet supported for JavaScript"

    def _js_dependencies(self, content: str) -> str:
        imports = []
        # ES6 imports
        for match in re.finditer(r'import\s+.*?\s+from\s+["\']([^"\']+)["\']', content):
            imports.append(f"import from '{match.group(1)}'")
        # CommonJS requires
        for match in re.finditer(r'require\s*\(["\']([^"\']+)["\']\)', content):
            imports.append(f"require('{match.group(1)}')")

        if not imports:
            return "No imports found"
        return "**Dependencies:**\n" + "\n".join(f"- {imp}" for imp in sorted(set(imports)))

    def _js_structure(self, content: str) -> str:
        functions = re.findall(r'(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\()', content)
        classes = re.findall(r'class\s+(\w+)', content)

        result = []
        if classes:
            result.append("**Classes:**\n" + "\n".join(f"- {c}" for c in classes))
        if functions:
            func_names = [f[0] or f[1] for f in functions]
            result.append("**Functions:**\n" + "\n".join(f"- {f}" for f in func_names))
        return "\n\n".join(result) if result else "No classes or functions found"

    def _js_security(self, content: str) -> str:
        issues = []

        if re.search(r'\beval\s*\(', content):
            issues.append("[WARNING] Dangerous function: eval()")
        if re.search(r'innerHTML\s*=', content):
            issues.append("[WARNING] Possible XSS: innerHTML assignment")
        if re.search(r'dangerouslySetInnerHTML', content):
            issues.append("[WARNING] Possible XSS: dangerouslySetInnerHTML")
        if re.search(r'(password|secret|api_key)\s*=\s*["\'][^"\']+["\']', content, re.IGNORECASE):
            issues.append("[WARNING] Possible hardcoded secret")

        return "**Security Issues:**\n" + "\n".join(issues) if issues else "[OK] No obvious security issues found"
