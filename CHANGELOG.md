# 更新日志

## 2.1.3 — 2026-09-27

修「服务重启会把**正在跑**的任务判死」的恢复漏洞，并把「排队超时」的计时口径摆正。

- **恢复漏洞**：重启取消在跑的 `_run_task` 时，`CancelledError` 分支写回
  `status='queued' + account=NULL`，会话（`conversation_id`）留着。而启动恢复的两个入口
  一个要求 `account` 非空（`recoverable_tasks`）、一个要求 `conversation_id` 为空
  （`recoverable_queued_tasks`）—— 这类行两边都不沾，只能干等看门狗按「排队超过 30 分钟」
  判死（线上实测：`video_41d33ec7…`、`video_c7724d79…` 这两个 30 秒两段任务就是这么没的，
  而它们其实已经在上游生成过一版）。
  现在 `recoverable_queued_tasks` 改成「没会话**或**没账号」，与 `recoverable_tasks`
  恰好互补，四种组合一个不漏（新增 `tests/test_restart_recovery.py` 断言这个不变式）。
- **排队超时计时口径**：`stale_pending_tasks` 的 `waiting` 桶从 `created_at` 改成
  `COALESCE(updated_at, created_at)`（= 最后一次状态变化）。旧口径下，一个跑了大半截、
  刚被重启打回队列的任务会在 60 秒内被判「排队等待超过 30 分钟仍未拿到可用账号」，
  报错文案还指向「上游限流」，完全误导；看门狗日志同样按新口径报分钟数。
  配套：`_run_task` 开头写一次 `status='queued', error=''` 把排队时钟清零，
  保证刚捞回来的任务拿到完整 30 分钟等待窗口（2.0 等「满额」最长要等 10 分钟）。
- **过期备注**：重新受理 / 新一轮尝试开始时清空上一轮的 `error`
  （`on_account_try`、`on_conversation_id` 写入 `error=""`），否则任务成片后面板
  「报错/备注」列还挂着「任务被中断（服务重启），已重新排队」。
- 启动日志补一行 `[startup] 重新受理 N 个没有账号的任务（含被重启取消、只剩会话的）`，
  重启后到底捞回了几个任务一眼可见。
- 版本号 `2.1.2` → `2.1.3`（`server.py APP_VERSION`，`/health` 同步暴露）。

## 2.1.2 — 2026-09-23

按要求**全部放开并发限制**（这次是批量「你好探测」，以及它背后的真正瓶颈：线程池）。

- 批量「你好探测」：`DOLA_HELLO_PROBE_CONCURRENCY` 默认 **0 = 不限并发**，
  `DOLA_HELLO_PROBE_INTERVAL` 默认 **0 = 不等**。被登出仍一次判风控；
  「没回复」重试一次仍没回复才判（两振出局，防误锁）；上游 710022002/网络失败只记
  「未完成」，不锁号。上游对本机出口 IP 的限流大概率会让其中一批落到「未完成」，
  想稳一点就把这两个值调成 3 / 2.0（或 1 / 2.0）。
- **线程池放开**：出片与探测都跑在 `asyncio.to_thread` 里，而 Python 默认线程池只有
  `min(32, CPU+4)` 个线程 —— 不限并发时第 33 个任务只能排队，等于悄悄限了并发。
  现在启动时显式设置线程池：`DOLA_THREAD_POOL_MAX`（默认 0 = 自动 `max(64, CPU×8)`），
  启动日志会打印 `[startup] 线程池 N 路`。
- 面板：批量探测的确认弹窗文案同步（不再写"并发 3 路"）。


## 2.1.1 — 2026-09-23

「你好探测」从"单号按钮"升级成**顶部批量按钮**，并修掉批量探测暴露的三处问题。

- 面板：账号管理页顶部新增两个按钮
  - **你好探测**（批量）：勾选了就探勾选的；没勾选就探**当前筛选出来的号**（受分组按钮影响）。
    后台任务 + 弹窗进度，结论分三档：`通过` / `未通过(进风控)` / `未完成`。
  - **批量恢复**：把选中的（或当前筛出来的）【风控】/【异常】号批量放回流程。
    （风控恢复后回到【待激活】，建议紧接着再探一次确认登录。）
