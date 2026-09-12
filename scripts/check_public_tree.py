"""Check exactly the staged or committed source tree, without printing secrets."""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "PUBLIC_FILES.txt"
SOURCE_NAME_EXCEPTIONS = {"master_repository.py", "manual_workflow.py"}
PRIVATE_PARTS = {
    "config", "private", "downloads", "screenshots", "chrome_profiles",
    "browser_profiles", ".venv", ".git", ".codex", ".agents", "__pycache__",
}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".har", ".log", ".pem", ".key",
                    ".pfx", ".p12", ".mp4", ".mkv", ".webm", ".resolved"}
PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub credential": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "AWS access key": re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "personal home path": re.compile(rb"[A-Za-z]:[/\\]+Users[/\\]+", re.I),
    "absolute POSIX home path": re.compile(rb"/(?:home|Users)/[A-Za-z0-9_.-]+/"),
}


def git(*args, root=ROOT):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def inventory(ref=None, *, root=ROOT):
    if ref is not None:
        commit = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}", root=root).decode().strip()
        records = git("ls-tree", "-r", "-z", "--full-tree", commit, root=root)
    else:
        records = git("ls-files", "--stage", "-z", root=root)
    entries = {}
    for record in records.split(b"\0"):
        if not record:
            continue
        info, raw_path = record.split(b"\t", 1)
        parts = info.decode().split()
        mode, oid = (parts[0], parts[2]) if ref is not None else (parts[0], parts[1])
        path = raw_path.decode("utf-8")
        if ref is None and parts[2] != "0":
            raise ValueError("Resolve index conflicts before release review.")
        entries[path] = (mode, oid)
    return entries


def forbidden_path(name):
    path = PurePosixPath(name)
    return (
        path.is_absolute() or ".." in path.parts or "\\" in name
        or any(part.lower() in PRIVATE_PARTS or part.lower().endswith(" sessions") for part in path.parts)
        or path.suffix.lower() in PRIVATE_SUFFIXES
        or (name not in SOURCE_NAME_EXCEPTIONS
            and path.name.lower().startswith((".env", "master_", "final_", "global_blacklist", "manual_")))
    )


def validate_tree(entries, read_blob):
    """Return only file names and finding categories, never matching values."""
    issues = []
    if MANIFEST not in entries:
        return ["Missing explicit public source manifest."]
    try:
        manifest = read_blob(entries[MANIFEST][1]).decode("utf-8")
        paths = [line.strip() for line in manifest.splitlines() if line.strip() and not line.startswith("#")]
    except (ValueError, UnicodeError):
        return ["Invalid public source manifest."]
    if len(paths) != len(set(paths)):
        issues.append("Duplicate paths in public source manifest.")
    allowed = set(paths)
    for path in sorted(set(entries) - allowed):
        issues.append(f"{path}: not in public source manifest")
    for path in sorted(allowed - set(entries)):
        issues.append(f"{path}: manifest entry missing from tree")
    if "pyproject.toml" in entries:
        try:
            project = tomllib.loads(read_blob(entries["pyproject.toml"][1]).decode("utf-8"))
            modules = project["tool"]["setuptools"]["py-modules"]
            if not isinstance(modules, list) or not all(
                isinstance(module, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module)
                for module in modules
            ):
                raise ValueError("Invalid module inventory")
        except (KeyError, TypeError, ValueError, UnicodeError):
            issues.append("pyproject.toml: invalid runtime module inventory")
        else:
            for module in sorted(set(modules)):
                if f"{module}.py" not in entries:
                    issues.append(f"{module}.py: declared runtime module missing from tree")
    for path, (mode, oid) in sorted(entries.items()):
        if mode not in ("100644", "100755"):
            issues.append(f"{path}: symlinks and submodules are not permitted")
            continue
        if forbidden_path(path):
            issues.append(f"{path}: private/runtime path is not permitted")
        data = read_blob(oid)
        if len(data) > 8 * 1024 * 1024:
            issues.append(f"{path}: oversized source asset needs separate review")
        for label, pattern in PATTERNS.items():
            if pattern.search(data):
                issues.append(f"{path}: {label}")
    return issues


def check(ref=None, *, root=ROOT):
    entries = inventory(ref, root=root)
    issues = validate_tree(entries, lambda oid: git("cat-file", "blob", oid, root=root))
    if issues:
        raise ValueError("Public source check failed:\n" + "\n".join(issues))
    return len(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", help="Scan this commit/ref; default scans the index.")
    args = parser.parse_args()
    try:
        count = check(args.ref)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else "Cannot read the requested Git tree.")
        return 1
    print(f"Public source check passed: {count} reviewed files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
