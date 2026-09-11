"""Packaging inventory: the wheel must carry every runtime module."""

import ast
import os
import tomllib
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Every top-level module reachable from an entry point at runtime.
RUNTIME_ENTRY_POINTS = ("gui_app.py", "web_app.py")


def local_modules() -> set[str]:
    return {
        path.stem
        for path in BASE_DIR.glob("*.py")
        if not path.name.startswith("test_")
    }


def imported_top_level(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def runtime_inventory() -> set[str]:
    """Transitive closure of local modules reachable from the entry points."""
    available = local_modules()
    seen: set[str] = set()
    queue = [Path(BASE_DIR, name).stem for name in RUNTIME_ENTRY_POINTS]
    while queue:
        module = queue.pop()
        if module in seen or module not in available:
            continue
        seen.add(module)
        path = BASE_DIR / f"{module}.py"
        if not path.is_file():
            continue
        for name in imported_top_level(path):
            if name in available and name not in seen:
                queue.append(name)
    return seen


class PackagingInventoryTests(unittest.TestCase):
    def setUp(self):
        with open(BASE_DIR / "pyproject.toml", "rb") as handle:
            self.pyproject = tomllib.load(handle)
        self.declared = set(
            self.pyproject["tool"]["setuptools"]["py-modules"]
        )

    def test_every_runtime_module_is_declared(self):
        missing = sorted(runtime_inventory() - self.declared)
        self.assertEqual(
            missing,
            [],
            f"pyproject py-modules is missing runtime modules: {missing}",
        )

    def test_declared_modules_all_exist(self):
        absent = sorted(
            name
            for name in self.declared
            if not (BASE_DIR / f"{name}.py").is_file()
        )
        self.assertEqual(absent, [], f"declared but missing: {absent}")

    def test_no_test_module_is_packaged(self):
        packaged_tests = sorted(
            name for name in self.declared if name.startswith("test_")
        )
        self.assertEqual(packaged_tests, [])

    def test_entry_points_are_declared(self):
        for name in RUNTIME_ENTRY_POINTS:
            with self.subTest(entry_point=name):
                self.assertIn(Path(name).stem, self.declared)


class VendoredDistutilsShimTests(unittest.TestCase):
    """Keep the source shim local and declare setuptools for Python 3.12+."""

    def test_setuptools_is_a_runtime_dependency(self):
        from packaging.requirements import Requirement

        with open(BASE_DIR / "pyproject.toml", "rb") as handle:
            project = tomllib.load(handle)["project"]
        names = {Requirement(value).name for value in project["dependencies"]}
        self.assertIn("setuptools", names)

    SHIM_DEPENDENT_MODULES = (
        "ctb_core",
        "legacy_stripchat_family",
        "mfcscrape",
        "modelscraper_service",
        "stripchat_adapter",
        "stripchat_core",
        "web_app",
        "xhamsterlive_core",
    )

    def test_shim_exists_and_is_not_packaged(self):
        shim = BASE_DIR / "distutils" / "version.py"
        self.assertTrue(shim.is_file(), "vendored distutils shim is missing")
        with open(BASE_DIR / "pyproject.toml", "rb") as handle:
            pyproject = tomllib.load(handle)
        declared = set(pyproject["tool"]["setuptools"]["py-modules"])
        self.assertNotIn("distutils", declared)
        self.assertNotIn(
            "packages",
            pyproject["tool"]["setuptools"],
            "a top-level distutils package must not be shipped in a wheel",
        )

    def test_shim_dependent_modules_are_still_the_known_set(self):
        needs_shim = set()
        for path in BASE_DIR.glob("*.py"):
            if path.name.startswith("test_"):
                continue
            if "undetected_chromedriver" in path.read_text(encoding="utf-8"):
                needs_shim.add(path.stem)
        # Modules importing uc directly; the rest reach it transitively.
        self.assertTrue(needs_shim)
        self.assertTrue(
            needs_shim.issubset(set(self.SHIM_DEPENDENT_MODULES) | {"ctb_core"}),
            f"new module depends on the distutils shim: {sorted(needs_shim)}",
        )


if __name__ == "__main__":
    unittest.main()
