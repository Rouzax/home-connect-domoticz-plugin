import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEV_ONLY = {"pytest", "ruff", "pyright", "vulture"}


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_root_modules_are_runtime_only():
    # release.yml ships every root *.py: none may import a dev tool.
    for path in ROOT.glob("*.py"):
        if path.name.startswith("."):
            continue  # dev-only dotfiles such as .vulture_whitelist.py are not shipped
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not _imported_roots(tree) & DEV_ONLY, path.name


def test_release_ships_the_notice_and_licence():
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    lines = release.splitlines()
    cp_lines = [x for x in lines if x.strip().startswith("cp *.py README.md LICENSE NOTICE ")]
    add_prefix = "git add -- *.py README.md LICENSE NOTICE"
    add_lines = [x for x in lines if x.strip().startswith(add_prefix)]
    assert len(cp_lines) == 2
    assert len(add_lines) == 1
    assert 'PKG="homeconnectex"' in release


def test_workflow_actions_are_pinned_to_full_shas():
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "uses:" in line:
                ref = line.split("uses:")[1].split("#")[0].strip().split("@")[1]
                assert len(ref) == 40, (workflow.name, line)
