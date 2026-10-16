"""Static guards that a behavioural test cannot provide: the stub accepts anything."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = sorted(p for p in ROOT.glob("*.py"))
ALLOWED_DOMOTICZ = {"domoticz_api.py", "plugin.py"}


def tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def imported(node):
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def test_only_two_modules_import_domoticz():
    for path in RUNTIME:
        for node in ast.walk(tree(path)):
            if any(n in ("Domoticz", "DomoticzEx") for n in imported(node)):
                assert path.name in ALLOWED_DOMOTICZ, path.name


def test_never_update_typename_and_never_delete():
    for path in RUNTIME:
        for node in ast.walk(tree(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "Delete", f"{path.name}: Unit.Delete() is forbidden"
                if node.func.attr == "Update":
                    keywords = [k.arg for k in node.keywords]
                    assert "TypeName" not in keywords, f"{path.name}: Update(TypeName=...)"


def test_workers_never_use_domoticz():
    for node in ast.walk(tree(ROOT / "workers.py")):
        if isinstance(node, ast.Name):
            assert node.id not in ("Domoticz", "DomoticzEx")


def test_runtime_is_standard_library_only():
    local = {p.stem for p in RUNTIME}
    allowed = local | {"DomoticzEx", "Domoticz"} | set(sys.stdlib_module_names)
    for path in RUNTIME:
        for node in ast.walk(tree(path)):
            for mod in imported(node):
                assert mod.split(".")[0] in allowed, (path.name, mod)
