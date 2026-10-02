"""Validate release archives against the source tree without installing them."""
from __future__ import annotations

import argparse
import ast
import configparser
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_version() -> str:
    tree = ast.parse((ROOT / "src/harness_acp_mcp/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            return str(ast.literal_eval(node.value))
    raise ValueError("missing source version")


def check_metadata(payload: bytes, version: str) -> None:
    metadata = BytesParser().parsebytes(payload)
    assert metadata["Name"] == "harness-acp-mcp"
    assert metadata["Version"] == version
    assert metadata["Requires-Python"] == ">=3.11"
    assert metadata["License-Expression"] == "MIT"
    assert metadata.get_all("License-File") == ["LICENSE"]
    assert metadata.get_all("Requires-Dist") == ["mcp<2,>=1.30"]


def check_dist(directory: Path) -> None:
    version = source_version()
    wheels = list(directory.glob("*.whl"))
    sdists = list(directory.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "use a clean output directory with one wheel and sdist"
    assert wheels[0].name == f"harness_acp_mcp-{version}-py3-none-any.whl"
    assert sdists[0].name == f"harness_acp_mcp-{version}.tar.gz"
    modules = {
        f"harness_acp_mcp/{path.name}": path.read_bytes()
        for path in (ROOT / "src/harness_acp_mcp").glob("*.py")
    }
    info = f"harness_acp_mcp-{version}.dist-info"
    with zipfile.ZipFile(wheels[0]) as archive:
        expected = set(modules) | {
            f"{info}/{name}" for name in
            ("METADATA", "WHEEL", "entry_points.txt", "RECORD", "licenses/LICENSE")
        }
        assert set(archive.namelist()) == expected, "unexpected or missing wheel files"
        for name, payload in modules.items():
            assert archive.read(name) == payload, f"wheel source mismatch: {name}"
        check_metadata(archive.read(f"{info}/METADATA"), version)
        assert archive.read(f"{info}/licenses/LICENSE") == (ROOT / "LICENSE").read_bytes()
        entry_points = configparser.ConfigParser()
        entry_points.read_string(archive.read(f"{info}/entry_points.txt").decode())
        assert dict(entry_points["console_scripts"]) == {
            "harness-acp-mcp": "harness_acp_mcp.server:main",
            "harness-acp-mcp-daemon": "harness_acp_mcp.daemon:main",
        }

    expected_sources = {
        Path(name) for name in
        (".gitignore", "README.md", "LICENSE", "config.example.yaml", "pyproject.toml", "uv.lock")
    }
    for pattern in ("src/harness_acp_mcp/*.py", "tests/*.py", "tools/*.py", "docs/*.md"):
        expected_sources.update(path.relative_to(ROOT) for path in ROOT.glob(pattern))
    prefix = f"harness_acp_mcp-{version}/"
    with tarfile.open(sdists[0]) as archive:
        files = {member.name: member for member in archive.getmembers() if member.isfile()}
        expected = {prefix + path.as_posix() for path in expected_sources} | {prefix + "PKG-INFO"}
        assert set(files) == expected, "unexpected or missing sdist files"
        assert all(member.isfile() or member.isdir() for member in archive.getmembers())
        for path in expected_sources:
            stream = archive.extractfile(files[prefix + path.as_posix()])
            assert stream is not None
            assert stream.read() == (ROOT / path).read_bytes(), f"sdist source mismatch: {path}"
        stream = archive.extractfile(files[prefix + "PKG-INFO"])
        assert stream is not None
        check_metadata(stream.read(), version)
    print(f"Validated wheel and sdist for harness-acp-mcp {version}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=ROOT / "dist")
    check_dist(parser.parse_args().directory)
