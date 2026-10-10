# 本地 Agent 桥接层

从仓库根目录运行 `sh agent_bridge/build-agent.sh`。常规使用请加 `--read-only`，例如 `agent_bridge/bin/wx-cli bridge --read-only mcp`。数据读取不依赖 GUI 助手。

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

当前仅包含结构化后端适配器和合成测试。真实微信发送尚未通过验证，GUI 工具也不是原生发送的替代证明。默认只读模式不会暴露发送工具。

已有第三方旧版本 Hook 的对象布局与所测 build 不同；更新地址并不足以证明 ABI 兼容。目前需要继续验证任务对象、序列化调用、线程及回调生命周期。当前版本不能用于批量消息发送。
