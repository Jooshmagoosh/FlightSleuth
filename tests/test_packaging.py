import ast
import importlib
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "flightsleuth"
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class PackagingTest(unittest.TestCase):
    def test_installing_pulls_in_nothing_else(self):
        self.assertEqual(PROJECT["project"]["dependencies"], [])

    def test_the_command_points_at_the_real_entry_point(self):
        module, _, function = PROJECT["project"]["scripts"]["flightsleuth"].partition(":")
        self.assertTrue(callable(getattr(importlib.import_module(module), function)))

    def test_every_package_folder_is_included(self):
        (pattern,) = PROJECT["tool"]["setuptools"]["packages"]["find"]["include"]
        self.assertEqual(pattern, "flightsleuth*")
        for init in PACKAGE.rglob("__init__.py"):
            dotted = ".".join(init.parent.relative_to(ROOT).parts)
            self.assertTrue(dotted.startswith("flightsleuth"), dotted)

    def test_every_data_file_is_included(self):
        patterns = PROJECT["tool"]["setuptools"]["package-data"]["flightsleuth"]
        for path in PACKAGE.iterdir():
            if path.is_file() and path.suffix != ".py":
                self.assertTrue(any(path.match(pattern) for pattern in patterns), path.name)
        self.assertTrue((PACKAGE / "links.json").is_file())

    def test_the_core_imports_only_the_standard_library(self):
        for path in PACKAGE.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    self.assertTrue(
                        top == "flightsleuth" or top in sys.stdlib_module_names,
                        f"{path.relative_to(ROOT)} imports {name}, which is not in the standard library",
                    )


if __name__ == "__main__":
    unittest.main()
