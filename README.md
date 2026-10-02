# harness-acp-mcp

`harness-acp-mcp` exposes local or remote Agent Client Protocol harnesses to an MCP client.
The MCP process is a thin stdio client; one per-user daemon owns all live sessions.

Supported harness identifiers are `codebuddy`, `agy`, and `codex`. Every session requires a
working directory and model ID. Harness processes are supervised in dedicated process groups, so
closing a session also closes supported child and background processes.

## Install and run

Requires Python 3.11+ on **Linux or macOS**. Windows is not supported: the daemon uses Unix
sockets, POSIX file locks, and process groups. Install and authenticate the selected ACP harness
separately (`codebuddy`, `agy_acp_server`, or `codex-acp`); these executables are not bundled.
SSH and Docker are only needed for sessions that use them.

Once the package is published to PyPI, run it without a checkout:

```bash
uvx harness-acp-mcp
```

Or install a persistent command:

```bash
uv tool install harness-acp-mcp
harness-acp-mcp
# Alternatively: python -m pip install harness-acp-mcp
```

For development from this repository:

```bash
uv sync --locked
uv run harness-acp-mcp
```

The server speaks MCP over stdio; it is normally started by an MCP client, not used as an
interactive terminal command. The thin client starts `harness-acp-mcp-daemon` automatically. Running the daemon command directly
is useful for diagnostics; the per-user lock prevents a second daemon from starting.

Codex MCP configuration:

```toml
[mcp_servers.harness_acp]
command = "uv"
args = ["tool", "run", "harness-acp-mcp==0.2.0"]
```

Pin the version to make client launches reproducible. For a source checkout, use
`args = ["run", "--project", "<bridge-directory>", "harness-acp-mcp"]` instead.
Ensure the harness executables are on the MCP client's `PATH` (GUI clients may have a different
`PATH` from your shell), or set their absolute paths in `launch` configuration.

Configuration is optional; defaults work for local sessions. Copy `config.example.yaml` to
`$XDG_CONFIG_HOME/harness-acp-mcp/config.yaml` (default `~/.config/harness-acp-mcp/config.yaml`) or set
`HARNESS_ACP_MCP_CONFIG` to a local YAML file. Local configuration, logs, sockets, process files,
and the authentication-rate database must not be committed.

## Tools

- `create_session`: launch `codebuddy`, `agy`, or `codex` locally or remotely, directly or in Docker.
  Docker image, directory mounts, published ports, and host-network use are per-call options.
- `authenticate`: run one advertised ACP authentication method.
- `get_user_info`: return a reliable login boolean and optional account details.
- `set_model`: switch the required session model between turns.
- `prompt`: run a prompt and prefer MCP elicitation for permission and information requests.
- `respond_interaction`: compatibility response when the MCP client lacks elicitation.
- `cancel_turn`: stop the current turn without closing the session.
- `close_session`: close the supervisor and complete harness process group.

Local session example:

```json
{
  "harness": "codex",
  "cwd": "<project-directory>",
  "model_id": "<model-id>",
  "target": "local",
  "runtime": "direct",
  "permission_mode": "auto"
}
```

For a remote session, set `target` to `remote` and supply `remote_host` as an SSH alias or target.
SSH authentication is delegated to the installed SSH client, its agent, and user-owned SSH
configuration. The remote login shell must support non-interactive job control (for example Bash);
Linux `/bin/sh` implementations such as dash do not meet this requirement. For Docker, set `runtime` to `docker` and pass an explicit `docker_image` on the
call. The image, directory mounts, published ports, and host-network choice are per-call options and
are never read from the daemon YAML file. The daemon starts the image as a keep-alive container and
runs the configured harness over `docker exec -i` stdio. Docker and SSH must be available on the
selected host. Configure `launch.<harness>_command` if its executable differs from the default;
callers cannot supply commands, arguments, SSH options, or environment values.

With `runtime: "docker"`, `cwd` is the path **inside the container** used as the harness working
directory. It must be an absolute container path, is never interpreted as a host directory, and is
never bind-mounted; only explicit `mounts` publish a host directory into the container. If the
directory does not already exist in the image, the daemon creates it inside the container
(`mkdir -p`) before starting the harness. This applies to both newly created and reused containers
and only ever affects the container filesystem, so a container working directory may legitimately
differ from every host path. Direct (`runtime: "direct"`) sessions keep their normal host
working-directory semantics.

Docker options, all validated before launch:

- `docker_image` (string, required when creating a new container): the image to run.
- `mounts` (array): `{"source": "<host-path>", "target": "<container-path>", "read_only": <bool>}`
  entries. `source` and `target` must be absolute paths without commas; `read_only` defaults to
  `false`; two entries may not share the same `target`.
