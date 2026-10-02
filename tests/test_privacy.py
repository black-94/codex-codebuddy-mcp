from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def _source_files(root: Path) -> list[Path]:
    if (root / ".git").exists():
        # Include tracked files even if ignored, plus new publishable sources. Do not
        # scan dependencies, build artifacts, or private ignored deployment files.
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        return sorted({root / os.fsdecode(name) for name in result.stdout.split(b"\0") if name})

    # An unpacked sdist has no Git metadata. Prune generated directories there too.
    excluded = {
        ".git", ".venv", ".venv-release", "__pycache__", "node_modules", "dist", "build", ".pi",
    }
    files: list[Path] = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in excluded]
        files.extend(Path(directory) / name for name in names)
    return sorted(files)


def test_repository_sources_do_not_embed_private_deployment_values() -> None:
    root = Path(__file__).resolve().parents[1]
    candidates = [
        path
        for path in _source_files(root)
        if path.is_file()
        and (
            path.suffix in {".py", ".md", ".toml", ".yaml", ".yml", ".json"}
            or path.name in {".gitignore", "LICENSE", "uv.lock"}
        )
    ]
    forbidden_literals = [
        "/" + "Users" + "/",
        "/" + "home" + "/" + "dev" + "/",
        "black" + "94",
        "git" + "user",
        "dev" + "-box",
        "test" + "-host",
    ]
    private_address = re.compile(
        r"(?<![0-9])(?:10(?:\.[0-9]{1,3}){3}|192\.168(?:\.[0-9]{1,3}){2}|"
        r"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2})(?![0-9])"
    )
    private_home = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")
    email_address = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
    findings: list[str] = []
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for value in forbidden_literals:
            if value in text:
                findings.append(f"{path.relative_to(root)} contains forbidden literal")
        if private_address.search(text):
            findings.append(f"{path.relative_to(root)} contains a private network address")
        if private_home.search(text):
            findings.append(f"{path.relative_to(root)} contains an absolute user home path")
        for match in email_address.finditer(text):
            if not match.group(0).lower().endswith("@example.invalid"):
                findings.append(f"{path.relative_to(root)} contains an email address")

    assert findings == []


def test_source_files_respect_git_ignores_and_keep_tracked_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "tracked.md"
    tracked.write_text("tracked", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.md"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("node_modules/\ntracked.md\n", encoding="utf-8")
    dependencies = tmp_path / "node_modules"
    dependencies.mkdir()
    (dependencies / "README.md").write_text("third-party", encoding="utf-8")
    new_source = tmp_path / "new.py"
    new_source.write_text("# new source", encoding="utf-8")
    deleted = tmp_path / "deleted.py"
    deleted.touch()
    subprocess.run(["git", "add", "deleted.py"], cwd=tmp_path, check=True)
    deleted.unlink()

    files = _source_files(tmp_path)
    assert tracked in files
    assert new_source in files
    assert dependencies / "README.md" not in files
    assert [path for path in files if path.is_file()] == [
        tmp_path / ".gitignore", new_source, tracked,
    ]


def test_source_files_without_git_metadata_prune_generated_files(tmp_path: Path) -> None:
    source = tmp_path / "README.md"
    source.write_text("source", encoding="utf-8")
    for name in ("node_modules", "dist", ".venv", ".venv-release", "build", ".pi", "__pycache__"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "README.md").write_text("generated", encoding="utf-8")
    assert _source_files(tmp_path) == [source]
