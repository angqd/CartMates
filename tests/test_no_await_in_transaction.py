"""No await inside `with db.transaction(...)`.

The bot runs on one thread, so an await lets other tasks run while the transaction is still open on the shared
connection. Their BEGIN fails, or their writes join this transaction and vanish if it rolls back.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "cartmates"
BANNED = (ast.Await, ast.AsyncWith, ast.AsyncFor)
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _is_transaction(item: ast.withitem) -> bool:
    call = item.context_expr
    if not isinstance(call, ast.Call):
        return False
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr == "transaction") or (
        isinstance(f, ast.Name) and f.id == "transaction"
    )


def _banned_in(node: ast.AST):
    """Yield banned nodes under node, skipping nested scopes that don't run inside the block."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, SCOPES):
            continue
        if isinstance(child, BANNED):
            yield child
        yield from _banned_in(child)


def scan_source(source: str, name: str = "<src>") -> tuple[int, list[str]]:
    """Return (number of transaction blocks, list of 'name:line' violations)."""
    blocks, bad = 0, []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.With) and any(_is_transaction(i) for i in node.items):
            blocks += 1
            for stmt in node.body:
                if isinstance(stmt, BANNED):
                    bad.append(f"{name}:{stmt.lineno}")
                if not isinstance(stmt, SCOPES):
                    bad += [f"{name}:{n.lineno}" for n in _banned_in(stmt)]
    return blocks, bad


def test_no_await_inside_transaction_blocks():
    blocks, bad = 0, []
    for path in sorted(ROOT.rglob("*.py")):
        n, found = scan_source(path.read_text(), str(path.relative_to(ROOT.parent)))
        blocks += n
        bad += found
    assert blocks > 0, "found no transaction blocks; was db.transaction renamed?"
    assert not bad, "await inside db.transaction:\n" + "\n".join(bad)


def test_checker_catches_and_ignores_the_right_things():
    def run(body: str) -> list[str]:
        src = "async def f(db, conn):\n" + body
        return scan_source(src)[1]

    assert run("    with db.transaction(conn):\n        await x()\n") == ["<src>:3"]
    assert run("    with transaction(conn):\n        async with y():\n            pass\n") == ["<src>:3"]
    assert run("    with db.transaction(conn):\n        async for i in z():\n            pass\n") == ["<src>:3"]
    assert run("    with db.transaction(conn):\n        async def g():\n            await x()\n") == []
    assert run("    with db.transaction(conn):\n        pass\n    await x()\n") == []