- `ports` (array): `{"host_ip": "<bind-address>"|null, "host_port": <1-65535>,
  "container_port": <1-65535>, "protocol": "tcp"|"udp"}` entries. `protocol` defaults to `tcp`;
  `host_ip` is optional and must be a valid IPv4 or IPv6 address. An IPv6 `host_ip` is bracketed as
  `[addr]:host:container` in the Docker argument vector.
- `host_network` (bool, default `false`): run with `--network host`. It cannot be combined with
  `ports`.
- `container_policy` (`"remove"` or `"keep"`, default `"remove"`).
- `docker_id` (string): reuse an existing container instead of creating one.

Docker-only options are rejected when `runtime` is `direct`. The daemon builds the `docker run`
argument vector directly from these validated values and never concatenates caller input into a
shell.

Docker sessions default to `container_policy: "remove"`, which removes the container on close.
Use `container_policy: "keep"` to stop it without deleting its filesystem. Only this policy returns
`docker_id`, the full Docker container ID. To reuse it, pass that ID as `docker_id` in a new Docker
`create_session` call. A retained container already fixes its image, mounts, published ports, and
network, so `docker_image`, `mounts`, `ports`, and `host_network` must be omitted in that call; the
daemon checks the container with `docker inspect` before starting it, reading back the container's
image reference, bind mounts, published ports, and network mode so the result can report them, then
launches a fresh harness process. No bridge record lookup is needed. Reused containers default to
`keep`; set
`container_policy: "remove"` to delete one after use. Only one active session should use a retained
container at a time.

For both a newly created and a reused Docker container the result's `launch_info` reports the
Docker configuration. `docker_image` is reported whenever it has a value and `host_network` is
always reported (it is a meaningful boolean, so `false` is kept). A container with no bind mounts or
no published ports omits `mounts` and `ports` instead of reporting empty lists, following the same
empty-field omission applied to every other result. A newly created container echoes the
creation-time request values so a caller can verify what was applied. A reused container reports the
values read back from `docker inspect`: the configured image reference, its bind mounts (each with
`read_only`), its published ports (including IPv4 or IPv6 host addresses), and `host_network` derived
from the container's network mode, rather than defaults. A reused container still reports
`launch_info.reused_container: true`, and its `docker_id` is returned when the container is kept.

Only the image, bind mounts, port bindings, and network mode are projected out of `docker inspect`
for these fields. The full inspect payload and the container's environment are never included in a
result or the private log, consistent with the authentication privacy notes below.

`permission_mode` accepts `read`, `edit`, `auto`, and `bypass`. CodeBuddy maps these to `plan`,
`acceptEdits`, `auto`, and `bypassPermissions`. Codex maps them to its ACP `read-only`, `agent`,
`agent`, and `agent-full-access` modes, respectively; Codex has no separate edit-only preset.
For Agy, `auto` uses its default mode. To offer `read`, `edit`, or `bypass`, the administrator
must set the corresponding `launch.agy_<mode>_mode_id` to a mode ID supported by that Agy ACP
server. The daemon applies it with ACP `session/set_mode`; an unconfigured choice fails before
launch. Agy's real mode IDs and behavior require verification against the installed server.

`buffers.max_read_bytes` and `buffers.max_output_bytes` are configured only in the daemon YAML
file, never as MCP tool parameters. Their defaults are `100MiB` and `128KiB`. `create_session`,
`prompt`, and `respond_interaction` reject those names if a caller still sends them.

`create_session` returns a bridge `session_id` for live MCP calls and a `harness_session_id` from
ACP. To restore the harness's own session later, pass that `harness_session_id` as
`resume_session_id` in a new `create_session` call; the bridge requests ACP `session/load`.
The caller still supplies `harness`, `cwd`, and `model_id`. `resume_session_id` is independent of
`docker_id`: one restores ACP history, and the other selects a retained container. A supplied
`resume_session_id` must be a non-empty string.

`create_session` runs under one overall budget of three startup timeouts plus 30 seconds, covering
Docker `inspect`, container startup, and ACP initialization in sequence. The daemon aborts and
cleans up the session within that budget, and the MCP call waits for the budget plus a short IPC
margin, so a client-side timeout cannot leave an orphan session behind.

The response also includes `output_log_path`, a private JSON-lines file with a unique UUID name
inside a per-daemon subdirectory of the system temporary directory. That subdirectory is namespaced
by the daemon's `ipc.lock_path`, so two daemons sharing the temporary directory never sweep each
other's logs. Each received harness stdout JSON message and stderr line is appended as a timestamped
record. The file keeps at most 100MiB by removing the oldest complete records. It remains after
the session closes for manual inspection. Preserved logs are deleted once
`daemon.output_log_retention_seconds` has elapsed since their last write (default `604800`, one
week); set it to `0` to keep them indefinitely. Logs of live sessions are never swept. This file
is separate from the temporary result file returned when an MCP response exceeds
`max_output_bytes`.