- 批量探测的判定口径（比单号更保守，避免误封）：
  - **被登出**（`x-tt-agw-login != 1`）是强信号 → 一次就判【风控】；
  - **没回复 / 没探成** → 退避重试一次，**两次都失败才判【风控】**（两振出局）——
    实测上游限流时号会假死，一次就锁会把好号误封（线上刚发生过：9 个号被判风控，
    其中有的重试后回复正常）；
  - 上游限流（710022002）/ 网络不通 → 记为`未完成`，**绝不锁号**。
- 并发与节奏：实测并发 3 路会被上游按出口 IP 整批限流（3 个不同的号同一秒全被拒），
  所以默认**串行 + 每个间隔 2 秒**，软失败再退避 4 秒重试。
  三个旋钮：`DOLA_HELLO_PROBE_CONCURRENCY`(1)、`DOLA_HELLO_PROBE_INTERVAL`(2.0)、
  `DOLA_HELLO_PROBE_RETRY_WAIT`(4.0)。
- 修：批量重试后探通过的号仍停在【风控】组（按规则风控要人工恢复）→ 现在日志会明确提示
  「该号仍在【风控】组，要人工恢复才会重新派发」。
- 修：模块级 `asyncio.Semaphore` 绑死第一个事件循环的隐患 → 批量任务改为在协程内部创建。
- 测试：`tests/test_batch_probe.py`（3 例：结论与分组、软失败不锁号、重试救回假死号）。


## 2.1.0 — 2026-09-23（分组大迭代，分期落地）

按拍板的规则重做账号分组与派发；每期都能单独部署/回滚。

### P5（【异常】组：派发后 5 分钟没有任何回应）

- `DOLA_NO_ACK_SECONDS` 默认 **300 秒（5 分钟）**，且**对所有时长生效**：
  - 30 秒档：`_wait_video_same_conversation()` 的 ack 阈值从 420 秒改 300 秒；
  - 其它时长：新增 `_await_first_ack()`，提交后先等「第一次回执」（额度播报 / 状态 / 报错；
    自己的提示词回显不算），5 分钟没有任何回应就判异常，
    而不是干等到 15 分钟总超时（`DOLA_PURE_TIMEOUT=900`）。
- 判异常的动作：`AbnormalNoAckError` → `BrowserPool._mark_abnormal()`（`DOLA_ABNORMAL_COOLDOWN_SEC`
  默认 1800 秒自动恢复）→ **换号重试**；所有号都异常时任务报错
  **「生视频过程中出现异常情况，请重试」**（拍板要求的文案，`ABNORMAL_RETRY_HINT`）。
- 【异常】组不参与派发，面板有独立按钮 + 行内「恢复」按钮。
- 修一个线程安全问题：`BrowserPool` 的 sqlite 连接是**多线程共用**的（探测在
  `asyncio.to_thread` 里、出片回调 `on_balance` 也在工作线程），sqlite3 的隐式事务
  会让两个线程的 BEGIN/COMMIT 互相踩，冒出 `cannot commit - no transaction is active`
  把任务判失败（在并发用例里复现到）。改法：连接加 `isolation_level=None`（自动提交，
  每条语句各自原子），并把探测的**网络部分**留在工作线程、**落库部分**回到事件循环线程
  （`_probe_network` / `_persist_probe` / `probe_hello_async`）。
- 探测通过/未通过都打日志，便于观察（`access.log` 里 `[pool] … 你好探测通过/未通过`）。
- 测试：`tests/test_abnormal.py`（3 例）+ `tests/test_first_ack.py`（4 例：提示词回显不算回执、
  额度播报算回执、failure_reasons 算回执、非 pending 状态算回执）。

### P4（「你好」风控探测 + 风控组 + 待激活 + 人工恢复）

- 新增纯 API 探测 `pure_api_gen.hello_probe()`：发一句「你好」，同时读**登录标记**
  `x-tt-agw-login`，返回 `status` 四态：
  - `ok`（登录有效 + 有回复）/ `logged_out`（被登出）/ `no_reply`（一句都没回）/ `error`（探测没跑成）。
