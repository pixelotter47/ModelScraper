"""Export checked committed source, excluding local state and Git history."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from check_public_tree import ROOT, check, git, inventory


def export_blobs(output, entries, read_blob):
    """Copy the exact reviewed blobs; Git export attributes cannot rewrite them."""
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name, (mode, oid) in sorted(entries.items()):
            info = ZipInfo("modelscraper/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = (0o100755 if mode == "100755" else 0o100644) << 16
            archive.writestr(info, read_blob(oid))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "modelscraper-public.zip")
    args = parser.parse_args()
    commit = git("rev-parse", "--verify", "--end-of-options", f"{args.ref}^{{commit}}").decode().strip()
    count = check(commit)
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Output already exists; choose a new archive name.")
    output.parent.mkdir(parents=True, exist_ok=True)
    export_blobs(output, inventory(commit), lambda oid: git("cat-file", "blob", oid))
    print(f"Exported {count} checked files from {commit[:12]} to {output}")


if __name__ == "__main__":
    main()
