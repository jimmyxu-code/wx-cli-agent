# wx-cli-agent

用于 macOS 微信的本地读取 CLI 和 MCP 桥接层，基于 [pandorafuture/wx-cli](https://github.com/pandorafuture/wx-cli)。保留上游 Git 历史及 MIT 许可证，本分支补充账号解析、搜索和 Agent 接口。

常规 CLI 和 MCP 发布接口提供**已验证的本地读取能力**。另附独立的 [原生文本发送实验](experimental/native_send/README.md)：本机微信 4.1.13/build 269602 已通过结构化参数、原生回包及手机接收验证，测试对象是账号自己的文件传输助手。它尚未集成到常规 MCP 发送工具，也未验收批量真实发送。这里不包含微信程序、账号数据库、聊天记录、捕获密钥或私有配置。

## 验证范围

本分支的新增流程在 Apple Silicon Mac、微信 **4.1.13 / build 269602** 上验证过。其他版本、同版本其他 build 和其他机器仍需单独验证，不能据此声称全面支持“最新版”。

- 经数据库首页 HMAC 校验的逐库密钥导入；本机历史读取成功。
- 联系人、群聊、仅存在于会话表的精确账号标识查询。
- 分页聊天、时间范围、类型筛选、跨会话时间线和上下文定位。
- 原生词索引搜索，以及 `search --scan` 解码消息正文扫描。扫描搜索不依赖微信分词索引，但也只覆盖 CLI 能解码的本地消息。
- 数据库、表、行、BLOB 分块和只读 SQL；二进制值编码为 JSON 可传输格式。
- 本地已下载的图片、语音、视频和附件读取。语音处理需另装 FFmpeg；缺失或未下载的媒体不能凭空补齐。
- 真正的新消息读取和游标重复拉取已验证。游标不保证补捕旧序号的迟到消息、修改或撤回；会话 watch 可合并中间消息。
- 只读 MCP stdio 和结构化 JSON CLI。SSE 连接测试成功，但尚未验证真实新消息通过 SSE 到达。

## 构建和启动

需要 macOS arm64、Rust stable、Python 3.9+、Xcode Command Line Tools，以及微信已有的本地账号数据。常规读取不需要 GUI 自动化、辅助功能权限或关闭 SIP。

从源码克隆时先构建。已包含 `agent_bridge/bin/wx-cli` 的发行包可跳过构建，直接运行以下诊断和读取命令。

```sh
sh agent_bridge/build-agent.sh
agent_bridge/bin/wx-cli bridge --read-only call wechat_doctor '{}'
agent_bridge/bin/wx-cli bridge --read-only call wechat_accounts '{}'
agent_bridge/bin/wx-cli bridge --read-only call wechat_read_messages '{"contact":"filehelper","limit":20}'
agent_bridge/bin/wx-cli search 'example' --scan --no-server --format json
```

构建脚本将本分支二进制放到 `agent_bridge/bin/wx-cli`，桥接层不会悄悄改用其他目录中的旧版本。也可以明确设置 `WX_CLI_BINARY` 和 `WX_BRIDGE_SCRIPT`。

MCP 配置示例，将路径换为你的克隆目录的绝对路径：

```json
{
  "mcpServers": {
    "wechat-local": {
      "command": "/absolute/path/wx-cli-agent/agent_bridge/bin/wx-cli",
      "args": ["bridge", "--read-only", "mcp"]
    }
  }
}
```

只读模式不暴露发送、批准或 GUI 操作工具。使用多账号时，通过 `wechat_accounts` 返回的匿名 alias 明确选择账号。`agent_bridge/wechat-bridge` 是独立 Python 入口，读取时同样需要构建的 Rust 二进制。

## 数据访问权限和密钥

macOS 可能阻止终端或宿主 App 访问微信容器。请在系统设置中为实际运行程序开启“文件与文件夹 → 微信”访问权限。诊断中的“密钥文件存在”不等于解密成功，需要实际读取验证。

已有合法获得的密钥可沿用上游配置。新增的 `key import-capture` 会验证捕获文件中的逐库密钥，并安全合并进本地密钥库：

```sh
agent_bridge/bin/wx-cli key import-capture \
  --capture-file /private/path/keys.json \
  --db-root /absolute/path/to/your/account/db_storage
```

`agent_bridge/backends/receive/debug_copy_source` 提供实验性临时副本取钥脚本及其出处。在所测 build 上，保留 SIP、保留原版签名的 LLDB PBKDF 捕获曾成功；这并不意味着完全不改变安全设置：临时副本需要重签、调试权限，所测配置还移除了临时主程序 App Sandbox 并关闭其 Library Validation。它可能要求重新登录。详见 [实验说明](agent_bridge/README.md)。不要直接在原版上执行重签。

`key scan-trial` 是有严格身份检查的实验命令，在本机仍被 `task_for_pid` 拒绝；不作为成功取钥方案宣传。上游原版取钥命令的 SIP 要求保留，参见 [上游说明存档](docs/UPSTREAM_README.md)。

## 本地状态和限制

桥接层默认使用 `agent_bridge/.state` 存放私密缓存和游标，目录权限 0700。可通过 `WX_CLI_CACHE_ROOT` 指定解密缓存位置。原生 CLI 的密钥位置可用 `wx-cli paths --json` 查看。解密后的缓存包含明文个人数据，请自行保护。

- 只读取这台 Mac 已经保存的数据，不等于微信云端全部历史。
- 只读 SQL 有权限限制；原始表可能包含索引和内部记录，表行数不能当成聊天消息数。
- 不跳过数据库刷新、WAL 或消息分片错误后假装读取完整，也不在这种情况下推进接收游标。
- `backends/send/chatter.py` 是另一种可选适配器，只有合成 HTTP 服务测试通过。常规桥接层的能力报告尚未将独立的 `native_413` 实验作为可用发送后端。
- 本分支未附带 Accessibility 助手。常规数据库读取及诊断不依赖它。

## 测试

```sh
cargo fmt --check
cargo clippy -- -D warnings
cargo test --locked
sh agent_bridge/test.sh
```

Python 测试使用合成数据库和模拟服务，不读取你的微信账号，也不发送消息。CI 同时运行 Rust 与 Python 检查。

## 来源与许可证

上游：`pandorafuture/wx-cli`，基于 commit `2abe708f55bfe135539a385df856fdc58f97fc74`，MIT，保留原版权说明。

临时副本捕获代码改编自 [acmerfight 的公开 Gist](https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f)，原始文件摘要及 MIT 许可证保存在 `debug_copy_source/SOURCE.json` 和该目录的 `LICENSE`。这些摘要用于标注所参考的原文件，本分支文件已修改。