- 判据（按拍板）：**被登出 或 没有回复 → 进【风控】组**（永久，人工恢复才出组）；
  但 `error` 属**软失败**（网络不通、上游 710022002 拒绝），**绝不判风控** ——
  2026-09-23 线上实测：健康号 acc110 的探测被上游拒绝一次，按软失败判风控会把好号永久锁死。
- 派发前探测（`BrowserPool.probe_hello`，带 300 秒缓存 `DOLA_HELLO_PROBE_CACHE_SECONDS`）：
  未通过的号本次直接跳过、换下一个号，不白提交一次出片。
  探测通过后等 `DOLA_HELLO_PROBE_SUBMIT_GAP_SECONDS`（默认 15 秒）再提交出片 ——
  探测和出片走同一条上游提交通道，挨着发就是 710022002 的来源。
- 【待激活】：未完成首次登录确认（`login_ok` 为空）的新号不派发；验证通过 / 探测通过即激活。
- 登录态失效（`login_ok=0`）也归【风控】组（「被登出」是这个组的事实基础），不再是悄悄失效。
- 人工恢复：`BrowserPool.recover_account()` + 面板行内「恢复」按钮；
  **风控恢复会把登录态清空退回【待激活】**（登录失效是事实，不能按一下按钮就假定它能出片），
  异常恢复直接清到期时间。
- 面板：风控/待激活行加「你好探测」按钮（强制真探，弹窗显示 登录标记/是否回复/耗时/回复原文），
  风控/异常行加「恢复」按钮。
- 测试：`tests/test_hello_probe.py`（10 例：能聊但被登出仍判风控、无回复判风控、
  软失败不锁号、缓存命中不占通道、待激活、风控恢复退回待激活、异常恢复、探测门拦住出片）。

### P3（冷却重定义；删除「限流」「叠加失败」两套概念）

- 【冷却】按拍板重新定义：**剩余 0 或 1 点**的号一律进冷却组，不参与派发，额度日 23:00
  重置后自动回满额（`used_today` 按额度日切表，无需定时任务）。
  上游自己报的「今日次数用完 / 额度不足」也归冷却组，原因文案直接可读
  （例如「上游报额度不足（今日剩余 0）」）。
- **删除「限流」概念**（`DOLA_TRANSIENT_COOLDOWN_SEC` 保留变量但不再被读取）：
  上游 710022002「访问频繁」现在只是一次普通失败 —— 不冷却、不写状态、不影响分组，直接换号重试；
  同号短时间内重复试错由既有的 120 秒失败防抖兜着。面板上的「限流至 HH:MM」徽标一并删除。
- **删除「叠加失败 / 全池重置」概念**：`_mark_failed()`、`_clear_all_failed()`、
  `_overlay_unlockable()` 全部废弃（保留空壳兼容），`_schedulable()` 不再看 `failed_at`，
  调度循环里那段「清标记重跑一轮」也删了。`failed_at/failed_reason` 列保留但不再被读写。
  改法：普通失败只换号重试；**同号连续失败 3 次**才进【异常】组（`FAIL_STREAK_TO_ABNORMAL`，
  到期自动恢复），出片成功即清零；`profile 缺失` 也进异常组而不是永久锁号。
- 额度不足（CreditError）从「叠加失败」改为走额度口径的冷却（`quota_blocked`），
  重置额度时一并恢复。
- 面板：状态列改名「运行」，只留 出片中/登录失效（风控、冷却、待激活、异常都在「组」列）；
  「积分不足至/冷却至」徽标删除。
- 修好一条长期失败的用例：`test_reset_daily_quotas_restores_blocked_accounts` 少建了
  accounts/ 目录（`reset_daily_quotas` 按目录取名单，之前恒返回 0）。
  另按新语义重写 `test_upstream_failure_marks_by_kind`（限流不写状态、other 连续 3 次进异常组）。

### P2（按模型分流选号 + 2.0 排队）

