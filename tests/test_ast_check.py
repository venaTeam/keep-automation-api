"""AST contract check tests — including the never-executes-author-code invariant."""
import ast
from pathlib import Path

from src.bl.ast_check import validate_script
from src.contracts.limits import SCRIPT_MAX_BYTES
from src.contracts.validation_errors import ErrorCode


def code_of(errors):
    assert len(errors) == 1
    return errors[0].code


def test_valid_sync_handle_passes():
    assert validate_script("def handle(alert):\n    return {}\n") == []


def test_valid_async_handle_passes():
    assert validate_script("async def handle(alert, context):\n    return {}\n") == []


def test_two_positional_params_pass():
    assert validate_script("def handle(alert, context): pass") == []


def test_syntax_error_reported():
    errors = validate_script("def handle(alert:\n    pass")
    assert code_of(errors) == ErrorCode.SCRIPT_SYNTAX_ERROR.value


def test_handle_missing():
    errors = validate_script("def process(alert):\n    return {}\n")
    assert code_of(errors) == ErrorCode.HANDLE_MISSING.value


def test_handle_zero_params_rejected():
    errors = validate_script("def handle():\n    pass")
    assert code_of(errors) == ErrorCode.HANDLE_BAD_ARITY.value


def test_handle_three_params_rejected():
    errors = validate_script("def handle(a, b, c):\n    pass")
    assert code_of(errors) == ErrorCode.HANDLE_BAD_ARITY.value


def test_nested_handle_does_not_count():
    script = "def outer():\n    def handle(alert):\n        pass\n"
    errors = validate_script(script)
    assert code_of(errors) == ErrorCode.HANDLE_MISSING.value


def test_oversized_script_rejected_before_parse():
    errors = validate_script("#" * (SCRIPT_MAX_BYTES + 1))
    assert code_of(errors) == ErrorCode.SCRIPT_TOO_LARGE.value


def test_top_level_side_effect_is_never_executed(tmp_path):
    # A script whose import/exec would create a file. Validation must parse it
    # fine (it IS valid Python) and the side effect must NOT happen.
    marker = tmp_path / "pwned.txt"
    script = (
        f"open(r'{marker}', 'w').write('executed')\n"
        "def handle(alert):\n"
        "    return {}\n"
    )
    assert validate_script(script) == []
    assert not marker.exists(), "author code was EXECUTED during validation"


def test_validation_modules_contain_no_exec_paths():
    # Structural safety net: the validation path must never gain a call to
    # exec/eval/compile/__import__ — that would breach the parse-only invariant.
    forbidden = {"exec", "eval", "compile", "__import__"}
    src_dir = Path(__file__).resolve().parents[1] / "src" / "bl"
    for module in ("ast_check.py", "validation.py"):
        tree = ast.parse((src_dir / module).read_text(encoding="utf-8"))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not calls & forbidden, f"{module} calls {calls & forbidden}"
