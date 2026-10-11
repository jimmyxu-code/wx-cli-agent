# 微信 4.1.13 原生结构化文本发送实验

2026-10-11，在 Apple Silicon Mac 的微信 **4.1.13 / build 269602** 上，向账号自己的文件传输助手实际发送一条文本。参数经过 JSON 控制器和私密 Unix socket，调用微信原生任务接口；原生完成回调的错误类型、错误码和 BaseResponse Ret 均为 0，用户在手机独立确认收到。GUI 只用于用户登录，没有输入或点击发送。

这是独立的实验后端。常规读取 CLI、MCP 工具和既有 `chatter.py` 适配器没有切换到它。任意联系人、群聊、图片/附件、长期服务及大批量真实发送尚未验收。

## 已实现

- 独立 C++ 任务、文本 protobuf 编码、原生缓冲区序列化、回包解析及回调释放。
- JSON Lines 客户端：`capabilities`、`prepare`、`status`、`enqueue`、`cancel`、`release`。
- 当前用户拥有的 0700 目录和 0600 Unix socket；连接两端核验相同 UID。
- 单次测试控制器核验准确提案和人类授权摘要，在 enqueue 前保存领取记录；未知结果不会自动重发。
- 发送前等待原生当前账号信号；没有账号时不领取授权、不提交。账号存在仅是必要条件，最终仍检查回包与实际接收。
- 严格绑定本机 build、模块 UUID、代码摘要和所属临时进程，初始化观察后删除硬件断点。
- 辅助诊断可以在原生 transport 指令前用硬件断点阻断。诊断线程检查自己的实际调试寄存器，没有精确断点就拒绝进入包装函数。

本机模块 UUID 为 `CDB81058-0FAC-3518-95F5-C0CC7860F9B5`。其他版本或 build 需要重新核验，不能直接平移地址。

## 此次故障与修正

最初一次试验在调试停止时结束，当时未保留足够的停止原因，结果未知。修正控制器后，第二次取得真实失败回调：类型 9、错误码 -1、没有解析到回包。按 [腾讯 Mars 的错误定义](https://github.com/Tencent/mars/blob/master/mars/stn/stn.h)，该组合符合本地任务超时。

随后发现客户端任务管理器 active 并不代表已登录。原生当前账号信号在登录页为 false，用户进入聊天界面后为 true。加入登录就绪门禁后，第三次独立授权的测试取得合法成功回包，手机确认收到。前两次领取记录没有重置或重放。首次停止的确切原因仍无法追溯，不能断言它与第二次完全相同。

本次手机收到，但发送后查询桌面本地的近期记录未找到对应正文。桌面消息持久化和多端同步还需进一步核验；服务端成功、手机收到与本地历史可查是不同的验证项。

## 构建

需要 macOS arm64、Xcode Command Line Tools 和 Apple LLDB Python。源代码不包括微信二进制、数据库、密钥或实际授权记录。

```sh
cd experimental/native_send/native_413
mkdir -p .state
xcrun clang++ -std=c++17 -Wall -Wextra -Werror -dynamiclib \
  core.cpp rpc.cpp -o .state/libwxnative413.dylib
codesign --force --sign - .state/libwxnative413.dylib
```

运行控制器还需要已核验的私密调试副本目录。该目录包含 `WeChat-trial.disabled`、签名计划、原版完整性摘要和签名记录，权限为 0700，记录文件为 0600。`metadata_trial.py` 和 `isolated_attach_trial.py` 只复用已准备的副本，不替你生成授权或修改原版。

所测临时副本经过重签，具有调试权限；临时主程序移除 App Sandbox 并关闭 Library Validation，其余配置与签名计划逐项校验。原版签名与 SIP 保留。临时副本只在实验期间运行，结束后禁用。准备副本的相关脚本见仓库的 `agent_bridge/backends/receive/debug_copy_source`；准备记录的 schema 仍需与本实验的 preflight 对齐，不是下载即用的安装器。

## 结构化调用

控制器启动临时副本、附加其所属进程、载入独立库并建立私密 socket。普通启动后附加与直接 LLDB Launch 的容器访问行为在本机不同，当前控制器采用前者。

JSON 客户端与已运行的 socket 通信：

```sh
python3 client.py --socket /absolute/private/directory/rpc.sock
```

每行一个命令，例如：

```json
{"op":"capabilities"}
{"op":"prepare","recipient":"filehelper","text":"自己的测试文本","timestamp":1791684415,"client_message_id":123456789}
{"op":"status","handle":1}
{"op":"cancel","handle":1}
```

准备消息只创建任务，没有提交。时间应换成当前 Unix 秒，消息编号使用新值。`enqueue` 需要控制器事先授予准确 handle 的一次权限，普通 JSON 命令没有授权入口；未授权请求返回拒绝。

实际单次自测入口为 `runtime_sender.py`，接收 `--root`、`--library`、`--proposal`、`--approval`、`--claim` 和可选 `--report-name`，路径使用绝对规范路径。提案限定 `schema=1`、`kind=send_text`、`recipient=filehelper`、`maximum_sends=1`、`gui_input=false`，并包含准确 `text`、`timestamp`、`client_message_id`。授权记录要求 `human_approved=true`、匹配规范 JSON 的 SHA-256 与短期 `expires_at`，必须在收到人类对准确提案的批准后创建。已有 claim 或报告不会覆盖。

状态分别报告入队、序列化、原生网络错误、回包 Ret 和任务释放。控制器自身不宣称手机收到；独立接收确认另记。暂不提供任意联系人或批量授权入口。

## 离线检查

以下检查不运行微信，不向网络发送消息：

```sh
xcrun clang++ -std=c++17 -Wall -Wextra -Werror \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  test_core.cpp -o .state/test-core
.state/test-core
xcrun clang++ -std=c++17 -Wall -Wextra -Werror \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  test_rpc_fixture.cpp -o .state/test-rpc
WX_NATIVE_RPC_FIXTURE="$(pwd)/.state/test-rpc" python3 test_rpc_client.py
```

覆盖中文/emoji、独立 wire 解码、损坏/缺失/重复 Ret、模拟成功与失败回调、异步释放、128 个任务容量、4096 次取消、并发 prepare/status/cancel、socket 权限与未授权拒绝、缺少硬件门禁拒绝。离线容量测试不构成真实批量吞吐或送达证据。

`test_sender_guard.py` 另验证准确授权、正文/收件人不可替换、过期与符号链接拒绝、单次领取不可重用，需要 Apple LLDB Python 的导入路径。这些源码按仓库 MIT 许可证发布；微信实现、二进制和用户数据不随仓库分发。