- 选号按模型的**额度组**分流（新增 `_quota_group()` / `_allowed_quota_groups()` /
  `_ordered_candidates()`）：
  - seedance-2.0（每条 3 点）**只**从【满额】组取号；
  - seedance-2.5（每条 2 点）**先**取【半额】，半额空了再用【满额】兜底（避免 1 点碎片浪费）。
- 冷却/风控/待激活/异常 四个组一律不参与派发（`BLOCKED_GROUPS`）。
- 2.0 撞上「满额暂时用完」不再立刻失败：先排队等待（`DOLA_V20_WAIT_SECONDS`，默认 600 秒，
  每 5 秒重扫一次），超时才报「没有可用的【满额】账号…请稍后重试」；排队期间任务在库里仍是
  `queued`，画布显示「排队中」。新增 `AllAccountsGroupEmptyError`。
- 修一个静默 bug：`set_login_status()` 在 `accounts_meta` 还没有该行时 UPDATE 影响 0 行，
  账号会永远停在「未验证/待激活」；现在先 `_ensure_meta()`。
- 测试：`tests/test_model_routing.py`（7 例：2.5 优先半额、2.5 兜底满额、2.0 只吃满额、
  2.0 无满额报错、3 点/2 点/1 点/0 点的额度组、阻断组不派发）。
- 线上干跑（真实 110 号池，不扣点数）：2.5 命中 acc13（剩余 2 点/半额）、2.0 命中 acc1
  （剩余 4 点/满额），候选序列里半额号排在满额号前面。

### 分组口径（拍板后最终版）

```
全部 110 │ 有效(=正常) 110 │ 满额 97 │ 半额 11 │ 冷却 2 │ 生成中 0 │ 风控 0 │ 待激活 0 │ 异常 0
剩余额度 = 满额×4 + 半额×2 + 冷却/生成中的剩余 = 414 点（≈seedance-2.5 可出 207 条）
判定优先级：风控 > 待激活 > 异常 > 生成中 > 冷却 > 半额 > 满额
派发：2.0(3 点) 只吃【满额】；2.5(2 点) 先【半额】后【满额】；冷却/风控/待激活/异常 不派发
额度日 23:00（东京 00:00 = 北京 23:00）自动重置，冷却组自动回满额
```

### P1（分组计算 + 面板按钮，只读不动内核）

- 新增分组判定 `browser_pool.group_of()`，优先级：**风控 > 待激活 > 异常 > 生成中 > 冷却 > 半额 > 满额**。
  - 满额 = 剩余 == 每日上限（4 点，seedance-2.0 专用）
  - 半额 = 2 ≤ 剩余 < 上限（seedance-2.5 优先）
  - 冷却 = 剩余 ≤ 1（0/1 点一律冷却，额度日 23:00 重置后自动回满额）
  - 生成中 = 被任务占用（内存锁 ∪ 任务库 processing 账号）
  - 风控 / 待激活 / 异常 见 P4/P5
  - 【有效】= 【正常】= 满额+半额+冷却+生成中
- `accounts_meta` 新增 6 列（幂等迁移）：`risk_since`、`abnormal_until`、`abnormal_reason`、
  `activated_at`、`probe_ok_at`、`probe_result`；老号用 `login_checked_at` 回填 `activated_at`，
  避免被误判成待激活。
- `/api/admin/accounts`、`/api/admin/stats` 返回 `groups` 计数、`all_count`、`valid_count`、
  `remaining_points`（正常组剩余总额度 = 满额×4 + 半额×2 + 冷却/生成中的剩余）。
- 面板：账号管理页顶部新增分组按钮条（全部 / 有效 / 满额 / 半额 / 冷却 / 生成中 / 风控 /
  待激活 / 异常 / 剩余额度），每个按钮显示数量、点击筛表；号池概况卡片同样显示一条分组概览
  （点击可跳转到对应分组）；账号表新增「组」列（带原因小字）。
- 测试：`tests/test_groups.py`（8 例，含优先级与上限变化）。

## 2.0.12 — 2026-09-23

把上游（dola）的**拒稿原文**完整、可读地落到「任务报错」与「账号备注」，面板上不用再翻日志。

