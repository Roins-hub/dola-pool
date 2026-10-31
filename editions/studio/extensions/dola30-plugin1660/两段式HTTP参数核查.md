# 两段式独立 HTTP 参数：离线核查

## 已检查

- dola-debugger-v1.6.37-1792601938981.json：提交 25、44、67。
- dola-debugger-v1.6.38-1792603088249.json：提交 102。
- 插件自带 Dola / 豆包 skill-pack 配置快照，以及当前签名前提交代码。

已观察的 video 参数只有 model、duration、resolution、input_box_content，部分请求还有 ratio。
input_box_content 中有 user_input_content 和 reply_message_format。
前三次提交的逐字段覆盖标记为完整（135、139、80 个字段）。导出的正文是摘要而非原始可重放请求。
这几次请求里未观察到明确的 video_count / num_videos / segment_count 等控制项。

server_do_compare 是现有会话参数中的候选布尔字段，不能仅凭名称认定其控制两条视频，
更不能认定它将一个剧本分成前后两段。chat_history_count 等是会话相关字段，
不构成独立视频数量控制的证据。reply_message_format 的用途也没有被验证为隐藏生成指令。
不向这些字段塞入规则，不冒用 system 消息，不修改已签名请求。

## 结论与边界

现有证据尚不足以实现“独立 HTTP 参数控制两段，并且聊天无规则”。
未发现不代表服务器绝对不支持，但随意添加 count=2、甚至 HTTP 200，都不能证明生效。
平台聊天回复的“两条／2额度”不是本检查器认可的结构化任务证据。
插件自带配置是历史快照，不是当前服务器能力声明。

本次没有新增生产注入字段、没有执行真实生成，也未自动改变现有两段式提示词开关。
如需下一次发送完全没有两段式文字规则，先取消当前「两段式规则」勾选。
此前已发出的聊天记录不会因此删除；截图中其他长前缀不属于这一个开关。

## 可重复检查

在项目根目录运行（只读取指定 JSON，结果输出到终端）：

```powershell
node extensions/dola30/tests/audit-two-part-protocol.cjs downloads/dola-debugger-v1.6.37-1792601938981.json downloads/dola-debugger-v1.6.38-1792603088249.json
```

继续实现需要：原生支持多条/分段的入口及其真实请求，或平台客户端/接口中相应字段的定义。
若走猜测字段的在线 A/B 验证，需要另行明确测试次数及可消耗额度，且结果仍可能无效；本次未执行。