If an ACP stdout JSON line exceeds `buffers.max_read_bytes`, only that line is discarded; reading
continues with the following lines. The daemon reports `status: "error"`, and `prompt`,
`respond_interaction`, and `create_session` surface it to the MCP client as a tool error
(`isError: true`) whose structured content keeps the English JSON error: `code` is
`acp_json_line_too_large`, together with `max_read_bytes`, `discarded_line_only: true`,
`turn_cancelled` (`best_effort` when a turn was active, otherwise `not_needed`), and a `warning`
string.

Discarding is bounded so a harness that keeps emitting over-limit lines cannot spin the reader
forever. A single line is discarded while its own length stays at or below eight times
`max_read_bytes`; the discard budget counts the whole line, not only the part above `max_read_bytes`,
and at most 32 consecutive over-limit lines are discarded. If either bound is exceeded, the
transport is closed instead of discarding indefinitely; within the bounds only the offending line is
dropped.

Cancellation of the active turn is best effort, not a guarantee. Because the oversized line was
discarded before parsing, its JSON-RPC request ID is unavailable, so an oversized permission or
information request cannot be answered directly. A harness that does not honor `session/cancel`
may therefore remain waiting for that reply; call `close_session` for such a session. The behavior
cannot promise that a harness never hangs.

Model identifiers remain harness-specific. In particular, current `codex-acp` releases may require
the `model[effort]` form; callers must pass the exact identifier accepted by the selected harness.

## Authentication and interactions

An unauthenticated `create_session` returns `authentication_required` while retaining the initialized
ACP process. `authenticate` is serialized per harness and target. Frequency limiting is configurable
and may be disabled, but same-target serialization always remains enabled to prevent concurrent
credential-state writes. Authentication timeout closes the entire session process group.

Public results never include harness credentials. The `user` object is rebuilt from a small
whitelist of identity fields (`userId`/`username`/`name`/`displayName`/`nickname`/`email`), so
fields such as `token`, `accessToken`, or `refreshToken` returned by a harness are dropped while the
`authenticated` boolean still reflects the real login state. The private JSON-lines output log
replaces values under credential-looking keys with `"[redacted]"`, so an authentication response's
token is not persisted to disk. Credential-looking values are masked the same way in error messages,
including the captured harness stderr tail surfaced by `create_session`, `get_user_info`, or
`authenticate` failures.

When the MCP client supports elicitation, permission and structured information requests are shown
through elicitation even if the harness itself uses a two-message request/response protocol. Without
elicitation, `prompt` or `authenticate` returns `interaction_required`; the caller supplies the
answer with `respond_interaction`. This status means an application-level request/response pause,
not two-factor authentication. The bridge never silently approves a permission request.

## Process lifecycle

Local harnesses run in their own process groups. A separate session supervisor watches its daemon
control pipe; daemon loss, harness exit, explicit close, or timeout triggers group-wide TERM followed
by KILL. SSH wrappers enable shell job control, record the remote process-group ID, install exit
traps, and perform a second cleanup connection when needed. Docker sessions stop or remove the named
container according to `container_policy`.

Options that intentionally detach a harness, including background and terminal-multiplexer modes,
are rejected. A third-party process that deliberately creates a new session outside the managed
group is outside the supported lifecycle contract.

## Tests

```bash
uv run pytest -m "not real_codebuddy and not real_codex and not model"
uv run ruff check .
```

CI runs these non-account tests on Linux and macOS and checks built distributions separately.
See [the release checklist](docs/releasing.md) for package validation and publication steps.

Real-harness tests are separately marked because interactive authentication, model prompts, and
approved commands can have account or machine side effects. No test performs an account-usage reset.
The real tests take model identifiers from `HARNESS_ACP_REAL_CODEBUDDY_MODEL` and
`HARNESS_ACP_REAL_CODEX_MODEL`; model and permission turns additionally require their explicit
`RUN_HARNESS_ACP_REAL_MODEL_TEST` or `RUN_HARNESS_ACP_REAL_PERMISSION_TEST` opt-in flag.

## Repository privacy

Tracked files must not contain private deployment configuration, real account names, machine names,
network addresses, home-directory paths, or personal project and file names. Documentation uses
placeholders; tests use temporary paths and generated identifiers. Run the tracked-content privacy
scan before publishing changes (`uv run pytest tests/test_privacy.py`). It checks tracked files
and new, non-ignored sources, not local dependencies or ignored deployment state.