以前的问题：真实拒稿是这样落库的（2026-09-23 线上样例）——

```
连续 3 次生成均失败（依次尝试账号: acc1、acc10、acc100）: acc100 出片未成功
status=failed: 出于肖像保护考虑，未认证人脸暂不支持用 Dreamina Seedance 2.5 生成视频。
你可以尝试换其它参考图或文生视频。 | <同一句再重复 2 遍> | 生成视频：出场角色…（提示词回显）
```

- 链路里被 `[:300]` 截一次、落库被 `[:500]` 截一次，面板表格再 `slice(0,30)` 截一次，
  最后看到的是「连续 3 次生成均失败（依次尝试账号: acc1、acc10」——原因完全看不到。

改动：

- 新增 `failure_text.py`：按 ` | ` 拆片段 → 丢掉提示词回显/超长噪音 → 去重 → 识别分类
  （肖像保护 / 内容审核 / 版权·侵权 / 参考图问题 / 时长协商）→ 输出两行：
  `【上游·肖像保护】<上游原话>` + `技术细节：<换号顺序/status>`。
- `server.py`：任务落库（受理/恢复/测试生成）统一走 `failure_text.summarize()`，不再 `[:500]` 硬截。
- `browser_pool.py`：换号链里的 `str(last_err)[:300]` 放开到 1500，原因不再被切；审核类拒稿
  （肖像保护/内容审核/侵权）**原文追加进该账号的「备注」**（`append_note`，保留人工备注，
  上限 500 字，只留最新），限流/时长协商不写备注，避免刷屏。
- `browser_pool.py`：`CONTENT_REFUSAL_MARKERS` 补上「肖像保护 / 未认证人脸 / 内容审核 /
  不合规 / 涉嫌侵权 / 未授权使用」——以前「肖像保护」不在这张表里，会被归成 other，
  把好好的账号标成「失败(待全池重置)」。
- `web/index.html`：任务列表新增「报错/备注」列（完整展开、可换行、带 tooltip），不再只显示 30 字；
  账号管理页与仪表盘账号表新增「备注」列（显示上游拒稿原文，点击即可编辑）。
- 历史数据：线上 `tasks.db` 里已有的失败任务报错用新格式重写了一遍（原库先备份），
  老任务的报错也能看全了。

- 测试：`tests/test_failure_text.py`（9 个用例，含线上真实样本）+ `tests/test_quota_reset.py`
  新增 3 个用例（肖像拒稿写备注、限流不写备注、备注追加保人工内容）。

## 2.0.11 — 2026-09-22

把线上（CentOS 7 / glibc 2.17 + 宝塔面板）的部署方式**收进仓库**，换台机器照着
`deploy/` 走一遍就能起来，不用再踩 glibc 轮子、multipart 1m、证书续签这些坑。

- 新增 `deploy/` 目录：
  - `README.md`：部署手册（Python 3.11 独立发行版、依赖约束、Node 16、目录权限、
    systemd、看门狗、nginx 反代、宝塔面板、环境变量表、排障小抄）；
  - `run.sh.example`：启动脚本模板（域名 / Key 用占位符，所有环境变量注释齐全）；
  - `service.sh` / `healthcheck.sh`：日志落盘入口与健康看门狗脚本；
  - `stable-dola-pool.service`：systemd 单元（`Restart=always` + `MemoryLimit=2800M`，
    注明 CentOS 7 的 systemd 219 只认 `MemoryLimit`）；
  - `stable-dola-pool-health.service` + `.timer`：每 2 分钟探活，连续 3 次失败自动重启；
  - `nginx-site.conf.example`：反代要点（`client_max_body_size 64m`、
    `proxy_read_timeout 1800s`、well-known 放行）。
- `.gitignore` 增加 `refs/`（参考图落盘的运行期目录）。

功能代码无改动；线上实例已同步到 `2.0.11`。

## 2.0.10 — 2026-09-22

接着 2.0.9 的「不假生成中」再补两块：**任务看门狗**与**重启不留孤儿**。
起因是线上有一批任务挂起一小时以上没结果（上游 `710022002` 限流 + 服务重启中断）。

