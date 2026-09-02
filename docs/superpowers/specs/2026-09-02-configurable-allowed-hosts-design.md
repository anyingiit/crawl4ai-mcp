# Configurable Allowed Hosts — Design

日期:2026-09-02
状态:已批准(用户逐节确认)

## 背景与问题

`src/crawl4ai_mcp/__main__.py` 的 `mcp.run(allowed_hosts=[...])` 中硬编码了单台机器的
Tailscale FQDN(`instance-20250526-0820.taila20d2.ts.net` 及带 `:11237` 端口的变体)。
这是把单机部署特例塞进通用代码,使其他用户无法在不改源码的情况下配置自己的
反向代理主机名。

项目既有约定:
- 非机密配置在 `config.toml`(repo 只追踪 `config.example.toml`,`config.toml` 本身不追踪)。
- 机密在 `.env`,经 `config.py` 的 `SECRET_ENV_VARS` 白名单注入。
- `bind_host` 由 `loopback_only` 校验器强制 `127.0.0.1`;远程访问按设计必须经
  反向代理(如 tailscale serve),代理会带自己的 `Host` 头。

已验证的底层能力(fastmcp `3.4.x`,`server/http.py`):
- `HostOriginGuardMiddleware` 的 `_host_matches` 使用 `fnmatchcase(host, pattern)`,
  **原生支持 glob 通配**(`*`、`?`、`[seq]`),`"*"` 全匹配。
- 匹配前 `_normalize_host` 会**剥离端口**(`host.count(":")==1` 时取冒号前部分),
  因此配置项只需主机名模式,无需 `:11237` 这类端口条目(现有硬编码中带端口的
  条目实际是冗余)。

## 目标

- 移除硬编码 FQDN,允许用户通过 `config.toml` 注入额外的允许主机名(含通配)。
- 防止二次开发者误提交本地配置(`.gitignore` 保护)。
- 不引入新模块,不自实现主机匹配逻辑,零配置用户行为不变。

## 非目标(YAGNI)

- 不做纯环境变量注入(FQDN 非机密,走 `.env` 违反项目分层约定)。
- 不做用户级配置路径(`~/.config/...`,引入第二配置来源)。
- 不改变 `loopback_only` 绑定语义。
- 不自行实现通配匹配(直接透传 fastmcp 的 `fnmatchcase`)。

## 架构

配置透传:`config.toml` 新增非机密字段 → `AppConfig` 模型 → `__main__.py` 调用
`mcp.run()` 时把用户条目附加到内置 localhost 条目之后 → fastmcp
`HostOriginGuardMiddleware` 用原生 `fnmatchcase` 完成匹配。匹配、端口归一化全部
交给 fastmcp;本项目只负责"安全地把用户配置传进去"。

## 组件

| 文件 | 改动 |
|------|------|
| `src/crawl4ai_mcp/config.py` | `AppConfig` 加 `extra_allowed_hosts: list[str] = []`;`field_validator` 剥离每个条目首尾空白、拒绝剥离后为空的条目、对 `"*"` 记 warning 日志(不阻止)。非机密,不进 `SECRET_ENV_VARS`。 |
| `src/crawl4ai_mcp/__main__.py` | 删除 2 行硬编码 FQDN;`allowed_hosts = [f"{config.bind_host}:{config.bind_port}", f"localhost:{config.bind_port}", *config.extra_allowed_hosts]`(内置条目保留端口,fastmcp 会归一化剥离)。 |
| `.gitignore` | 加一行 `config.toml`。 |
| `config.example.toml` | 加注释示例 `# extra_allowed_hosts = ["*.ts.net"]`。 |

`docs/operations.md` 当前无 Tailscale/allowed_hosts 内容(相关内容只在未合并的
CHANGELOG.md 中),本设计不改 operations.md。

## 数据流

```
config.toml ──tomllib──> load_config() ──> AppConfig.extra_allowed_hosts
                                                    │
.env ──(仅机密,不经此字段)─────────────────────────┤
                                                    ▼
             __main__.py: allowed_hosts = 内置 localhost 条目 + 用户条目
                                                    ▼
             fastmcp HostOriginGuardMiddleware
                 _normalize_host(剥端口) → fnmatchcase(host, pattern)
                                                    ▼
                                     匹配 → 放行 / 不匹配 → 421
```

## 错误处理

- **空串/纯空白条目**:`field_validator` 剥离后为空 → 抛 `ValueError`,启动即失败
  (fail fast),错误信息指明是哪个条目。
- **`"*"` 全匹配**:记 warning("disables host origin protection"),不阻止——fastmcp
  本身允许,尊重用户显式选择。
- **无 `config.toml`**:`extra_allowed_hosts` 默认为 `[]`,行为与当前 localhost-only
  完全一致,向后兼容。
- **不合法主机名格式**:不额外校验。fastmcp 的 `fnmatchcase` 对任意字符串安全,
  最坏情况只是不匹配 → 421(fail-closed)。

## 测试

`tests/test_config.py` 追加:
- 默认 `extra_allowed_hosts == []`。
- 从 toml 加载列表字段。
- 空串/纯空白条目 → `ValidationError`。
- 条目首尾空白被剥离(`"  *.ts.net  "` → `"*.ts.net"`)。

`__main__.py` 集成测试(若已有 `run_server` 测试则追加,否则新增):
- `config.extra_allowed_hosts = ["*.ts.net"]` 时,`mcp.run` 收到的 `allowed_hosts`
  包含该条目,且**不含**任何硬编码 FQDN。
- 默认配置下 `allowed_hosts` 只有内置 localhost 条目。

## 部署迁移(本机)

本机 `config.toml` 当前不存在;实施后在本机创建 `config.toml`,内容包含:

```toml
extra_allowed_hosts = ["instance-20250526-0820.taila20d2.ts.net"]
```

(单条即可;`:11237` 端口变体因 fastmcp 端口归一化而冗余,不需要。)

`.gitignore` 加 `config.toml` 后,该文件不会被 `git status` 显示,也不会被提交。

## 防误提交

`.gitignore` 新增 `config.toml` 条目。`config.example.toml` 保持被追踪,作为模板。
用户照文档 "copy to config.toml" 后,`git status` 不再显示它,`git add .` 不会带入。
