"""Keep `.env.example` in sync with the environment variables `backend/config.py` reads.

`.env.example` is the only configuration template shipped with the repository, so a
variable that is read by the code but missing from the template silently falls back to
the built-in default on every deployment that copies it. These tests compare the two
files statically, which is why they do not import `config`: importing it would make the
result depend on a developer's local `.env`.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = ROOT / "backend" / "config.py"
ENV_EXAMPLE = ROOT / ".env.example"
README = ROOT / "README.md"

READER_FUNCS = {"os.getenv", "_env_bool", "_env_int", "_env_float"}

# backend/config.py accepts these lower-case spellings as aliases of the upper-case OSS
# keys documented in .env.example. The alias itself is not a separate setting, so only
# its upper-case form has to be documented.
LEGACY_ALIASES = {"oss_access_key_id", "oss_access_key_secret", "oss_bucket", "oss_endpoint"}

_UNPARSABLE = object()
_NO_DEFAULT = object()


def _read_names_with_defaults():
    """Map every env var name read by config.py to its default expression."""
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            call_name = f"{func.value.id}.{func.attr}"
        elif isinstance(func, ast.Name):
            call_name = func.id
        else:
            continue
        if call_name not in READER_FUNCS or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        found[first.value] = node.args[1] if len(node.args) > 1 else _NO_DEFAULT
    return found


def _parse_env_example():
    """Map documented key -> raw value, ignoring blank lines and comments."""
    documented = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        documented[key.strip()] = value.strip()
    return documented


def _eval_int_expr(node):
    """Evaluate integer literals and + - * // over them, or return None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _eval_int_expr(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv)
    ):
        left, right = _eval_int_expr(node.left), _eval_int_expr(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return None if right == 0 else left // right
    return None


def _module_constant(name):
    """Read a module-level string constant such as DEFAULT_SECRET_KEY."""
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(node.value, ast.Constant):
                        return node.value.value
    return None


def test_every_env_var_read_by_config_is_documented():
    documented = _parse_env_example()
    missing = sorted(
        name
        for name in _read_names_with_defaults()
        if name not in documented and name not in LEGACY_ALIASES
    )
    assert missing == [], f"read by backend/config.py but absent from .env.example: {missing}"


def test_documented_values_match_config_defaults():
    documented = _parse_env_example()
    mismatched = []
    for name, default_node in sorted(_read_names_with_defaults().items()):
        if default_node is _NO_DEFAULT or name not in documented:
            continue
        actual = documented[name]

        if isinstance(default_node, ast.Name):
            # Default is another documented setting, e.g. MILVUS_URI -> MILVUS_LITE_URI.
            referenced = documented.get(default_node.id)
            if referenced is None:
                continue
            expected = referenced
        elif isinstance(default_node, ast.Constant) and isinstance(default_node.value, bool):
            if actual.lower() not in {"true", "false"} or actual.lower() != str(
                default_node.value
            ).lower():
                mismatched.append(f"{name}: template={actual!r} config_default={default_node.value!r}")
            continue
        elif isinstance(default_node, ast.Constant):
            expected = str(default_node.value)
        else:
            evaluated = _eval_int_expr(default_node)
            if evaluated is None:
                continue
            expected = str(evaluated)

        if actual != expected:
            mismatched.append(f"{name}: template={actual!r} config_default={expected!r}")

    assert mismatched == [], "defaults drifted from backend/config.py: " + "; ".join(mismatched)


def test_secret_key_example_is_the_public_placeholder_everywhere():
    placeholder = _module_constant("DEFAULT_SECRET_KEY")
    assert placeholder, "DEFAULT_SECRET_KEY not found in backend/config.py"

    documented = _parse_env_example()
    assert documented.get("SECRET_KEY") == placeholder, (
        "SECRET_KEY must stay at the public placeholder: it is the only value the startup "
        "guard refuses to sign with, so any other sample value reintroduces a known signing key"
    )

    readme_values = [
        line.strip().partition("=")[2].strip()
        for line in README.read_text(encoding="utf-8").splitlines()
        if line.startswith("SECRET_KEY=")
    ]
    assert readme_values, "README.md no longer shows a SECRET_KEY example"
    assert set(readme_values) == {placeholder}, (
        f"README.md SECRET_KEY example drifted from .env.example: {readme_values}"
    )