- **新增任务看门狗** `_task_watchdog_loop`（默认每 60 秒，`DOLA_WATCHDOG_INTERVAL_SECONDS` 可调）：
  - `processing` 但超过 `DOLA_STALE_TASK_SECONDS`（默认 600s）没有任何进度更新 → 重新派发，
    最多 3 次，仍无进展则明确失败；
  - 停在 `queued`、创建至今超过 `DOLA_MAX_QUEUE_WAIT_SECONDS`（默认 1800s）→ 明确失败并说明原因
    （「排队等待超过 30 分钟仍未拿到可用账号（上游限流），请稍后重试」），客户端不再无限等。
- **重启不再留孤儿**：`_run_task` 捕获 `asyncio.CancelledError` 并把任务退回 `queued`
  （CancelledError 不是 `Exception`，原来不会被 `except Exception` 兜住，那些行会永久卡在
  `processing` 且没人接管）。
- **对上游限流降速**：`run.sh` 增加 `DOLA_MIN_SUBMIT_INTERVAL_SECONDS=20`（同一账号两次提交间隔），
  缓解 `710022002 当前服务访问频繁`。
- 补 `tests/test_stale_watchdog.py`（分桶判定 + 看门狗动作），并修掉单测误写线上库的问题
  （测试改用临时 store）。

## 2.0.9 — 2026-09-22

**管理面板改版 + 并发默认放开**：面板换成玻璃拟态、板块按钮移到左侧边栏；全局并发默认
改为不限，并修掉「等在并发槽上的任务被误报成生成中」。

### 界面（管理面板）

- 顶部 `<header>` 与横排 `<nav>` 整体移除，改为固定在左侧的 `<aside class="sidebar">`：
  品牌 + 7 个板块按钮（各配描边 SVG 图标）+ 底部全局操作（自动刷新 / 更新时间 / 刷新 /
  压力测试 / 注销）。顶栏不再保留，避免只剩一条按钮横条。
- 整体改为**玻璃拟态**：深色底 + 四层极光色团（青绿 / 蓝 / 紫 / 橙）`blur(28px)` 缓慢漂移；
  侧边栏 / 卡片 / 弹窗 / 登录框统一 `backdrop-filter: blur(20px) saturate(170%)`
  + 1px 高光描边 + 深投影；徽章 / 按钮 / 输入框 / 开关 / 滚动条全部换成半透明描边体系
  （原来的 `#e6f7f0` 这类浅底色块在深色底上会糊掉）。
- 4 个统计卡改为高透明彩色玻璃 + 顶部内高光，避免在深色底上发闷。
- **窄屏适配**（原来没有任何媒体查询）：≤920px 时侧边栏收成 92px 图标轨道，统计卡 2 列，
  宽表格改为横向滚动而不是挤压换行。
- 顺带修两个主题变量笔误：`#srcFilter` 上的 `color: var(--tx)` 与「测试生成成功」提示用的
  `var(--ok)` 都从未定义过，一直是失效声明。

### 并发

- **全局并发默认改为不限**：`DOLA_MAX_CONCURRENCY` 默认 `3` → `0`（`0` = 不限，走
  `browser_pool._UnlimitedSemaphore`）。注意这是**行为变更** —— 线上若没显式设置该变量，
  重启后全局并发将不再有上限。
- 随之修掉 `workers` 在「不限」时被 `max(1, ...)` 强行报成 `1` 的问题；并给排队等待估算的
  `ahead // workers` 加防零守卫（不限时 `batches = 1`，任务之间不互相排队）。
- **修状态误标**：`_run_task` 原本在 `pool.generate_video` 之前就写 `status=processing`，
  而真正的等待（全局并发槽 / 账号锁）发生在那之后 —— 被卡住的任务因此对客户端显示成
  「生成中」而不是「排队中」，2.0.8 的排队进度提示只在卡「每 Key 并发限制」时才会触发。
  现在改为由 `on_account_try` 在真正拿到账号（= 拿到槽位）那一刻才置 `processing`；
  `started_at` 只记首次尝试，避免换号把出片耗时均值（ETA）拉长。
  - 修复前（3 个任务抢 1 个槽）：`processing, processing, processing`，
    `GET /health` 的 `queue` 报 `{queued: 0, running: 3}` —— 2 个任务一个槽都没拿到却报成在跑。
  - 修复后：`processing, queued, queued`，`queue` 报 `{queued: 2, running: 1}`。
