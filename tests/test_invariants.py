"""Repo-level invariants from CLAUDE.md section 12 (anti-goals), enforced by CI.

These are the rules that are easy to state and easy to violate six files later.
"""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
EVAL = Path(__file__).resolve().parents[1] / "eval"


def _python_files(*roots: Path) -> list[Path]:
    return [p for root in roots if root.exists() for p in root.rglob("*.py")]


def test_no_wall_clock_outside_clock_module():
    """CLAUDE.md section 12: `datetime.now()` anywhere outside clock.py.

    Checked against the parsed AST, not against raw lines: a comment or docstring
    that merely *mentions* the rule is prose, not a violation. Only real call
    expressions count.
    """
    banned_calls = {
        "datetime.now", "datetime.datetime.now", "datetime.today",
        "datetime.date.today", "date.today", "time.time", "time.monotonic",
    }
    offenders = []
    for path in _python_files(SRC, EVAL):
        if path.name == "clock.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                name = ast.unparse(node.func)
                if name in banned_calls:
                    offenders.append(f"{path.name}:{node.lineno}: {name}()")
    assert not offenders, "wall clock used outside clock.py:\n" + "\n".join(offenders)


def test_no_agent_framework_imports():
    """CLAUDE.md section 12: no LangChain / CrewAI / LangGraph. Hand-rolled loop."""
    banned = re.compile(r"^\s*(?:from|import)\s+(langchain|langgraph|crewai|autogen|llama_index)")
    offenders = [
        f"{p}:{i}"
        for p in _python_files(SRC, EVAL)
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if banned.match(line)
    ]
    assert not offenders, f"agent framework imported: {offenders}"


def test_money_fields_are_integers():
    """CLAUDE.md section 12: no floats for money. Every *_paise field is an int."""
    offenders = []
    for path in _python_files(SRC, EVAL):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"_paise\s*:\s*float", line):
                offenders.append(f"{path}:{i}: {line.strip()}")
    assert not offenders, "float used for money:\n" + "\n".join(offenders)


def test_policy_is_never_shown_to_the_llm():
    """CLAUDE.md section 1: the LLM never sees policy.yaml and cannot modify it.

    Checked as real imports and real string constants in the AST. A docstring that
    states the rule is prose, not a breach of it.
    """
    decide = SRC / "decide"
    offenders = []
    for path in _python_files(decide):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = {
            ast.get_docstring(n, clean=False)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                              ast.AsyncFunctionDef))
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                ("src.policy", "policy")
            ):
                offenders.append(f"{path.name}:{node.lineno}: imports {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(("src.policy", "policy")):
                        offenders.append(f"{path.name}:{node.lineno}: imports {alias.name}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docstrings:
                    continue
                if "policy.yaml" in node.value:
                    offenders.append(f"{path.name}:{node.lineno}: references policy.yaml")
    assert not offenders, f"decide/ must not reach into policy: {offenders}"


def test_agent_cannot_reach_latents():
    """CLAUDE.md section 9.1/9.5: src/ must never import the simulator's hidden state."""
    offenders = [
        f"{p}:{i}"
        for p in _python_files(SRC)
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"(from|import)\s+eval[\.\s]", line)
    ]
    assert not offenders, f"src/ reaches into eval/ (latent leakage risk): {offenders}"


def test_no_dialect_specific_sql_outside_trigger_definitions():
    """CLAUDE.md section 10: no Postgres-specific SQL - Razorpay runs MySQL.

    The schema must port. Checked against real string constants in the AST, skipping
    docstrings, so prose describing the rule is not mistaken for a breach of it.
    Trigger syntax is unavoidably dialect-specific and is implemented for both engines.
    """
    postgres_only = re.compile(
        r"\b(JSONB|SERIAL|BIGSERIAL|ILIKE|RETURNING|TSVECTOR|UUID_GENERATE)\b",
        re.IGNORECASE,
    )
    offenders = []
    for path in _python_files(SRC, EVAL):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docstrings:
                    continue
                if postgres_only.search(node.value):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "Postgres-specific SQL found: " + ", ".join(offenders)


def test_no_hardcoded_currency_symbols_outside_the_market_module():
    """Locale belongs in config/markets.yaml, not scattered through the decision core.

    Razorpay operates in India, Malaysia and Singapore. A rupee symbol compiled into
    the agent is a bug for two thirds of those merchants.
    """
    offenders = []
    for path in _python_files(SRC):
        if path.name in {"market.py", "models.py"}:   # the two sanctioned places
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = {
            ast.get_docstring(n, clean=False)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                              ast.AsyncFunctionDef))
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docstrings:
                    continue
                if "₹" in node.value or "RM" == node.value or "S$" in node.value:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"hardcoded currency symbol: {offenders}"
