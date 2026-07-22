"""Synchronous AST contract check (spec §5.1) — the same rule set CI runs.

SECURITY INVARIANT (spec §3.4): the control plane NEVER executes author code.
This module uses `ast.parse` exclusively — no compile-to-code-object, no exec,
no eval, no import of the submitted script. Importing would run top-level
statements; parsing cannot. tests/test_ast_check.py asserts this structurally.
"""
import ast

from src.contracts.limits import SCRIPT_MAX_BYTES
from src.contracts.validation_errors import ErrorCode, FieldError

HANDLE_PARAMS_MIN = 1
HANDLE_PARAMS_MAX = 2


def validate_script(script: str) -> list[FieldError]:
    # Defense-in-depth: the model boundary already caps this (AutomationIn),
    # but validate_script must be safe when called directly.
    if len(script.encode("utf-8")) > SCRIPT_MAX_BYTES:
        return [
            FieldError(
                field="script",
                code=ErrorCode.SCRIPT_TOO_LARGE,
                message=f"script exceeds {SCRIPT_MAX_BYTES} bytes.",
            )
        ]

    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return [
            FieldError(
                field="script",
                code=ErrorCode.SCRIPT_SYNTAX_ERROR,
                message=f"line {exc.lineno}: {exc.msg}",
            )
        ]

    handle = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "handle"
        ),
        None,
    )
    if handle is None:
        return [
            FieldError(
                field="script",
                code=ErrorCode.HANDLE_MISSING,
                message="script must define a top-level handle function.",
            )
        ]

    positional = len(handle.args.posonlyargs) + len(handle.args.args)
    if not HANDLE_PARAMS_MIN <= positional <= HANDLE_PARAMS_MAX:
        return [
            FieldError(
                field="script",
                code=ErrorCode.HANDLE_BAD_ARITY,
                message=f"handle must take {HANDLE_PARAMS_MIN}–{HANDLE_PARAMS_MAX} "
                f"positional parameters (got {positional}).",
            )
        ]
    return []