- `GET /health` 与 `progress.workers` 在「不限」时返回 `0`（`0` = 不限）；`API.md` 已注明。

### 影响与注意

- 不限并发后，唯一的背压是 `DOLA_MAX_PENDING_TASKS`（默认 100）与每号每日点数；真正会串行的
  只剩「同号互斥」（per-account `asyncio.Lock`）。因此实际同时出片数 ≈ 空闲账号数，
  超出的任务会停在 `queued` 等账号（这个状态现在显示正确了）。
- 原来的 `DOLA_MAX_CONCURRENCY=3` 同时是一道防 OOM / 防风控的闸门（`ADD_ACCOUNT_SEM` 的注释
  提到 1.9GB 机器同时开浏览器会 OOM）。号池大 / 机器内存小的部署，建议显式设一个正数观察。
- 想收回限制：`export DOLA_MAX_CONCURRENCY=3`，无需改代码。

## 2.0.8 — 2026-09-22

**新增：排队/进度可见** —— 客户端（画布）现在能显示「排队中，预计 N 分钟」，
而不是在服务端还在生成时就把任务显示成失败。

- `POST /v1/videos*` 与 `GET /v1/videos/{id}` 的响应新增 `progress` 对象：
  `state / position / ahead / running / workers / typical_seconds / eta_seconds / eta_text / message`。
  `message` 是可直接展示的一句话，例如「排队中，预计约 4 分钟（前面还有 2 个任务）」
  或「生成中，预计约 4 分钟」。`status` 字段语义不变，老客户端可忽略 `progress`。
- 预计耗时取**近 3 天同档位实测均值**（样本 < 3 条时用兜底表：5s≈130s / 10s≈260s /
  15s≈390s / 30s≈560s），排队等待按 `(ahead // 并发槽 + 1) × 均值` 折算。
- `GET /health` 增加 `queue: {queued, running, workers, typical_seconds, eta_text}`。
- 画布（infinite-canvas）同步改造：视频节点在生成中会显示号池回的进度文案
  （`web/src/services/api/video.ts` 透传 `progress`，`canvas-node.tsx` 渲染）。

## 2.0.7 — 2026-09-22

小版本：把「CentOS 7 / glibc 2.17 的安装约束」这类部署辅助文件纳入仓库，避免下次部署
再踩同样的坑；版本号与线上对齐。

- 新增 `deploy-constraints.txt`：`pillow` / `greenlet` / `numpy` / `opencv-python-headless`
  锁到 manylinux2014 版本（它们在 glibc 2.17 上会退化成源码编译，而这些机器通常没有 clang）。
- 新增 `deploy-extra-requirements.txt`：`requirements.txt` 漏了但 import 链需要的
  `opencv-python-headless`（`server.py → browser_pool.py → video_worker_ui.py → gap.py → cv2`）。
- 安装命令固定为（缺 `-c` 会在老 glibc 机器上编译失败）：

  ```bash
  .venv/bin/pip install -c deploy-constraints.txt -r requirements.txt
  .venv/bin/pip install -c deploy-constraints.txt -r deploy-extra-requirements.txt
  ```

## 2.0.6 — 2026-09-22

修 2.0.5 引入的「参考图偶发失效」问题，以及重启导致任务被浏览器路径判死的问题。

### 修复

- **参考图不会再被提前删掉**：2.0.5 在任务结束（含被取消/服务重启）时无条件清理
  `refs/` 落盘文件，但库里存的是路径 —— 任务重排/重启后恢复时就找不到文件，
  报出误导性的「参考图片只支持 http/https 公网 URL」。
  现在只在任务**走到终态**（completed/failed）时才清理文件并清空该字段；
  非终态保留文件供恢复使用，残留由启动清扫（24h）兜底。
