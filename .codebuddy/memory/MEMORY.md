# MEMORY

## 项目：codex-codebuddy-mcp（/home/share/git/codex-codebuddy-mcp）

- 定位：MCP server，把 Codex/任意 MCP 客户端接到 CodeBuddy Code 的 ACP（stdio）服务。
- 构建：hatchling；`uv build` 出 wheel+sdist；本地安装用 `uv tool install <whl|.>`，CLI 落在 `~/.local/bin`。
- 桥接用 `codebuddy_command` + `codebuddy_args` 启动 ACP 对端，随后自行追加 `--model/--permission-mode/--acp/--acp-transport stdio`；
  因此 `codebuddy_args` 里禁止出现 `--model`、`--permission-mode`、`--acp`、`-p`、`--print` 等（validate_config 会拒绝）。
  → 用 `docker` 作为 `codebuddy_command`、把 `docker run ... codebuddy` 放进 `codebuddy_args` 即可让 ACP 跑在容器里。
- 连接的容器：`agent-harness:2`（Ubuntu 24.04 + node + codebuddy-code 2.155.0 + codex，用户 master，HOME=/home/master，工作目录 /workspace）。
- 容器内 codebuddy 登录态：镜像不预置。凭证目录挂载点是容器 `/home/master/.local/share/CodeBuddyExtension`；
  宿主机导出的凭证在 `/srv/agent/auth/codebuddy/codebuddy1`（含 `Data/Public/auth/Tencent-Cloud.coding-copilot.info`）。
  容器内凭证文件含 accessToken/refreshToken，可自动续期（挂载需可写）。
- 容器网络：默认 bridge 会绕过宿主机 xray 代理，访问外部 API 不稳定，跑 ACP 时建议加 `--network=host`。
- 跨机/容器分发凭证的目录约定：`/srv/agent/auth/<tool>/<name>`（codex 用 `/srv/agent/auth/codex/codex0` → 容器 `~/.codex`）。
- 参考脚本：`examples/docker_codebuddy_hi.py`（PEP 723 + mcp stdio 客户端，跑通 docker ACP + 发一条 prompt）。

## 用户偏好

- 软件安装位置：图形/专有应用 `~/Applications/`，CLI/软链 `~/.local/bin`，uv 数据 `~/.local/share/uv/`，
  缓存 `~/.cache/uv/`，配置 `~/.config/`；不要装到 CodeBuddy 工作区；安装包解压后及时清理。
