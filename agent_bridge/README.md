# 本地 Agent 桥接层

新智能体从仓库根目录运行 `./wx connect`，一次完成运行时补齐、真实读取验证和任务示例。例如 `--task download` 选择下载工作流，另支持 read、search、send、mcp；`--install-command` 可注册固定 `wx` 命令。原 `setup/status/tools` 接口保留兼容。发行包已有运行时；源码入口下载固定、校验过的 macOS arm64 二进制。需要源码编译时用 `./wx setup --build`。默认读取不依赖 GUI 助手，MCP 入口为 `./wx mcp`，JSON 配置由 `./wx config` 生成。旧 `bridge --read-only` 接口保留兼容。

## 合并记录原件下载

`./wx call wechat_list_merged_attachments JSON` 定位外层与内部文件，
`wechat_read_merged_attachment` 默认自动补齐缺失原件，
`wechat_download_merged_attachment` 强制网络下载并跳过已有文件。
返回 `status=success`、`originalFileObtained=true`、大小/MD5 均 verified 才成功；
强制下载还需 `networkDownloadPerformed=true`。缓存或鉴权失败才需确认原版微信登录联网。
无需 GUI、分屏、SpaceO 或原生发送实验。后端模块见
`backends/receive/cdn_download.py`，当前支持 C2C filetype=5 的合并记录文件。
下载引用、auth key 和 AES 参数不回显、不入发布包。

## 临时副本取钥实验

`backends/receive/debug_copy_source/bootstrap_keys.py` 会创建微信临时副本、逐项重签并由 LLDB 启动。它要求用户明确选择调试配置，不修改原版签名或 SIP。它是版本敏感的实验工具，不能保证其他 build 上启动成功。

在本分支所测 4.1.13 / 269602 上，使用过 `--profile minimal-debug-copy-unsandboxed` 和 `--authorized-disable-main-library-validation`。这会扩大临时主程序的访问范围及可加载库范围；原版的系统安全配置保持原状。默认 `preserve` 配置不会自动采用这些变更。

在执行之前应当了解并同意这项实验，正常退出微信，准备可重新登录的已有账号，使用全新的私密状态目录。不得复制别人账号、覆盖已有密钥或对 `/Applications/WeChat.app` 重签。脚本会拒绝 root、仍运行的微信和重复状态目录。

先查看参数，不会启动或修改微信：

```sh
python3 agent_bridge/backends/receive/debug_copy_source/bootstrap_keys.py --help
```

真正执行时需要 `--acknowledge-debug-copy`，以及明确选择的 profile。若出现 `manual_restore_required`，应先正常退出残留的临时微信并恢复原版；不要继续叠加实验。成功时密钥只写入状态目录中的 `keys.json`，由 `key import-capture` 做数据库 HMAC 校验后导入。

将状态目录放在 `agent_bridge/.state/` 下。不要将密钥、数据库、日志、临时微信副本或明文缓存加入 Git。此仓库不分发微信专有二进制。

## 原生发送研究状态

独立原生后端已在 4.1.13/build 269602 向文件传输助手完成一次真实文本发送验收。它现可通过 `./wx setup --native-root DIR` 接入原生文本工具，使用 `./wx send` 和 `./wx job`；默认未配置时只开放读取。详见 [实验说明](../experimental/native_send/README.md)。GUI 工具不是原生发送的替代证明。

已有第三方旧版本 Hook 的对象布局与所测 build 不同；更新地址并不足以证明 ABI 兼容。其他版本、群聊、媒体和真实批量发送仍待单独验证。

## 自动语音文字

消息读取和增量接收自动附加 `voiceTranscript`（文本、状态、来源、缓存标记），优先读取微信已保存文本；缺失时由项目本地 SenseVoice 自动转写。首次按固定 SHA-256 准备约 205 MB 运行时/模型/FFmpeg，模型缓存为每位本机用户共享，转写缓存按账号与会话隔离且仅当前用户可读。没有音频或识别失败返回 unavailable，不交给智能体另写识别脚本。详见根目录 README 的“语音消息直接读取文本”。