- **文件丢失时给出正确提示**：形如 `refs/xxx.png` 但文件不存在 → 报
  「参考图文件已丢失（任务重排或被清理）」，不再伪装成格式错误。
- **重启后不再走浏览器 resume**：`_resume_task` 对 cookie 来源账号改为**按原参数走纯 API
  重新受理**（参考图落盘文件已保留），避免在跑不了 Chromium 的机器上报
  `Connection closed while reading from the driver` 把任务判死；login 号仍走浏览器 resume。

## 2.0.5 — 2026-09-22

本版把「号池 → 画布 / 客户端」这条链路从"能跑"修到"扛得住"，并处理了当天线上
出现的 4 类报错。所有改动已在线上服务器验证。

### 新功能

- **画布（纯前端）直连**：全站加 CORS 中间件（`DOLA_CORS_ORIGINS`，默认 `*`）；
  `POST /v1/videos`（及 `/v1/videos/generations`）在 JSON 之外同时接受
  **Sora 风格 multipart/form-data**：`seconds → duration`、`size/ratio → 画面比例`、
  `image[] / image / first_frame / last_frame` 文件自动转成参考图，`video[] / audio[]` 记日志忽略。
- **任意时长**：`DOLA_NATIVE_DURATION_MAX`（默认 0 = 只放原生 5/10/15/30；
  设为 15/20/30 时放开 4~30 区间，非原生时长只走 `seedance-2.5`）。
- 管理面板：账号支持重命名；「重置额度」不再把残留元数据算进账号数。
- `/health` 新增 `version` 与 `storage`（任务数 / 库体积 / 出片目录体积 / 保留天数）指标。

### 修复

- **cookie 批量导入不再强依赖浏览器**：探测 patchright 驱动可用性（CentOS 7 的 glibc 2.17
  跑不了自带 driver/node），不可用时只落盘 `cookie_state.json`，登录态验证改走纯 API 探活；
  浏览器路径启动失败也会降级。解析层兼容 `Cookie: ` 前缀。
- **参考图不再写进数据库**（当天面板卡死的元凶）：`data:` URL 先落盘到 `refs/`，
  库里只存路径（一条任务从 36MB 降到 31 字节）；新增 `DOLA_REFERENCE_TOTAL_MAX_BYTES`（默认 60MB）。
- **BDMS 签名器加固**：签名单次等待上限（`DOLA_SIGN_TIMEOUT`，默认 15s，原来会无限持锁）、
  长驻进程失败自动用一次性 node 兜底、熔断窗口 30~180s、每 `DOLA_SIGN_RECYCLE_AFTER`(500)
  次签名主动重启、重启/退出收尸。压测 400/400 成功。
- **30 秒档**：上游 2026-09-22 改用「确认后可拆两段」的话术拒绝单条 30 秒，识别扩展后
  在 `DOLA_ALLOW_30S_PAIR=1` 时走**本地 2×15 秒 + ffmpeg 拼接**；实测一次成功、
  成片 30.04 秒、扣 2 点。
- `requirements.txt` 补 `python-multipart`（multipart 解析必需）。

### 稳定性 / 运维

- **三层防线**防止库和磁盘再被撑爆：写入侧单列截断（`store._FIELD_LIMITS`）+ prompt 长度校验
  （`DOLA_MAX_PROMPT_CHARS`）；存储侧保留期清理（`DOLA_TASK_RETENTION_DAYS`，默认 14 天，
  清任务记录 + 视频文件 + `VACUUM`）与出片目录上限（`DOLA_DOWNLOAD_MAX_BYTES`，默认 20GB）；
  故障自愈（systemd `MemoryLimit` + `Restart=always`、看门狗 timer 每 2 分钟探活、连续 3 次失败自动重启）。

### 升级注意

- 数据库与 `downloads/`、`refs/` 目录保持 `www:www` 权限；升级后建议看一眼
  `GET /health` 的 `storage` 与日志里的 `[maint]` 行。
- `python-multipart` 是画布/表单接入的必需依赖，别漏装。
