# Release checklist

This is a Python package published as `harness-acp-mcp`; the local npm manifests are agent
tooling, not an npm distribution. Publishing is manual. CI validates packages but never uploads
them to a package index.

## 1. Prepare the release

- Keep the version in `src/harness_acp_mcp/__init__.py` (the single build-time version source).
  `0.2.0` is a final version, not a prerelease; a stable PyPI release does not require `1.0.0`.
- If that version has already been uploaded, choose a new version; PyPI does not permit replacing
  published files. Update the pinned MCP example in `README.md` when changing the version.
- Review the diff and explicitly add intended new source and test files. Never add local
  configuration, credentials, logs, agent state, or dependencies. The sdist has an explicit
  include list, and the wheel contains only the Python package and distribution metadata.
- Stop an old daemon before upgrading or changing configuration: it remains alive after the
  stdio client exits. Send SIGTERM to the PID in its `ipc.lock_path` after checking it is the
  correct process. Restarting the MCP client will start the new daemon.
- Confirm package-name availability and access to the intended PyPI project.

## 2. Test and build

```bash
uv lock --check
uv sync --locked
uv run --locked ruff check .
uv run --locked pytest -m "not real_codebuddy and not real_codex and not model"
```

The privacy test includes tracked and new, non-ignored sources. Real harness/model tests are
opt-in; run the relevant marked tests separately with an approved account before advertising
verified support. Agy mode IDs must be verified against the installed ACP server. No release
check should trigger real authentication or model calls automatically.

Use a fresh output directory to avoid accidentally uploading old artifacts:

```bash
release_dir=$(mktemp -d)
uv build --no-sources --out-dir "$release_dir"
uv run --no-project python tools/check_dist.py "$release_dir"
uvx --from twine twine check --strict "$release_dir"/*
```

Check both console scripts and MCP initialization from an isolated installation. This uses a
private temporary daemon, never launches a harness, and shuts that daemon down afterwards:

```bash
uv venv .venv-release
uv pip install --python .venv-release/bin/python "$release_dir"/*.whl
.venv-release/bin/python -I tools/smoke_install.py

uv build "$release_dir"/*.tar.gz --wheel --out-dir "$release_dir/rebuilt" --no-sources
uv pip install --python .venv-release/bin/python --reinstall --no-deps "$release_dir/rebuilt"/*.whl
.venv-release/bin/python -I tools/smoke_install.py
```

`-I` prevents the checkout or `PYTHONPATH` from hiding missing installed modules. CI repeats
these checks; require all Linux/macOS test jobs and the package job to pass before publication.

## 3. Publish deliberately

After reviewing the archives, optionally upload the **original** wheel and sdist to TestPyPI:

```bash
uv publish --publish-url https://test.pypi.org/legacy/ "$release_dir"/*.whl "$release_dir"/*.tar.gz
```

TestPyPI may not contain the runtime dependencies. For an install test, install dependencies
from PyPI first, then install the exact TestPyPI package with `--no-deps`; avoid mixing indexes
for normal dependency resolution.

When ready, upload those same validated artifacts to PyPI:

```bash
uv publish "$release_dir"/*.whl "$release_dir"/*.tar.gz
```

Use a scoped token through `UV_PUBLISH_TOKEN` or an approved trusted-publishing setup; do not
put tokens in files, command arguments, or Git. Do not upload the rebuilt test wheel as a second
release artifact. After success, tag the reviewed commit as `v<version>` and smoke-test the
pinned PyPI install from a separate environment. Keep the archives or their SHA-256 digests for
release provenance.
