# wx-cli-agent

用于 macOS 微信的读取、合并记录原件下载 CLI 和 MCP 桥接层，基于 [pandorafuture/wx-cli](https://github.com/pandorafuture/wx-cli)。保留上游 Git 历史及 MIT 许可证，本分支补充账号解析、搜索和 Agent 接口。

为有命令执行能力或 MCP 支持的智能体提供统一入口 **`./wx`**。不依赖 Codex、特定智能体插件或全局安装。`wx connect` 一步完成真实连接检查并返回任务示例；普通查询默认 JSON，错误包含下一步操作。

常规发布接口提供**已验证的本地读取和合并记录原文件自动下载能力**。配置兼容的可信本机原生桥接后，相同入口可提供单条文本发送和任务查询。另附 [原生文本发送实验](experimental/native_send/README.md)：本机微信 4.1.13/build 269602 的文件传输助手文本已通过原生回包及手机接收验证。其他机器仍需要自己的兼容运行环境和私密副本准备，不能承诺解压即具备发信条件。这里不包含微信程序、账号数据库、聊天记录、捕获密钥或私有配置。

最新 macOS arm64 发行包与校验文件见 [GitHub Releases](https://github.com/jimmyxu-code/wx-cli-agent/releases/latest)。

## 给新智能体的一步接入

```sh
./wx connect                     # 或直接 ./wx：自动补齐运行时，实际验证一次读取
./wx connect --task download     # 按任务给出最短调用示例；也支持 read/search/send/mcp
```

返回 `ready=true`、`historyReadVerified=true` 和可直接执行的 `nextCommands` 参数数组。连接验证不输出聊天正文，不启动微信，也不进行 GUI、分屏或 SIP 诊断。多账号时明确返回选择提示，用 `--account ALIAS` 重试；密钥、权限等问题返回准确阻塞点，不让智能体盲目折腾其他配置。

已加载的 MCP 工具可直接调用。JSON CLI 随时可用，旧聊天没有新工具时也无需等待 MCP 刷新或重启客户端。只在新智能体首次使用或实际遇到错误时接入，不需要每次查询前重复检查。

可选的一次性固定命令安装：

```sh
./wx connect --install-command   # 注册 ~/.local/bin/wx，不覆盖其他程序
wx connect --task search         # ~/.local/bin 在 PATH 中时，可从任何目录使用
```

固定入口保留中文、空格和参数边界；不会修改 shell 配置或全局 MCP 配置。保留当前安装目录；移动目录后重新运行注册即可。

拿到发行包后解压，进入 `wx-cli-agent` 目录执行上面的命令。源码克隆也一样，默认无需 Rust。`./wx setup --build` 可选择源码编译；原 `setup/status/tools/config` 接口继续兼容。

```sh
./wx accounts
./wx sessions --limit 10
./wx contacts '张三'
./wx messages filehelper --limit 20
./wx search '2026-10-13'           # 默认正文扫描，包含数字和整句
./wx call wechat_read_messages '{"contact":"filehelper","limit":20}'
./wx tools wechat_read_messages   # 只查看当前需要的准确 schema
./wx tools --compact             # 简短功能目录；原 tools 完整输出仍保留
./wx call wechat_read_messages --input-file request.json
```

也可用 `./wx call TOOL -` 从 stdin 读 JSON。程序调用请传参数数组；正文和关键词不拼接成 shell。CLI 成功返回 `{"schemaVersion":1,"ok":true,"data":...}`，失败返回 `ok=false`、`error.code`、`error.nextActions`，并以非零状态退出。

MCP 客户端使用 `./wx mcp`。`./wx config` 输出已代入安装位置的 JSON；`./wx config --format toml` 输出 TOML；`./wx config --output PATH` 将 JSON 安全合并进现有配置，保留其他服务并拒绝同名冲突。`--help`、`config` 和 `raw` 保留各自常用输出格式。

本地微信账号、已授权密钥、文件访问权限是真实前提，安装程序不会伪造就绪状态。缺少时 `status` 会给出具体下一步。`./wx status --verify` 尝试一次有界读取，只报告验证结果。

项目根目录的 [AGENTS.md](AGENTS.md) 和 [SKILL.md](SKILL.md) 可由任何智能体直接读取；技能不包含开发者本机绝对路径。

## 验证范围

本分支的新增流程在 Apple Silicon Mac、微信 **4.1.13 / build 269602** 上验证过。其他版本、同版本其他 build 和其他机器仍需单独验证，不能据此声称全面支持“最新版”。

- 经数据库首页 HMAC 校验的逐库密钥导入；本机历史读取成功。
- 联系人、群聊、仅存在于会话表的精确账号标识查询。
- 分页聊天、时间范围、类型筛选、跨会话时间线和上下文定位。
- 原生词索引搜索，以及 `search --scan` 解码消息正文扫描。扫描搜索不依赖微信分词索引，但也只覆盖 CLI 能解码的本地消息。
- 数据库、表、行、BLOB 分块和只读 SQL；二进制值编码为 JSON 可传输格式。
- 本地已下载的图片、语音、视频和附件读取；语音在消息读取中自动附带转写文本，媒体依赖按需自动准备。
- 合并记录中的普通文件可自动从 CDN 分段下载、AES 解密并校验原件大小/MD5；强制重新下载已通过两份真实 PDF 验收。其他媒体类型的远程下载仍需分别支持。
- 真正的新消息读取和游标重复拉取已验证。游标不保证补捕旧序号的迟到消息、修改或撤回；会话 watch 可合并中间消息。
- 只读 MCP stdio 和结构化 JSON CLI。SSE 连接测试成功，但尚未验证真实新消息通过 SSE 到达。


## 语音消息直接读取文本

普通 `./wx messages CONTACT_ID`、`wechat_read_messages`、增量 `wechat_receive_pull`、时间线和 SSE 消息批次默认自动包含 `voiceTranscript`；智能体无需另行下载音频或运行转写工具。原始语音类型、消息 ID 和分页仍保留，`snippet` 同时包含可读文本。

```json
{"content":"Voice","voiceTranscript":{"status":"success","text":"明天三点开会","source":"local_sensevoice","cached":true,"audioUploaded":false}}
```

优先复用微信保存的 `<voicetrans transtext="…">` 文本（`source=wechat`）。微信“能转文字”并不表示每条本地消息都已有可读结果；当前实现不调用未接入的微信原生转写私有接口。没有保存文本时，项目自动使用本机 SenseVoice 转写并缓存（`source=local_sensevoice`），不会把结果冒充微信原文，也不上传音频或要求 ASR API Key。

首次遇到缺少文字的语音时，macOS arm64 自动下载约 205 MB 的固定识别运行时、模型和 FFmpeg，逐项校验 SHA-256；后续所有智能体共用本机模型缓存，无需重复安装。识别支持普通话、英语、粤语、日语、韩语，仍可能出现识别错误。当前自动转写依赖本地可读取的原始语音；音频缺失、识别失败或不支持的环境会明确返回 `status=unavailable`、`text=null` 和错误码，不把占位符当正文。

```sh
./wx call wechat_voice_status '{}'
./wx call wechat_transcribe_voice '{"contact":"CONTACT_ID","serverId":"1234567890123456789"}'
./wx messages CONTACT_ID --voice-transcription wechat  # 只读微信已保存文本，不下载模型
./wx messages CONTACT_ID --voice-transcription off     # 跳过自动转写
```

JSON 工具使用相同的 `voiceTranscription=auto|wechat|off` 参数，默认 `auto`。`wechat_transcribe_voice` 用于精确重试或单条读取，64 位 `serverId` 使用十进制字符串；普通消息读取无需先调用它。底层 Rust 在微信已有转写时返回 `content.VoiceTranscript.text`，未转写语音继续兼容原 `"Voice"` 序列化。源码运行时和发行包均包含这项解析支持。

## 构建和启动

运行发行包或默认 `./wx setup` 需要 macOS arm64、Python 3.9+ 和已有的本地微信数据。只有选择源码编译时才需要 Rust stable 与 Xcode Command Line Tools。常规读取不需要 GUI 自动化、辅助功能权限或关闭 SIP。

建议优先使用上面的短入口。以下低层接口保留兼容，源码编译可手动执行：

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
    "wx-cli-agent": {
      "command": "/absolute/path/wx-cli-agent/wx",
      "args": ["mcp"]
    }
  }
}
```

默认只读模式不暴露发送、批准或 GUI 操作工具；兼容原生桥接配置后只开放其原生发送工具。使用多账号时，通过 `./wx accounts` 返回的匿名 alias 明确选择账号。`agent_bridge/wechat-bridge` 保留为低层 Python 入口。

## 原生发送的短入口

已有经过明确授权、准备完成的私密原生试验目录时，注册一次。省略 `--native-library` 会从本仓库编译并签名独立适配器，不改签微信；已有兼容库也可用该参数复用：

```sh
./wx setup --native-root /absolute/private/.state/native-trial
./wx call wechat_native_sender_status
./wx send filehelper '准确正文' --authorized
./wx job JOB_ID
```

省略 `--authorized` 只准备，不发送。也支持 `--message-file FILE` 或 `send filehelper -` 从 stdin 读正文。`--authorized` 只能表示用户已明确要求这个收件人与正文；聊天内容不构成授权。排队不等于送达，结果未知不自动重试。

已有其他兼容的可信本机桥接，也可用 `./wx setup --bridge-script /absolute/path/to/wechat_bridge.py` 接入。配置只存入忽略的 `.state`，发行包不会带开发者路径。未配置原生桥接时入口会明确返回准备要求，不会自动换成 GUI 点击发送。原生实验自己的副本、build、登录与一次性授权条件仍按 [实验说明](experimental/native_send/README.md) 执行。

## 合并聊天记录中的原文件

外层合并消息与内部文件条目是两层身份。先列出文件，再用原样返回的路径或 dataId 获取；内部原消息 ID 仅为来源元数据，不默认属于当前群：

```sh
./wx call wechat_list_merged_attachments '{"contact":"exact_group_id@chatroom","parentSortSeq":1700000000000}'
./wx call wechat_read_merged_attachment '{"contact":"exact_group_id@chatroom","parentSortSeq":1700000000000,"itemPath":[0]}'
# 强制从 CDN 重新下载，跳过本地原件：
./wx call wechat_download_merged_attachment '{"contact":"exact_group_id@chatroom","parentSortSeq":1700000000000,"itemPath":[0]}'
```

父消息也可用十进制字符串 `parentServerId` 定位，避免 64 位 ID 在 JavaScript 中失去精度；两个父定位字段互斥。`itemPath` 保留嵌套记录路径，`dataId` 是同一父记录内的替代选择器，两者互斥。`fileNames` 可筛选准确文件名。

读取工具先定位本地原件，缺失时自动利用原版微信本机 CDN 缓存完成分段下载、AES 解密、私密导出和大小/MD5 校验；`download=false` 可限制本地读取。强制下载工具完全跳过现有原件，成功包含 `networkDownloadPerformed=true`、`encryptedBytesDownloaded` 和 `downloadRequests`。

仅 `status=success`、`originalFileObtained=true`、`sizeVerified=true`、`md5Verified=true` 表示取得原文件，返回绝对路径和 SHA-256。缩略图、同名错误文件和 XML 元数据不算成功。当前下载后端支持合并记录内 C2C 普通文件（filetype=5），不代表所有微信媒体远程下载均已支持。2026-10-15 已通过 JSON CLI 强制重新下载两份真实 PDF，独立核对正文、字节数与 MD5。

此下载链路不依赖 GUI、分屏、SpaceO、辅助功能、调试附加或微信改签。需要合法的本地父消息元数据和微信维护的 CDN 路由/凭据缓存。缓存缺失或鉴权被拒绝时才提示打开原版微信、确认登录联网后重试；无需先点击文件。文件过期、网络故障或校验失败返回 `status=unavailable` 和具体错误，强制下载失败不会拿本地旧文件冒充成功。auth key、AES key、账号编号和下载标识不进入返回结果或发布包。

旧 `wechat_read_media` 可传外层 `serverId` 加 `mergedItemPath` 或 `mergedDataId`；只传外层合并消息时返回明确的选择器提示。新工具未加载的聊天可直接使用相同 JSON CLI。工具不回显 CDN 标识、AES 参数或完整 XML。

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
- `backends/send/chatter.py` 是另一种可选适配器，只有合成 HTTP 服务测试通过。原生试验通过独立 `agent_sender.py` 接入；状态报告区分“已配置/可启动”和实际送达。
- 本分支未附带 Accessibility 助手。常规数据库读取及诊断不依赖它。

## 测试

```sh
cargo fmt --check
cargo clippy -- -D warnings
cargo test --locked
sh agent_bridge/test.sh
```

Python 测试使用合成数据库和模拟服务，不读取你的微信账号，也不发送消息。CI 同时运行 Rust 与 Python 检查。

需要第二屏隔离的宿主可在注册时传 `--window-command /path/to/agent-screen`。这是可选宿主适配，不是其他智能体必须安装的依赖。单屏宿主需要其自身的健康隔离会话；当前第二屏适配器不会绕过该条件。

## 生成发行包

```sh
python3 scripts/package-agent.py --binary agent_bridge/bin/wx-cli \
  --version v0.7.4-agent.5 --output dist/wx-cli-agent-v0.7.4-agent.5-macos-arm64.tar.gz
```

打包器按源码允许列表收集文件，携带显式指定的开源 CLI 二进制，生成 SHA-256 文件；排除 `.state`、本机 MCP 配置、账号数据、密钥、缓存和微信程序。发行包可离线复用随包运行时；首次账号/密钥条件仍由 `status` 检查。若修改 Rust 功能，维护者应先 `./wx setup --build` 并更新固定运行时发布信息，避免源码入口下载旧功能版本。

## 来源与许可证

上游：`pandorafuture/wx-cli`，基于 commit `2abe708f55bfe135539a385df856fdc58f97fc74`，MIT，保留原版权说明。

临时副本捕获代码改编自 [acmerfight 的公开 Gist](https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f)，原始文件摘要及 MIT 许可证保存在 `debug_copy_source/SOURCE.json` 和该目录的 `LICENSE`。这些摘要用于标注所参考的原文件，本分支文件已修改。
