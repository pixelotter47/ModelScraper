"""Regressions for accidental private data in staged/committed releases."""

import importlib.util
import sys
from pathlib import Path
from zipfile import ZipFile

MODULE = Path(__file__).parent / "scripts" / "check_public_tree.py"
spec = importlib.util.spec_from_file_location("public_tree_check", MODULE)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def tree(files, modes=None):
    files = dict(files)
    files["PUBLIC_FILES.txt"] = ("\n".join(sorted({*files, "PUBLIC_FILES.txt"})) + "\n").encode()
    entries = {path: ((modes or {}).get(path, "100644"), path) for path in files}
    return checker.validate_tree(entries, files.__getitem__)


def test_allowlist_cannot_override_private_runtime_paths():
    assert tree({"cb sessions/session 1/list.txt": b"synthetic"})
    assert tree({"config/settings.json": b"{}"})
    assert tree({"cookies.sqlite": b"synthetic"})


def test_symlink_is_rejected_even_if_manifest_lists_it():
    issues = tree({"app.py": b"../private"}, {"app.py": "120000"})
    assert any("symlink" in issue for issue in issues)


def test_secret_check_does_not_print_matching_value():
    secret = b"gh" + b"p_" + b"a" * 36
    issues = tree({"app.py": secret})
    assert any("GitHub credential" in issue for issue in issues)
    assert all(secret.decode() not in issue for issue in issues)


def test_clean_source_passes():
    assert tree({"app.py": b"print('hello')"}) == []


def test_unlisted_file_is_rejected():
    blobs = {"manifest": b"PUBLIC_FILES.txt\n", "extra": b"print('hello')"}
    issues = checker.validate_tree({"PUBLIC_FILES.txt": ("100644", "manifest"),
                                    "extra.py": ("100644", "extra")}, blobs.__getitem__)
    assert any("not in public source manifest" in issue for issue in issues)


def test_archive_preserves_checked_commit_blobs_despite_export_attributes(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "check_public_tree", checker)
    export_spec = importlib.util.spec_from_file_location(
        "public_archive_export", MODULE.parent / "build_public_archive.py"
    )
    exporter = importlib.util.module_from_spec(export_spec)
    export_spec.loader.exec_module(exporter)

    def git(*args):
        return checker.git(*args, root=tmp_path)

    git("init", "-q")
    files = {
        ".gitattributes": b"report.md export-subst\nLICENSE export-ignore\n",
        "report.md": b"Author placeholder: $Format:%ae$\n",
        "LICENSE": b"Synthetic license fixture\n",
    }
    files["PUBLIC_FILES.txt"] = ("\n".join(sorted({*files, "PUBLIC_FILES.txt"})) + "\n").encode()
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    git("add", ".")
    git("-c", "user.name=Synthetic Author", "-c", "user.email=synthetic-author@example.invalid",
        "commit", "-qm", "Synthetic publication fixture")
    (tmp_path / "report.md").write_text("Unreviewed working directory value", encoding="utf-8")
    (tmp_path / "staged-only.py").write_text("Uncommitted source", encoding="utf-8")
    git("add", "staged-only.py")
    assert checker.check("HEAD", root=tmp_path) == len(files)
    entries = checker.inventory("HEAD", root=tmp_path)
    output = tmp_path / "checked.zip"
    exporter.export_blobs(output, entries, lambda oid: git("cat-file", "blob", oid))
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {"modelscraper/" + name for name in files}
        for name, data in files.items():
            assert archive.read("modelscraper/" + name) == data
        assert b"synthetic-author@example.invalid" not in archive.read("modelscraper/report.md")
