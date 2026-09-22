from __future__ import annotations

import re
from pathlib import Path


def test_repository_sources_do_not_embed_private_deployment_values() -> None:
    root = Path(__file__).resolve().parents[1]
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in {".git", ".venv", "__pycache__"} for part in path.parts)
        and (
            path.suffix in {".py", ".md", ".toml", ".yaml", ".yml"}
            or path.name in {".gitignore", "LICENSE"}
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
