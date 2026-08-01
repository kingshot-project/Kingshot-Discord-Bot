"""Lint only the Python lines added or changed in this PR.

Runs `ruff check` on the given files, but reports (and fails on) only findings
that land on a line added by the diff against BASE. Findings on untouched
lines of a changed file are not reported, so a legacy file with grandfathered
issues does not fail CI just because one unrelated line in it changed.

Usage: ruff_changed_lines.py BASE FILE [FILE ...]
"""
import json
import os
import re
import subprocess
import sys

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines_by_file(base: str, files: list[str]) -> dict[str, set[int]]:
    """Map each changed file to the set of line numbers added in the diff."""
    diff = subprocess.run(
        ["git", "diff", "-U0", f"{base}...HEAD", "--", *files],
        check=True, capture_output=True, text=True,
    ).stdout

    added: dict[str, set[int]] = {}
    current_file = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[len("+++ "):]
            current_file = None if path == "/dev/null" else path.removeprefix("b/")
            if current_file is not None:
                added.setdefault(current_file, set())
            continue
        match = _HUNK_HEADER.match(line)
        if match and current_file is not None:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            added[current_file].update(range(start, start + count))
    return added


def run_ruff(files: list[str]) -> list[dict]:
    """Run ruff on the given files and return its JSON findings.

    ruff exits 0 (clean) or 1 (findings present) for a normal run; both are OK here,
    the findings list (empty or not) is what matters. Exit 2 means ruff itself failed
    (bad args, internal error) — treat that as a hard error, not "zero findings".
    """
    proc = subprocess.run(
        ["ruff", "check", "--output-format=json", *files],
        capture_output=True, text=True,
    )
    if proc.returncode not in (0, 1):
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"ruff failed to run (exit {proc.returncode})")
    return json.loads(proc.stdout or "[]")


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: ruff_changed_lines.py BASE FILE [FILE ...]", file=sys.stderr)
        return 2
    base, files = sys.argv[1], sys.argv[2:]

    added = added_lines_by_file(base, files)
    findings = run_ruff(files)

    repo_root = os.getcwd()
    kept = []
    for finding in findings:
        rel_path = os.path.relpath(finding["filename"], repo_root)
        row = finding["location"]["row"]
        if row in added.get(rel_path, set()):
            kept.append((rel_path, row, finding))

    if not kept:
        print(f"ruff: {len(findings)} finding(s) in changed files, 0 on added lines. OK.")
        return 0

    kept.sort(key=lambda item: (item[0], item[1]))
    for rel_path, row, finding in kept:
        col = finding["location"]["column"]
        print(f"{rel_path}:{row}:{col}: {finding['code']} {finding['message']}")
    print(f"\n{len(kept)} ruff finding(s) on added/changed lines "
          f"(of {len(findings)} total in changed files).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
