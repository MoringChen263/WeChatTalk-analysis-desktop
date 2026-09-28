# 狗头军师 · 微信对话分析台（jev-chat-analyzer）— 优化版规格

> 本文件是 `README.md`（原始计划书）的**优化版本**，原文件保持不动。
> 相对原计划书的改动见 **§0 变更摘要** 与 **附录 A：变更清单**。
> 阅读约定：文中 `【已存在】`＝`D:\Jev\jev-chat-src` 等既有资产可直接复用；`【待创建】`＝本项目新建。

---

## 0. 变更摘要（相对原计划书）

| # | 原计划书 | 优化后 | 章节 |
| --- | --- | --- | --- |
| 1 | 拟建全新模块树，重复实现已有能力 | **复用映射表**：改造 `jev-chat-src` 现有模块，不新造轮子 | §3 |
| 2 | 采集直接进 M1，无验证 | 前置 **M-1 可行性验证**（复用 `probe/*`），先定阈值 | §14 |
| 3 | "对齐依赖"含糊 | **依赖分层**：默认仅 `requirements.txt`，laya 走可选 extra，控体积 | §11 |
| 4 | 说话人映射只支持两方 | 明确 **v1 一对一**，写出 Non-goals；transcript 预留多 sender | §1.3 / §6 |
| 5 | 记忆"复用或内置同 schema"双实现 | **永远打包官方 `memory_store.py`**（零依赖，可离线） | §5.4 |
| 6 | 引用的文件其实不存在 | 文档内区分 `【已存在】/【待创建】` | §4.3 |
| 7 | 无隐私章节 | 新增 **§8 隐私与数据流**（本地优先 LLM、日志保留、告知义务） | §8 |
| 8 | API key 明文存 config | 凭据改 **Windows 凭据管理器 / 环境变量** | §8.4 |
| 9 | config 示例含绝对路径 | 改为 **相对/自动探测**，零个人路径 | §10 |
| 10 | `parse_output` 未定 | 定义 **严格 JSON schema + 约束解码 + 重试降级** | §7.2 |
| 11 | "帧间去重"未给算法 | 定义 **相似度去重 + 滚动拼接算法与阈值** | §5.2 |
| 12 | 线程模型未提 | 明确 **后台 worker 范式**（复用 `app/worker.py`） | §5.6 |
| 13 | 无成本/时延预算 | 新增 **token/超时/成本上限 + 取消流式** | §7.3 |
| 14 | 无测试策略 | 新增 **§12 测试策略**（单元 + 冒烟 probe） | §12 |
| 15 | 验收标准过弱 | 每里程碑给出 **可量化 DoD** | §14 |
| 16 | "算着发"等术语不清 | 新增 **§15 术语表** | §15 |
| 17 | 无许可合规 | 新增 **§13 许可与合规**（MIT 署名 + LICENSE） | §13 |
| 18 | 无自动更新 | 复用 `app/update.py`，纳入范围 | §5.7 |

### 0.1 勘误与实测修正（2026-09-23 对磁盘/真机实测后逐条修，优先级高于上文表格）

> E1–E6 = 开工前读代码/查磁盘发现；E7–E9 = M-1 与 M1 真机跑出来的；E10–E14 = M2 导入落地时补的；
> E15–E20 = M3 分析闭环落地时补的（其中 E16/E17 是自动化测试抓出来的真 bug）；
> E21–E27 = M4 记忆落地时补的（**全部是把官方 `memory_store.py` 当黑盒跑出来的真实行为**，
> 以及这一轮自己抓出的实现问题）；
> E28–E33 = M3 真机（真模型）第一跑撞出来的；
> E34–E36 = M5 打包时发现的 —— **这三条全都是「源码跑得好好的，一打包就废」**，
> 也就是离线测试与源码运行**根本覆盖不到**的那类问题（E34 由双路径等价性用例钉住，E35 由专项回归用例钉住）；
> E37–E40 = 同一轮 M5 继续深挖补上的（E37 让 skill 在包里**根本找不到**、E39 砍掉 120 MB 死重、
> E40 让打过包的记忆层**缺模块**）—— 这四条同样只在打包产物上才会暴露；
> E41–E42 = 「把包交给别人用」这一步补出来的：**可分发产物 ≠ 构建产物**，
> 以及构建脚本里**不能用中文文案做判定**（E42 是 E36 那轮踩到的编码坑，当时只修了实现没记档）。
> E37–E39 = M5 首次 one-dir 实测（420.1 MB）暴露的：**一条路径错、一条验收缺口、一组体积死重**；
> **E28–E33 = M3 真机（真模型）第一跑就撞出来的**（离线假服务器只会返回「永远合规的小 JSON」，
> 所以这些路径一条都没覆盖到）。每一条都改了实现，不是只改文档。

| # | 原写法 | 实测结论 | 已改为 |
| --- | --- | --- | --- |
| E1 | `target_window_class: "WeChatMainWndForPC"` | 那是微信 3.x。微信 4.x 进程为 **`Weixin.exe`**、窗口类 `Qt51514QWindowIcon`；且 `jev-chat-src/app/capture.py::find_wechat_hwnd()` 实际是**按进程名 + 标题「微信」**匹配，从不按类名。同进程还有「Weixin」工具窗、「图片和视频」看图窗，面积可能更大 → **不能按面积挑** | `target_process` + `title_hint`，类名降为兜底（§5.1 / §10） |
| E2 | 截图「可配临时目录，退出清理」 | 与 `jev-chat-src/docs/KICKOFF.md` 硬约束 3 冲突：捕获位图**全程内存对象，绝不 `.save()`**、不进日志、不上传 | 删掉临时目录选项，写死「永不落盘」（§8.1） |
| E3 | 用 `git pull` 同步 `goutoujunshi` 两份拷贝 | `D:\Jev\goutoujunshi` **没有 `.git`**，不是 git 仓库，`git pull` 无法执行 | 开发期用 `mklink /J` 目录联接、构建期 `robocopy /MIR`（§9） |
| E4 | §13 称「需核对 `jev-chat-src` 许可」 | 已核对：**MIT License, Copyright (c) 2026 rezoch340** | 必须与 goutoujunshi 一并在 `THIRD_PARTY_NOTICES.md` 署名 |
| E5 | §5.1「气泡 box 中心 x 与窗口中心比较分左右」 | **与实际实现不符**。`app/ocr.py::who_said()` 的注释明确写着「按 OCR 框里的颜色分类，**不看 x 坐标**」：绿底 → me；非绿且对比度 ≥150 → her；其余灰字 → 丢弃。实测 me 气泡底色众数 `(53,210,141)`（微信绿），成立率 100% | §5.1 改为「底色为主 + x 位置交叉校验」，x 只作第二信号（§5.1） |
| E6 | 未提「窗口被收进托盘」 | 微信 4.x 常态是**关闭到托盘**（`IsWindowVisible=False`，但 rect 正常）。上游 `find_wechat_hwnd()` 硬筛可见性 → 直接抛错；`unminimize()` 只救最小化。更关键：微信 4.x 真实界面画在**内嵌 Chromium 子窗口 `Chrome_WidgetWin_0`** 里，用 `ShowWindow` 把托盘隐藏的主窗口强行唤起后，**该子视图不会恢复** → 帧全白（实测唯一色 9、暗像素 0.8%）、OCR 0 行 | 新增 `app/capture/window_finder.py`：不筛可见性 + 空白帧探测 `frame_is_blank()`，遇空白帧**明确提示用户手动打开微信**，不静默失败（§5.1 / §8.5） |
| E7 | `ocr_min_score: 0.6` | M-1 实测置信度均值 **0.9895**、最低 0.9344，没有一行落在 0.6 附近；0.6 只会把「图片消息里的字」和错认一起放进来 | 阈值提为 **0.90**（已写进 `config.example.json` 与 `app/config.py` DEFAULTS） |
| E8 | §5.2 未定义「消息时间从哪来」 | **这是 M1 真机跑出来的真 bug**：微信消息本身不带时间，只有居中灰字分隔行（「14:01」）。首帧画面顶部锚点**之前**的历史消息若用采集时刻凑，会得到 `#1=20:07:29 → #2=14:01:00` 的**时间倒流**（被 schema 校验当场拦下） | 时间优先级：①微信灰字时间戳（`ts_source=wechat_mark`）→ ②锚点之前的历史消息取「锚点 −1 秒」（`before_mark`，并在 `warnings` 声明是估算上界、首条可能是上一段对话的结尾）→ ③实在没有才用采集时刻（`capture`，必进 warnings） |
| E9 | 空白帧判据（原 `dark_ratio ≤ 0.02`） | 深色主题下**整帧都是暗像素**，`dark_ratio` 恒 ≥ 0.5 → 判据永远为假、空白帧根本抓不出来 | 改成「唯一色数 ≤ 40 **且** 相对底色的 ink 占比 ≤ 1%」，与深浅主题无关。实测：正常聊天帧 uniq 200~202 / ink 0.4965（不误判），空白帧 uniq ≈ 9（抓得住） |
| E10 | 未提「帧是内容变化驱动的」 | WGC 只在窗口**提交新帧**时给回调：实测静止 16 秒里，只有前 6 秒有回调（122 帧），后 10 秒**一个都没有**。所以「这一轮没拿到帧」既可能是画面真的没变、也可能是微信没渲染，**不能当成错误**；反过来也说明静态屏永远测不出增量捕获（必须等真实新消息） | `collector.poll()` 把「无停稳帧」当正常返回（`None`），只有 `closed`/`blank` 才上报；增量验证必须用真机新消息驱动（§5.1 / §8.5） |
| E11 | §9/§13 只写「遵循 ChatLab 契约（`messages between --format agent`）」 | 查上游 `docs/cn/ai/external-agent.md` 实测：CLI 现名 **`clb`**（旧文档写 `chatlab`）；`--format agent` 返回的是**信封 JSON**，正文在 `data.text` 里，是「`--- 2026/6/1 ---` 日期分块 + `[#id]` / `[#id*]`（星标=被截断/脱敏）/ `[#a-b]`（合并块）」的紧凑文本，**不是**结构化消息数组；要结构化得用 `--format json`（`data.messages`）。失败是 `{ok:false,error:{code,message,hint,candidates}}` + 语义化退出码（2 参数错/3 未找到/4 歧义/5 SQL 错）。上游明确声明「契约可能变化，运行前以 `clb manifest` 为准」 | 新增 `app/capture/importer.py`，**两条路线都实现**：`data.text` 走文本解析、`data.messages` 走结构化；失败信封**原样报错**（含 code/hint/候选），不产出空 transcript；`meta.hasMore=true` 时警告「这只是一页」（§5.2） |
| E12 | 未提导出文件的编码 | 中文聊天导出**普遍是 GBK/GB18030**，按 UTF-8 硬读会 `UnicodeDecodeError` 或糊成乱码（实测夹具用 gb18030 才读对）。HTML 的编码还要先看 `<meta charset>`，且 `gb2312`/`gbk` 都应归到 `gb18030` | `importer.read_text()/read_html()` 逐级降级 `utf-8-sig → utf-8 → gb18030 → utf-16`，并把**实际用的编码**写进 `warnings` / `stats`（§5.2） |
| E13 | §5.2 只说「说话人由用户确认」，未区分采集/导入 | 导入文件里**根本没有几何信息**（没有左右、没有底色），所以 `speaker_map` 的 `left/right` 只能是**标签占位**；而「谁是 me」必须问用户——不能按出现频次、语气或「谁更像自己」猜（skill 边界原话） | `import_file(me_label=...)`：没给就返回 `ok=False, needs="me_label"` + 候选列表让 UI 问；唯一例外是文件**显式**写着 `me`/`我`/`本人`（那是元数据，照读不叫猜）。左右统一按 §5.2 固定契约记 `me=right`，并**强制写一条「这是标签占位」的警告** |
| E14 | §5.2 的去重算法直接套用到导入 | 采集路径的相似度判重是为**帧重复**设的；导入文件是权威快照，**没有帧重复问题**。同一人连发两条一模一样的（「行」「行」）是真实发生的事，按 `dup_threshold` 会被吞掉一条 | 导入路径写死 `dup_threshold=0.0`（关闭相似度判重），只做「时间升序重排 + 乱序警告」（§5.2）。另：无时间的消息**承上**继承上一条已知时间，**绝不能统一塞到文件末尾**——那会触发 schema 的「时间倒流」校验（与 E8 是同一个坑的两个面） |

| E15 | §7.2「优先使用模型原生 JSON 模式；不支持的模型走提示词约束」 | 兼容服务对结构化输出的支持参差不齐，报错方式也不统一 | 做**三档自适应阶梯**：`json_schema` → `json_object` → 仅提示词约束。**只有当服务端报错正文提到 `response_format`/`json_schema`/`structured` 时才降档**——像 `max_tokens` 超限的 400 必须原样报错，不能被掩盖成「不支持结构化输出」。实际用了哪一档写进 `LLMResult.response_format_tier` 并在 UI 如实显示（`llm.structured_output: "off"` 可强制只用提示词约束） |
| E16 | §5.6「LLM 请求可取消」 | **没区分流式/非流式。**`urlopen` 的一次 POST 在等响应头期间没有插入点，中途无法中断；而**靠重发 POST 轮询会让服务端把同一个请求算两次钱**（真金白银的 bug） | 流式：socket 读超时压到 `CANCEL_POLL_S = 2s`，每轮检查取消 → **2 秒内生效**；非流式：只在「调用前」与「读响应体的分块之间」生效，属 best-effort，UI 如实显示「取消中（等当前这一块返回）…」。**绝不靠重发 POST 实现取消**。流式另有「空闲上限」判定卡死（超过 `stream_read_timeout_s` 没新数据），已吐出的部分保留 |
| E17 | §7.2 只写「解析失败 → 修复 → 降级」 | 模型输出被**截断**时，括号平衡扫描会捞到内部的小对象（实测只剩 `steps.facts`）；若直接当分析结果渲染，用户看到的是一屏「模型未给出」 | 增加「是不是分析结构」判定：**含 `steps` 或 `scripts` 才算**，否则降级保留原文；同时支持拆信封（`{"analysis": {...}}`）。`probe/m3_unit.py` 里这两条都有专门反例 |
| E18 | §11.1 依赖分层没写 HTTP 客户端选型 | 环境里只有 `httpx`，还是别的包的传递依赖 | **`llm_client` 只用标准库 `urllib`**：少一个直接依赖、少一份许可证与打包体积；代价是要自己写 SSE 解析与超时/取消循环（已用 6 条路径的假服务器测试覆盖） |
| E19 | §7.3「超出预算 → 明确提示」 | **没配单价时根本无法判定金额**，`daily_cost_cap` 形同虚设 | 明确降级为告警：「未配置 `llm.pricing` 单价：本次无法估算费用，每日成本上限也不会生效」，**不假装在保护用户**。单价默认留空、由用户「与自己的账单对齐」填写；没填时费用显示为未知，而不是编一个数出来 |
| E20 | 未提 SKILL.md 的 frontmatter | frontmatter 里的 `description`（触发词说明）约 **5.9k 字符**，对分析毫无用处，却是每次请求的固定开销 | 喂 prompt 前剥掉 frontmatter：system 里的 SKILL.md 正文由 9620 → **3696 字符**（省掉约 40% 固定开销） |

| E21 | §5.4 只说「subprocess 调用官方实现」 | 官方**出错时也是退出码 1 + stdout 一段 JSON 信封**（`{"ok":false,"error":{"code","message"}}`）。把 stdout 当纯文本丢掉，就等于把错误码扔了，UI 只能显示一句听不懂的话；而 `enable`/`clear`/`revoke`/`forget-object` **必须带 `--confirm`**，不带直接 `CONFIRMATION_REQUIRED` | `store.run_cli()` 统一解析信封 → `MemoryResult(ok, code, message, data)`，并把错误码翻成「下一步该怎么办」的中文提示；破坏性操作的 `confirm` 做成**必填关键字参数**，逼调用点想清楚 |
| E22 | §5.4 未提官方子命令的真实参数形状 | `show` 用的是 **`--subject-id` 旗标**而不是位置参数（旧骨架写的 `show <scope>` 会直接被 argparse 拒）；`context --max-chars` 被限在 **500–8000** | 全部按实测签名封装，`context()` 里对 `max_chars` 做钳制；`probe/m4_unit.py` 有对应断言 |
| E23 | §5.4 只说「五类 scope」 | **`user` 类别的 `subject_id` 是字面量 `"user"`**：官方 `list_memories` 查的是 `subject_id IN ('user', ?)`，所以「按对象召回」时用户档案是靠这个字面量被带出来的。写别的值进去就召不回，而且**不报错**（静默失效） | 手动填写与自动提取都强制 `scope=user → subject_id="user"`；界面在选「用户档案」时把归属控件**禁用**并显示为 user，不给用户填错的机会 |
| E24 | §5.4「超限正确封顶」被当成一句话 | 封顶**不对称**：`event`/`hypothesis` 是**软上限 + 自动剪最旧**（20 / 5，写入不失败）；`user`/`object`/`relationship` 是**硬上限 + 报错**，且实现是「先查条数再插」，所以实际能写到 **上限 + 1** 条才报 `MEMORY_LIMIT_REACHED`（实测 user 到 31 条）。另外库不存在时 `status` **不返回 `memory_count`/`undo_count`** | 新增 `status_dict()` 统一补默认值（否则界面会显示「条数：None」）；测试按**实测边界**断言而不是按文档猜的数；界面文案只承诺总行 200 与撤销栈 20，不宣称 30/15/10 这些内部上限 |
| E25 | §5.4「撤销」未定义粒度 | `undo` **一次退一步**：每次 `apply` 就是一条操作记录，**覆盖写入也算一条**（不是「按字段撤销」）。撤销栈深 20，撤空后是 `NOTHING_TO_UNDO`。更要注意 **`forget-object` 会连撤销栈一起清空**（官方行为），所以删对象那一步是**不可撤销**的 | UI 的确认文案写清「退回最近一次写入/覆盖前的状态」；提示「只想暂时不用请用暂停」；删对象前明确告知**撤销栈会一并清空** |
| E26 | 未提子进程 stdout 的编码 | 官方用 `ensure_ascii=False` 打中文，而 Windows 下 Python 的 stdout 被重定向到管道时按 `locale`（中文机 cp936）编码 → 按 utf-8 解码就是**乱码**，且**只在中文内容上暴露**（英文测试全过） | 给子进程显式注入 `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1`；`probe/m4_unit.py` 专门有一条中文写入→读回比对的断言 |
| E27 | §5.4 未定义「谁能从分析结果自动建档」 | 把模型推断自动写成档案，等于让它悄悄改写自己下一次的输入。官方 `validate_delta` 的交叉规则其实**已经堵死了**这条路（`assistant_inference` 只能进 `hypothesis`；`user` 只接受 `user_explicit`） | 自动提取**只产出 `event`（带 `#id` 证据）与 `hypothesis`（标模型推断 + 置信度）**；`user`/`object`/`relationship` 只留**手动入口**，因为只有用户能说「这是我知道的」而不是「这是模型猜的」。写入前一律过复核窗，可勾选可编辑 |
| E28 | §7 未给输出长度上限定值 | 默认 `max_output_tokens=1200` **撑不下**「五步 + 四版话术」——真机实测要 3k+ tokens（单次流式 4205 字）。给少了的表现**不是报错**，而是模型输出被腰斩、解析失败，看起来完全像「JSON 写错了」 | 默认提到 **4000**；并新增「**截断 → 自动放大上限重试一次**（上限 8000）」。`config_version` 迁移会把老配置里固化的 1200 一起升级 |
| E29 | §7.3 未提 `finish_reason` | 客户端只看 `choices[0].message.content`，**从不读 `finish_reason`** → 「输出被长度截断」与「模型 JSON 写错」这两件**处理方式完全相反**的事，在客户端看来一模一样 | `LLMResult` 增加 `finish_reason` + `truncated`；流式（最后一个 chunk）与非流式都捕获；`chat()` 如实透传（本次补上后又在 `chat()` 里漏传一次，`truncated` 恒 False、重试逻辑全是死的 —— 被第 [7] 节用例钉住）；正文为空 + `length` 时报 `truncated` 而不是 `empty` |
| E30 | §7.2「提取其中的 JSON」未设边界 | `find_json_object` 在外层括号不平衡时会**往后找下一个 `{`**，返回一个「括号平衡但语义错位」的碎片。真机实况里该碎片恰好含 `scripts.primary.text` → 被当成「有结果」→ **修复重试反被绕过**，用户拿到「五步全空、只有话术卡」的残缺品，界面还显示「部分结构化」 | 整段本来就是 JSON（以 `{` 开头）时**不再往后找**，宁可承认失败；补括号后**顶层结构仍不合契约**的，按纯文本降级保留原文，不拿残缺结构去渲染 |
| E31 | §7.2「宽松修复」范围太窄 | 只处理尾逗号与中文引号 → 模型**手滑少写一个 `}`** 就整份降级，而这种情况补个括号就能零成本救回 | 新增 `_close_brackets()`：补未闭合的括号与被切断的字符串（跳过字符串内与转义引号），但**不猜中间缺的分隔符**（那是编造）。补出来的结果若结构完整即判为成功，并留痕 `MODE_REPAIRED` |
| E32 | §5.3 契约只盯 `steps` | `validate()` 只校验 `steps` 内部字段，**不管顶层有没有 `scripts`** → 模型把 `scripts` 嵌进 `steps` 里时，只会报「scripts.primary.text 为空」等 7 条字段缺失，让模型以为漏了字段去补，而真正的问题是**层级写错了**。更糟的是 `ok` 只看「有没有一条能发的话术」，`scripts` 在、`steps` 缺时 `ok` 仍为真，**结构错位根本不会被发现** | `validate()` 补一条「缺少 scripts（必须与 steps 平级，不能嵌在 steps 里面）」；新增 `Analysis.well_formed`（顶层同时具备 `steps` 与 `scripts`）作为**结构错位**的判据，据此触发修复重试；`REPAIR_INSTRUCTION` 明确写出层级要求 |
| E33 | §8.4 未提配置的**默认值演进** | `save()` 写全量键 → 文件里的旧值**永久压住 DEFAULTS**，改了默认值却「没生效」，而且现象是「代码明明改了」，几乎不会有人先怀疑配置。更麻烦的是文件里**分不清**「用户主动设的」和「上一版默认值」 | 改成**差分保存**（只写与 DEFAULTS 不同的键，`.devdata/config.json` 由 2251 → 563 字节）+ **版本迁移**（`config_version` + `MIGRATIONS`，只升级「恰好等于旧默认值」的项，用户自己调过的值原样保留，迁移幂等） |
| E34 | §5.4「subprocess 调用官方 `memory_store.py`」在打包后**必然失效**，而 §11.2 只写了「one-dir 打包」 | 调用写法是 `[sys.executable, script, *argv]`。**打包后 `sys.executable` 是 `jev-chat-analyzer.exe`**，而且 `--windowed` 的进程没有控制台 —— 这条命令会去**启动第二个 GUI 实例**：拿不到 stdout（记忆全读成空）、还会多弹一个空窗口。源码运行永远发现不了，因为那时 `sys.executable` 确实是 python.exe | frozen 时改走 **进程内调用**：`_run_inproc()` 复用 `load_official()` 缓存的官方模块对象，临时替换 `sys.argv` / `redirect_stdout` / `redirect_stderr`，把 `SystemExit`（argparse 参数错）也吞成退出码。**还是同一个文件、同一份规则**，不是复刻实现。`probe/m4_unit.py` 第 [8] 节对两条路径做**逐字段等价性**断言（11 组命令序列、含中文写入→读回、官方错误信封、类别×来源越界、argparse 报错），另加「进程内不许再起子进程」的反向断言 |
| E35 | 同上，且更隐蔽：E34 修完**仍然会炸** | 官方每个子命令都写 `with connect() as conn:`，而 Python 的 `sqlite3` 上下文管理器**只提交事务、不关连接** —— 文件句柄要等 CPython 回收 Connection 才释放，而 Connection 与它的 statement cache 互为**引用环**，不主动 gc 就一直在。**子进程路径靠「进程退出，OS 强制释放句柄」掩盖了这一点**，进程内没有这层保护。现象很具体：`revoke --delete` / 清空记忆库报 `WinError 32`（另一个程序正在使用此文件），也就是说打包版的「撤回同意并删除记忆库」**永远失败**。顺带还是一处 Connection 泄漏 | `_run_inproc()` 每次调用后 `gc.collect()`（实测一轮回收近千个对象）。`probe/m4_unit.py` 有**E35 专项用例**：进程内连续 3 轮写入把引用环堆起来，再断言 `revoke --delete` 仍能删掉库文件 —— 防止将来有人顺手删掉那个 `gc.collect()` |
| E36 | §8.3「日志与保留」只说了保留策略，没提**打包后日志还写不写得到** | `--windowed` 打包后进程**没有控制台**：`sys.stdout is None`，CPython 的 `print()` 会**静默返回**（不是报错，是丢掉）→ 界面上「设置入口挂载失败」这类 warning 人间蒸发；更糟的是**启动期任何未捕获异常会让进程无声退出**，用户看到的现象是「双击没反应」，连一句错误都没有。§12 只列了探针测试，没有「打包产物的可观测性」这一项 | 新增 `app/logging_setup.py`：轮转日志写 `%APPDATA%/jev-chat-analyzer/logs/app.log`，接管 `sys.excepthook` **与 `threading.excepthook`**（采集/分析/召回全在 worker 线程里，而**线程里的异常不经过 `sys.excepthook`**），Qt 存活时弹窗告知日志位置。GUI 路径的 `print` 全部改为 `log()`。另：`--selftest` 结果**额外落盘**为 `<数据目录>/selftest.txt`（windowed 下没有 stdout 可看），让打包产物本身可验收 —— `build.ps1` 第 7 步就是靠这个文件判定 PASS/FAIL |
| E37 | §11.2「`skills/goutoujunshi` 作为 data 整体打入 `_internal`」这句话**按字面写参数就会错** | `--add-data "skills;."` 的目标 `.` 是 `_internal`，而 PyInstaller 复制的是**源目录的内容**而不是目录本身 → skill 落在 `_internal/goutoujunshi/`，而 `app/paths.py` 找的是 `_MEIPASS/skills/…`。**症状是「程序一切正常，只有 skill 相关的功能全空」**：自检里「skill 参考索引：0 份」、问题类型路由 FAIL、记忆层整段 FAIL，但窗口照开、采集照跑，很容易被当成「skill 没装」而不是「路径错了」 | 目标写成同名目录：`--add-data "skills;skills"`。`build.ps1` 第 6 步专门检查 `_internal/skills/goutoujunshi/{SKILL.md,scripts/memory_store.py}` 是否存在，把这个错法钉死在构建阶段 |
| E38 | §12 的测试策略里没有**打包产物**这一层的验收 | 「依赖 import 成功」被当成「功能可用」。实测：`import rapidocr_onnxruntime` 只说明包目录在，**完全不能说明三个 `.onnx` 模型也进了包** —— 而模型缺失时程序照常启动、界面照常显示，只在**第一次对真实聊天窗口 OCR 时**才炸，正好落在用户手里。§12 列的探针全跑在源码树上，**跑不到这一层** | 新增 `app/main.py --ocr-check [IMAGE]`：在**当前进程**里真跑一次 OCR（不给图就现场用 PIL 合成一张中英文图，不依赖 `probe/` 素材），结果写入 `<数据目录>/ocr_check.json`；`build.ps1` 第 7 步用它判定 PASS/FAIL，第 6 步另查 `*.onnx` 数量与 `cv2.pyd`。**实测合成图能稳定识别**（`微信聊天记录2026` 0.9991 / `OCR check 12345` 0.975），所以这条断言不是空转 |
| E39 | §11.3 只说「目标 < 400 MB」，没给**怎么压** | 首次 one-dir 实测 **420.1 MB，超预算 20 MB**。查下来三块全是死重：① `numpy.f2py`（Fortran 构建工具，运行时无人加载）**import scipy** → 把整个 scipy 拖进来 **68 MB**（含 `scipy.libs`）；② `--collect-submodules qfluentwidgets` 把 `qfluentwidgets.multimedia` 收进来 → 连带 `PySide6.QtMultimedia/QtQml/QtQuick/QtPdf` 与 ffmpeg (`avcodec-61.dll` 13 MB / `avformat-61.dll`) **约 50 MB**，而界面只用到 `FluentWindow`/标签/按钮/`InfoBar`，一个多媒体控件都没有；③ `opencv-python` 自带 `opencv_videoio_ffmpeg*.dll` **29 MB**，我们只喂静帧，从不解码视频 | ① 加 `--exclude-module numpy.f2py` + `scipy`；② 去掉 `--collect-submodules qfluentwidgets`（保留 `--collect-data` 取图标字体），靠静态分析按需收集，另加 `--exclude-module` 兜底；③ cv2 的 ffmpeg DLL 采取**打包期间在 site-packages 里临时改名、finally 还原**（PyInstaller 没有「排除某个 binary」的选项，而事后从 `dist/` 删文件很脆：沙箱策略、杀软、文件被占用都可能拒绝删除，构建不该建立在「被允许删除」之上；改名是原子的，也不会改到文件内容）。**删除/改名只做在真正没人加载的东西上**：`--exclude-module scipy` 之后自检里 rapidocr 仍要真跑推理，藏起 ffmpeg 之后紧接着跑 OCR 冒烟 —— 猜着删是另一回事。`build.ps1` 第 5 步同时打印 `_internal` 的**分项体积**（原先只打总数，看不出去哪了） |
| E40 | §11.2 只说把 skill「作为 data 整体打入」，没人问过**它自己的 import 谁来管** | `skills/` 是**以数据文件形式**进包的，PyInstaller 的模块图分析**看不见它里面的 import**。官方 `memory_store.py` 需要 `sqlite3` 与 `uuid`，而本应用自己的代码从头到尾没用过这两个 —— 于是它们不进包，打包版第一次碰长期记忆就是 `ModuleNotFoundError: No module named 'sqlite3'`。**比 E34/E35 更彻底：那两条修好后调用方式是对的，但底层模块根本不在。** 而且这个缺口**源码运行永远复现不了**（源码环境里什么都有），只有在打包产物上跑 `--selftest` 才会露出来 —— 这正是 E36 加上「诊断入口落盘」的直接回报 | 新增 `scripts/scan_skill_imports.py`：构建时用 `ast` 扫 `skills/**/*.py` 的顶层 import（只取标准库，相对 import 与 `_` 私有扩展跳过），逐个变成 `--hidden-import`。**用扫描而不是手写清单**，这样 skill 升级引入新 stdlib 模块时不会静默漏掉；另在 `--hidden-import` 里**显式保留 `sqlite3`** 作兜底，因为它是「缺了就整块功能作废」的那个。`python scripts/scan_skill_imports.py skills/goutoujunshi` 可直接单跑，当前输出 12 个：`argparse,datetime,json,math,os,pathlib,re,sqlite3,subprocess,sys,typing,uuid` |
| E41 | §11.2 只讲到「打包出 `dist/`」，**没有区分「构建产物」与「可分发产物」** | `dist/` 直接发给别人会同时踩四个坑：① one-dir 的 `exe` 离开 `_internal/` 就是废的，而普通用户看到的是双击报错；② 接收方不知道该工具**需要自备 API Key**，装好后「能启动、一按分析就报错」，多数人会直接判定是坏软件；③ **最危险的一条**：构建树里可能躺着发送方的本地数据 —— `config.json`（内置 **API Key 的 DPAPI 密文**）、`*.sqlite3`（长期记忆库，含真实人物档案与聊天推断）、`logs/app.log`、`llm-cost-*.json`，一旦随包发出就是**静默泄漏密钥与聊天记录**（包能正常解压、程序照常运行，没有任何人会察觉）；④ 说明文档若用无 BOM UTF-8，旧版记事本按 ANSI 解码 → 中文说明全是乱码 | 新增 `scripts/make_release.ps1`：把 `dist/` 复制到 `release/_staging/`（**构建产物保持纯净**）→ 拷入 `packaging/` 下面向使用者的《先读我-README-FIRST.txt》与三份许可文件 → 顶层 `.txt`/`.md` **统一转 UTF-8 with BOM** → **隐私门断言**（扫描 `config.json`/`*.sqlite3`/`logs/`/`selftest.txt`/`ocr_check.json`/`llm-cost-*.json`，命中即 `throw`，不发包）→ 以 `includeBaseDirectory: true` 打成**根即应用目录**的 zip，打印体积与 SHA256。另新增 `scripts/verify_release.ps1`：把 zip **解压到干净目录**、重跑隐私门、用**私有空数据目录**（等价于别人电脑上首次运行）跑打包 exe 的 `--selftest` 与 `--ocr-check`，**按退出码判定** |
| E42 | §12 的测试策略没有约束「**构建/验收脚本自身怎么判定**」 | `build.ps1` 里曾写 `if ($txt -match '全部通过')`。PowerShell 5.1 按 ANSI 解码无 BOM 脚本，中文字面量被误解码 → 断言**永不命中**，于是一次 8 步全绿的构建被报成 FAIL。若反向写成「不命中就算通过」则更糟：**失败会被报成成功**。教训不是「给脚本加 BOM」，而是**别拿文案做判定** —— 脚本本来就存在编码不确定性，而文案还依赖被判定方的措辞不变 | 改用**退出码**判定：`--selftest` / `--ocr-check` 直接看进程退出码（E36 已让两个入口把结果落盘，报告只用于人看与排错，不再参与判定）。`build.ps1` 与 `verify_release.ps1` 现均为**纯 ASCII** 且判定只看 `exit code`，两者都在文件头写明这条约束 |





---

## 1. 项目定位

### 1.1 目标用户
想用狗头军师的分析能力，但希望**直接从微信取素材、在桌面界面里看分析与话术**的人。

### 1.2 范围（v1 In-scope）
- 微信 PC 窗口截图 + OCR → 结构化 transcript。
- 导入已有导出文件（txt / html / ChatLab agent JSON）→ 同一 transcript 管道。
- 加载 `goutoujunshi` skill（`SKILL.md` + 按需 `references/`）注入 **OpenAI 兼容 LLM**，产出五步分析 + 话术卡。
- 可选长期记忆（同意门控、可撤销）。
- PyInstaller one-dir 打包，脱离 Python 运行。

### 1.3 非目标（v1 明确不做 · Non-goals）
- **不**读取 / 解密微信数据库，**不**自动发送消息，**不**绕过权限采集。
- **不**支持群聊 / 多目标对象分析（v1 仅支持「我 ↔ 一个对象」两方；多对象留待 v2，见 §6）。
- **不**用内置 Laya 生成回复文案（Laya 仅作可选旁路信号）。
- **不**做关系诊断 / 心理疾病判断（沿用 skill 边界）。
- **不**做跨平台（仅 Windows）。

---

## 2. 核心能力

| 能力 | 说明 |
| --- | --- |
| 微信窗口监控 | 定时/手动截取微信 PC 主窗口，OCR 出文字，识别气泡左右 → 说话人 |
| 说话人锁定 | 首轮让用户确认「我在左侧还是右侧」，锁定 `用户/对象` 映射 |
| 导入导出文件 | txt / html / ChatLab agent JSON，预处理后进入同一 transcript 管道 |
| Skill 分析 | 按问题类型加载对应 references，构造 system prompt 调用 LLM |
| 话术卡 | 分析 + 首选回复 + 稳健/会撩/强势三版本 + 发送时机 + 后续分支 + 观察窗口 |
| 长期记忆 | 首次同意后写入精简档案与事件（**官方** `memory_store.py`，体积封顶） |
| 自动更新 | 复用 `app/update.py` |
| 打包 | PyInstaller one-dir → `jev-chat-analyzer.exe` + `_internal/` |

---

## 3. 复用映射（关键优化：不新造轮子）

`D:\Jev\jev-chat-src` 里**已经跑通**下列能力，本项目应**改造复用**而非重写：

| 本项目的模块 | 复用来源（jev-chat-src） | 改造要点 |
| --- | --- | --- |
| `capture/window_capture.py` | 【已存在】`app/capture.py` | 窗口枚举 + HWND 截图；确认/补齐滚动重截 |
| `capture/ocr.py` | 【已存在】`app/ocr.py` | rapidocr 包装，返回 `(text, box)`；补置信度 |
| `capture/speaker_mapper.py` | 【已完成】参考 `core/questions.py` 风格 | 左右 → 说话人建议值 + 用户确认落配置 |
| `capture/transcript.py` | 【已完成】 | 帧去重/排序/编号/间隔/导出（算法见 §5.2） |
| `capture/collector.py` | 【已完成】 | 编排采集（Qt 无关，CLI/GUI 共用） |
| `capture/importer.py` | 【已完成】 | 解析 txt/html/chatlab json |
| `analysis/skill_loader.py` | 【已完成】 | 读 `SKILL.md` + `references/` 建索引（含路由漂移校验） |
| `analysis/questions.py` | 【已完成】 | 问题类型目录 + 危机信号扫描 |
| `analysis/prompt_builder.py` | 【已完成】参考 `core/engine.py`、`core/draft.py` | 组装 system/user prompt |
| `analysis/llm_client.py` | 【已完成】`core/jev_client.py` | OpenAI 兼容；**改用标准库 urllib**（E18）+ 三档结构化输出 + 取消语义（E16） |
| `analysis/parse_output.py` | 【已完成】部分参考 `core/draft.py` | 结构化解析 → 话术卡（含结构判定与降级，E17） |
| `analysis/engine.py` / `worker.py` | 【已完成】参考 `core/engine.py`、`app/worker.py` | 编排与线程桥 |
| `analysis/laya_signal.py`（可选） | 【延后到 v2】`probe/probe_laya*.py`、`core/engine.py` | 旁路概率信号（不进 v1 默认包） |
| `memory/store.py` | 【已完成】`goutoujunshi/scripts/memory_store.py` | **只封装**官方实现（subprocess），并解析其出错信封（E21）；官方模块另用 `importlib` 加载，仅用于**写入前预检** |
| `memory/consent.py` | 【已完成】新建 | 同意门控：记「看过并同意过哪一版说明」，策略版本或文案变化后**重新征求** |
| `memory/extract.py` | 【已完成】新建 | 从分析结果提取候选记忆；**只产出事件与假设**（E27） |
| `memory/service.py` | 【已完成】新建 | 用例编排（同意/建档/召回）；核心不依赖 Qt，探针里塞假 `ask` 回调就能测全分支 |
| `ui/*` | 【已存在】参考 `app/overlay.py`、`app/settings.py` | 视觉/设置复用 |
| 线程模型 | 【已存在】`app/worker.py` | 后台任务范式 |
| 设置持久化 | 【已存在】`app/settings.py` | 配置读写 |
| 版本号 | 【已存在】`app/version.py` | 版本管理 |
| 自动更新 | 【已存在】`app/update.py` | 升级通道 |
| 可行性验证 | 【已存在】`probe/probe_ocr*.py`、`probe_win*.py`、`probe_printwindow.py` | 保留作 M-1 冒烟 |

> 原则：**能复用就不要重写**；仅当现有实现与 skill 的边界要求冲突时才改写。

---

## 4. 整体架构

### 4.1 分层

```
┌─────────────────────────── Desktop App (PySide6 + qfluentwidgets) ─────────────────────────┐
│  main_window                                                                                │
│    ├─ capture_panel   监控/截图/导入 → transcript                                          │
│    ├─ analysis_panel  五步分析 + 话术卡(复制/计算后发送)                                    │
│    └─ profile_panel    档案/记忆 查看·暂停·撤销·清空                                        │
└───────────────┬────────────────────────────┬────────────────────────────────┘
                │                            │
        capture 层                     analysis 层
  window_capture → ocr → speaker_mapper │ skill_loader → prompt_builder → llm_client
                 → transcript          │ memory(记忆召回)        │
       importer → transcript            └─ laya_signal (可选概率信号)
                │                            │
                ▼                            ▼
          transcript.json  ──►   OpenAI 兼容 API（云端 或 本地 Ollama/Qwen）
```

### 4.2 数据流（一轮分析）

```
截屏/导入 → OCR/解析 → 说话人锁定 → transcript(带编号·间隔·发送人)
   → 问题类型路由 → 加载 skill 上下文(+档案召回) → LLM 调用
   → 结构化输出 → 分析面板渲染 → (可选)记忆自动更新
```

### 4.3 工程结构（标注现状）

```
D:\Jev\jev-chat-analyzer\
├── README.md                     # 【已存在】原始计划书（不动）
├── README.optimized.md           # 【本文件】优化版规格
├── LICENSE                        # 【已完成】MIT（本应用）
├── THIRD_PARTY_NOTICES.md         # 【已完成】goutoujunshi=MIT(powerycy)、jev-chat-src=MIT(rezoch340)
├── pyproject.toml / requirements.txt   # 【已完成】
├── config.example.json            # 【已完成】
├── app/
│   ├── main.py                    # 【已完成】入口 + --selftest
│   ├── config.py                  # 【已完成】DEFAULTS / 点号读写 / DPAPI / 红线强制
│   ├── paths.py                   # 【已完成】源码与打包两种运行方式
│   ├── version.py                 # 【已完成】
│   ├── ui/                        # 【已完成，M1 已接真采集】
│   │   ├── _qt.py                 #   qfluentwidgets 优先、原生回落
│   │   ├── main_window.py
│   │   ├── capture_panel.py       #   监控开关 / 预览 / 说话人确认 / transcript / 导出
│   │   ├── analysis_panel.py      #   右栏（M3 接真实调用）
│   │   └── profile_panel.py
│   ├── capture/
│   │   ├── window_finder.py       # 【已完成】发现 + 可截取性 + 空白帧探测（修正版）
│   │   ├── window_capture.py      # 【已完成】WGC 盯窗口 + 消息区像素定位（= 上游 capture.py）
│   │   ├── ocr.py                 # 【已完成】RapidOCR + who_said 底色分类（= 上游 ocr.py）
│   │   ├── speaker_mapper.py      # 【已完成】底色→左右建议值 + 用户确认落配置
│   │   ├── transcript.py          # 【已完成】schema / 编号 / 间隔 / 时间锚点 / 判重 / 导出
│   │   ├── collector.py           # 【已完成】编排：帧→行→transcript（Qt 无关，CLI 与 GUI 共用）
│   │   ├── worker.py              # 【已完成】QThread 桥 + 跨线程请求队列
│   │   └── importer.py            # 【已完成】txt/html/ChatLab 导入（策略链 + 编码降级 + 不猜说话人）
│   ├── analysis/                  # 【已完成】见下方展开
│   │   ├── questions.py           # 【已完成】12 种问题类型（SKILL.md 按需加载表的可执行镜像）+ 危机扫描
│   │   ├── skill_loader.py        # 【已完成】SKILL.md（剥 frontmatter）+ 43 份 references 索引 + 预算截断 + 路由漂移校验
│   │   ├── prompt_builder.py      # 【已完成】system/user 组装 + token 预算截断 + [ #id ] 引用过滤
│   │   ├── schema.py              # 【已完成】analysis.json 的 JSON Schema + 零依赖校验/软提醒
│   │   ├── llm_client.py          # 【已完成】urllib + SSE 流式 + 三档结构化输出 + 取消/重试/费用与每日上限
│   │   ├── parse_output.py        # 【已完成】提取/修复/结构判定/降级 + 五步与话术卡渲染
│   │   ├── engine.py              # 【已完成】编排：危机扫描→载参考→prompt→LLM→解析→一次修复→渲染
│   │   └── worker.py              # 【已完成】QThread 桥（流式增量信号 + 可取消）
│   └── memory/                    # 【已完成】M4
│       ├── store.py               # 【已完成】只封装官方 memory_store.py（子进程）+ 出错信封解析 + 写入前预检
│       ├── consent.py             # 【已完成】同意门控（policy_version + 文案哈希，变了就重新征求）
│       ├── extract.py             # 【已完成】候选记忆提取（只产出事件与假设，模型推断不进档案类）
│       └── service.py             # 【已完成】用例编排：同意 / 建档 / 召回（核心不依赖 Qt，可单测）
├── skills/goutoujunshi/           # 【构建期拷贝】来自 D:\Jev\goutoujunshi（见 §9）
├── probe/                         # 【已完成】m1_unit / m1_transcript_live / m1_gui_live / m2_unit /
│                                  #          m2_gui_import / m3_unit / m3_gui_live / m3_live（真机，已跑通）/
│                                  #          m4_unit / m4_gui_live / m0_gui_smoke /
│                                  #          fake_llm（假模型服务器：可模拟三档拒绝 / 5xx / 挂起 / 生成中途卡住 /
│                                  #                    **finish_reason=length 的输出截断**）/ snapshot_chat / diag_framerate
│                                  #          报告：m1_report / m1_transcript_report / m2_import_report /
│                                  #                m3_report + m3_live_report / m4_report
└── scripts/{dev.ps1,build.ps1}    # 【已完成】
```

---

## 5. 模块设计

### 5.1 capture 层（微信采集）
- **窗口发现**：枚举**可见**顶层窗口，按**进程名 + 标题**匹配微信 PC —— 微信 4.x 进程 `Weixin.exe`（3.x 才是 `WeChat.exe`），标题「微信」为主窗口。**已实测**：`capture.py::find_wechat_hwnd()` 就是这套逻辑（同进程还有「Weixin」工具窗、「图片和视频」看图窗，面积可能更大，**不能按面积挑**）。窗口类名（4.x `Qt51514QWindowIcon`）**仅作兜底匹配**，不作主键；多窗口时让用户选句柄。
- **截图**：`windows_capture`（版本对齐 `jev-chat-windows`）按 HWND 截取；DPI 按 1.0/1.25/1.5 适配，**并处理运行中 DPI 变化与多显示器**（监听 `WM_DPICHANGED` / 重新取窗口矩形）。
- **滚动补全**：可选「自动滚动 + 多次截取 + 帧间去重」拼更长的记录；默认只分析当前可视内容。
- **OCR**：`rapidocr_onnxruntime`，输出每行 `(text, box, score)`；低于阈值标 `[不确定]`。
- **说话人映射**：以**气泡底色**为主判据（微信里「我」的气泡恒为绿色），x 位置仅作**交叉校验**，不单独使用。理由：`who_said()` 实测「绿底=我」成立率 100%，且与 x 位置的自洽率同样 100%（72 行气泡：me 16 行全在右侧、her 56 行全在左侧）。
  **首轮仍必须由用户确认**「我＝右侧/左侧」并存入配置（不按气泡大小/颜色静默推断）——底色判据是给程序用的，用户确认是给用户用的，两者互为兜底：
  - 用户改了聊天气泡配色 / 换了主题 → 底色判据失效，x 校验接管并提示用户重新确认；
  - 群聊里发言人名是灰字（被正确丢弃），但多人混排会让 her 归属不清 → v1 见 §1.3，UI 须提示切到单聊。

### 5.2 transcript（会话组装）
每行契约：`#id 时间 sender(me/obj-1) 原文`，并记录相邻间隔（秒）与主动消息统计。

**transcript.json schema（建议）**
```json
{
  "version": "1.0",
  "source": "ocr|capture|import",
  "captured_range": ["2026-09-23T19:00:00+08:00", "2026-09-23T19:06:00+08:00"],
  "window": { "class": "WeChatMainWndForPC", "hwnd": 123456, "dpi": 1.25 },
  "speaker_map": {
    "me": { "side": "right", "code": "me", "confirmed": true },
    "objects": [ { "code": "obj-1", "side": "left", "label": "对象A" } ]
  },
  "messages": [
    { "id": 1, "ts": "2026-09-23T19:01:03+08:00", "gap_s": 0, "sender": "me", "type": "text", "text": "…" },
    { "id": 2, "ts": "2026-09-23T19:01:20+08:00", "gap_s": 17, "sender": "obj-1", "type": "text", "text": "…" }
  ],
  "warnings": ["OCR 低置信度 3 行", "存在 1 条 [图片] 未识别"]
}
```
`type` ∈ `text | image | voice | sticker | unknown`（缺失即标注，**不虚构**）。

**消息时间来源（E8 落地版，`app/capture/transcript.py`）**
微信消息本身不带时间戳，只有居中灰字分隔行（「14:01」/「昨天 14:01」/「9月21日 14:01」）。优先级：
1. `wechat_mark` —— 用最近的灰字时间戳锚点，**可信**，`gap_s` 才算得准；
2. `before_mark` —— 首帧画面里位于锚点**上方**的历史消息，取「锚点 −1 秒」。绝不能拿采集时刻凑，
   否则会出现 `20:07 → 14:01` 的时间倒流（M1 真机踩到过）；同时在 `warnings` 声明是估算上界、
   且首条可能是**上一段对话的结尾**；
3. `capture` —— 完全没有锚点时才用采集时刻，必然写进 `warnings`。

**帧间去重算法（建议）**
1. 逐帧 OCR → 行列表 `(text, box)`。
2. 相邻帧做**重叠窗口对齐**：以最后 k 行的文本归一化（去空白/标点）后计算相似度；整体匹配率 ≥ 0.85 视为同一屏。
3. 匹配到的前缀丢弃，仅追加新增尾部行（滚动拼接）。
4. 行级去重：Levenshtein 归一距离 < 0.15 且时间戳同分钟 → 视作重复。
5. 参数（`match_ratio`、`dup_threshold`、`k`）写进 config，可调。
   **注意（E14）**：第 4 条只适用于**采集**路径。`source="import"` 时写死 `dup_threshold=0.0` 关掉它——
   导入文件是权威快照，没有帧重复问题，而同一人连发两条一模一样的（「行」「行」）是真实发生的。

**导入路径（`app/capture/importer.py`，M2 落地）**
输入 txt / html / ChatLab agent JSON，输出**同一个** `Transcript`（同一套编号、间隔、schema 校验）：

| 输入 | 解析路线 | 关键处理 |
| --- | --- | --- |
| `txt` | 5 条正则策略按「命中行数最多」择优 | 多行气泡并成一条；`[图片]/[语音]/[表情]` → `type` 且**保留原文**；承上补日期；续行归属上一条 |
| `html` | 优先 `<table>`（自动识别「时间/昵称/内容」三列）；不成立则剥标签回退到 txt 策略 | `<img>` → `[alt]` 占位；单元格内 `<br>` 保留换行不丢字；先读 `<meta charset>` |
| ChatLab | 信封 → `data.messages`（`--format json`）**或** `data.text`（`--format agent`）两条路线 | `[#id]`/`[#id*]`/`[#a-b]` 标记 + `--- 日期 ---` 分块；`src_id` 保留上游证据编号；失败信封 `ok=false` **原样报错**；`meta.hasMore` 警告「只是一页」 |

三条硬规则：**不猜说话人**（见 E13）、**不虚构内容**、**禁止静默失败**（认不出格式/缺日期/超长截断/编码退化都进 `warnings`）。
`Message.src_id` 是给导入路径用的上游证据编号（ChatLab 的 `[#1021]`），采集路径为 `None` 且**不写进 JSON**，不污染既有契约。

### 5.3 analysis 层（分析引擎）
- **skill_loader**：启动读 `SKILL.md`（**剥掉 frontmatter**，见 E20）；`references/` 建 标题↔路径 索引（43 份）；**按需 1–3 份**，不批量全载；单份与总量都有字符预算，超了 head 截断并留可见标记。
  > 实测加固：`validate_catalog()` 会逐条核对问题类型表里的路径是否真的存在——SKILL.md 改名、skill 升级导致的**路由漂移会当场报错**，不会静默退化成「没有参考可用」。
- **问题类型路由**（映射 SKILL.md「按需加载」表）：12 种类型（见下表），`max_references=3`；「关系趋势/长记录」标记 `wide_context`，预算 × `wide_context_multiplier`（默认 2，看趋势不能只看最近几条）。
- **危机扫描（新增，对应 §16 与 skill「紧急例外」）**：对 transcript 与用户诉求做**提示词层面的**关键词加急（家暴/跟踪/胁迫/自伤/未成年/财务控制），命中则
  ① 强制追加 `knowledge/17-中国法律安全与危机转介.md` 与 `08-同意边界性与亲密.md`；
  ② system prompt 插入「紧急例外」段（先确认当下安全 → 给可信支持与紧急服务 110/12338/12345 → 不得按普通恋爱咨询淡化 → 建议必须合法、低风险、可退出）；
  ③ UI 常驻红色横幅。
  > 它是**加急通道而不是分类器**，宁可多报一次；命中了也照常给完整分析（不拒答），只是把安全放在第一位。
- **prompt_builder**：system = SKILL.md 全文 + 选中 references +（档案/记忆召回）；user = 会话元信息（说话人锁定、条数/字数/主动比/最长间隔、**时间可信度告警**）+ transcript 摘要（按 token 预算截断为最近 N 条并**标注截了几条**）+ 用户目标/情绪强度/补充。
  > **引用可核对**：契约要求 `facts.known` 每条以 `[#id]` 开头，解析后会把记录里**不存在的编号**统一替换成 `[#?]` 并记进 `citations_dropped`（不让用户以为真有第 99 条）。
- **llm_client**：OpenAI 兼容 `POST {base}/chat/completions`，**标准库 urllib**（E18）；结构化输出三档自适应阶梯（E15）；流式 SSE；超时/重试/取消（E16）；token 用量与费用统计 + 每日成本上限（E19）。
- **parse_output**：见 §7.2（含 E17 的结构判定与降级）。
- **修复重试的取舍**：只在**渲染不出来**（`Analysis.ok` 为假，即拿不到可发送的成品话术）时才多花一次调用做格式修复；能渲染的部分结构化结果只加横幅提示，不多打一次——每次调用都是真钱。
- **laya_signal（可选开关）**：仅旁路展示情绪/热度/风险概率，**不进入回复文案**。

**问题类型 → 参考映射**（`app/analysis/questions.py`，路径逐条校验）

| key | 用户看到的名字 | 加载 references（≤3） |
| --- | --- | --- |
| `reply` | 这句怎么回 | `practical/实战话术编排器：从一句回复到后续分支.md` |
| `invite` | 邀约/开场/第一次见面 | `practical/主动表达、第一次见面与自然接触.md`、`practical/场景感、松弛感与社交校准：从接话到关系推进.md` |
| `imbalance` | 怠慢/投入失衡/要不要退 | `practical/关系投入失衡：互惠判断、降级投入与退出决策.md` |
| `conflict` | 冲突/吵架 | `knowledge/07-沟通冲突与修复.md`、`practical/实战话术编排器…` |
| `attachment` | 依恋/焦虑 | `knowledge/03-依恋理论与情绪调节.md` |
| `trend` | 关系趋势/长记录（宽上下文） | `practical/ChatLab聊天记录分析适配.md` |
| `pua` | 冷读/PUA 担心 | `knowledge/05-PUA操控与伦理替代.md`、`knowledge/20-经典社交体系的机制、证据与风险边界.md` |
| `mbti` | MBTI/人设匹配 | `knowledge/04-MBTI人格与匹配.md` |
| `boundary` | 同意/性/亲密边界 | `knowledge/08-同意边界性与亲密.md` |
| `crisis` | 安全/法律/危机 | `knowledge/17-中国法律安全与危机转介.md` |
| `memory` | 跨任务档案/记忆 | `practical/长期记忆与关系档案.md` |
| `default` | 默认（五步法） | `practical/实战话术编排器…` |

（原 §5.3 表格里的 8 行是本表的子集；新增 `mbti`/`boundary`/`crisis`/`memory` 四类，均取自 SKILL.md 的「按需加载」表，未自行发明。）

### 5.4 memory 层（长期记忆）
- **只使用官方** `goutoujunshi/scripts/memory_store.py`（subprocess 调用）。理由：纯标准库、零第三方依赖、可离线；**禁止**再写一份「同 schema 内置存储」（避免逻辑分叉）。
- 五类 scope：`user / object / relationship / event / hypothesis`。
- 体积控制：单条 ≤200 字；`event` ≤20/对象、`hypothesis` ≤5、撤销栈 ≤20、总行 ≤200。
- 用户控制：`enable / revoke / undo / forget-object / clear` 全走 UI。
- 召回只注入当前对象：`user` 稳定档案 + `object` 快照 + 最近事件摘要（≤ ~4000 字）。

#### 5.4.1 官方 CLI 的真实契约（黑盒实测，见 E21–E26）

**子命令与必须带的旗标**（少了旗标就直接失败，不是「可选」）：

```
status                                     # 永远安全：库不存在也回 {"exists": false}（退出码 0）
enable   --confirm                         # 不带 → CONFIRMATION_REQUIRED
pause / resume
apply    --json '<对象>'  |  --file <路径>   # 列表形式传参，不经 shell
undo     [--op-id <id>]                    # 不带 id = 撤最近一次
show     [--subject-id <id>]               # 是旗标，不是位置参数（E22）
context  [--subject-id <id>] [--max-chars 500..8000]
forget-object <subject_id> --confirm       # 会连撤销栈一起清空，不可逆（E25）
revoke   --confirm [--delete]
clear    --confirm
```

**出错是「退出码 1 + stdout 一段 JSON 信封」**（E21）：

```json
{"ok": false, "error": {"code": "MEMORY_LIMIT_REACHED", "message": "..."}}
```

`store.run_cli()` 把 `code` 捞出来并翻成人话（`MemoryResult.hint`），
界面显示的永远是「为什么失败 + 下一步做什么」，而不是一段裸栈。

**类别 × 来源的交叉约束**（写入前预检直接复用官方 `validate_delta`，不自己抄一份规则）：

| scope | 允许的 `source_type` |
| --- | --- |
| `user` | 只有 `user_explicit`（「用户稳定档案只接受用户明确陈述」） |
| `object` / `relationship` | `user_explicit` / `user_report` |
| `event` / `hypothesis` | 都行，但 `assistant_inference` **只能**进 `hypothesis` |

→ 由此推出**自动建档只产出两类**（E27）：`event`（`chatlab`，带 `#id` 证据与消息时间）
与 `hypothesis`（`assistant_inference`，带置信度）。`user`/`object`/`relationship`
只留**手动入口** —— 只有用户能说「这是我知道的」，而不是「这是模型猜的」。
不合法的候选在**复核窗里就灰掉并写明原因**，不在点保存之后才报错。

**封顶不对称**（E24）：

- `event` / `hypothesis`：**软上限 + 自动剪最旧**（超出条数按 `updated_at` 升序删），写入不会失败；
- `user` / `object` / `relationship`：**硬上限 + 报 `MEMORY_LIMIT_REACHED`**，实现是「先查条数再插」，
  所以实际能到 **上限 + 1** 条才拦；
- 总行超 200 时先删 `event`/`hypothesis` 的最旧行，仍超才报错。

**`undo` 的粒度**（E25）：一次退一步，**每次 `apply` 就是一条操作记录（覆盖写入也算一条）**；
`forget-object` 会把 `operations` 表整个清空，所以删对象是不可撤销的。

**子进程 stdout 编码**（E26）：官方打的是 `ensure_ascii=False` 的中文 JSON，
Windows 下管道 stdout 走 `locale`（中文机 cp936）→ 必须给子进程注入
`PYTHONIOENCODING=utf-8`，否则读回来是乱码。**这个坑只在中文内容上暴露**，英文测试全过。

#### 5.4.2 分层与职责

```
consent.py   同意门控：记「看过并同意过哪一版说明」（policy_version + 文案哈希），
             变了就重新征求；本文件只是台账，库里的 consent_enabled 才是事实来源
extract.py   从 AnalysisRun 提取候选 → Candidate(scope/field/value/来源/置信度/理由)
             split_cites() 剥掉开头**全部** [#n]（只剥第一个会把 [#3] 漏进 field 标签）
service.py   用例编排：consent_plan / ensure_consent(ask=…) / write_candidates / recall_for
             核心不依赖 Qt —— ask 是注入进来的回调，探针塞假回调就能测全分支
store.py     只做进程与协议：run_cli / status_dict / 上面那堆语义化封装 / format_memories
```

**`config.memory.enabled` 只是缓存镜像**，不是事实来源：官方库里的 `consent_enabled` 才是。
缓存的作用有两个 —— 预检时省一次 250 ms 的子进程（实测每次调用约 250 ms），
以及记忆没开时**不再每轮分析都刷一条「召回不可用」的噪声告警**。
真正做决定的地方（召回失败、写入）一律以官方返回码为准。

UI 侧：`ui/profile_panel`（状态/清单/全部控制）、`ui/consent_dialog`（首次同意，勾选才可点）、
`ui/memory_review_dialog`（写入前复核 + 手动记一条）。
**子进程一律不进 UI 线程**（一次刷新要调两次 ≈ 500 ms），只有对话框和确认框留在 UI 线程。

### 5.5 ui 层
- 仿 `jev-chat-windows` 视觉：QFluentWidgets 卡片式左右双栏。
- 左栏：会话监控（窗口选择、截图预览、OCR 结果、说话人确认/逐条校正）。
- 右栏：五步分析 + 话术卡（三版本 Tab，一键复制）。
- 顶栏：档案按钮（MBTI/评分）、问题类型下拉、设置（API base/model/本地模式）。
- 首次使用弹一次「允许保存本机档案？」同意窗；撤销入口常驻。

### 5.6 线程模型（复用 `app/worker.py`）
- OCR 轮询、截图、LLM 调用**一律在工作线程**，经信号回主线程刷新 UI（PySide6 禁止跨线程操作控件）。
- LLM 请求可取消；切换对象/问题时取消在途请求，防串台。

### 5.7 更新机制（复用 `app/update.py`）
- 检查版本 → 下载 → 校验 → 重启替换；更新源可配置，失败可回退。

---

## 6. 对话状态与说话人锁定
- 同一转录内固定映射「me = obj 0/右侧，对象A = obj 1/左侧」，在分析区头尾展示（契约与 ChatLab 对应）。
- **v1 仅两方**；transcript schema 已预留 `objects[]` 数组，v2 扩展群聊时不破坏契约。
- 不根据内容/称呼/代词静默翻转；用户手动纠正后重新锁定。
- 图片/语音条：OCR 缺失时标 `[表情]/[图片]/[语音]`，不给虚构。

---

## 7. 分析输出格式

### 7.1 五步 + 话术卡
1. **情绪落地**：2–4 句指出感受、触发点与冲突。
2. **事实拆分**：已知事实 / 合理推测 / 关键未知，标证据编号。
3. **利益判断**：互惠、可靠、吸引、可行性、机会成本。
4. **明确建议**：一句首选 + 2–4 理由；随后 ≤3 版本话术（稳健 / 会撩·策略 / 强势）。
5. **行动收束**：一个现在能做的小动作、观察窗口或停止条件、值得回来反馈的信号。

「这句怎么回」模式：第一屏先给一条可复制成品，再写时机、代价与积极/含糊/不回应的后续。

### 7.2 结构化输出契约（替换脆弱的自由解析）
优先使用模型原生 JSON 模式；不支持的模型走「提示词约束 + 解析 + 一次修复重试」。

**analysis.json schema（建议）**
```json
{
  "steps": {
    "emotion": "…",
    "facts": { "known": ["[#12] …"], "inferred": ["…"], "unknown": ["…"] },
    "interests": "…",
    "advice": { "primary": "…", "reasons": ["…"] },
    "actions": { "next_action": "…", "watch_window": "…", "stop_condition": "…", "signals_to_report": ["…"] }
  },
  "scripts": {
    "primary": { "text": "…", "timing": "…", "cost": "…",
                 "branches": { "positive": "…", "vague": "…", "no_reply": "…" } },
    "variants": { "steady": "…", "flirty": "…", "assertive": "…" }
  },
  "citations": ["[#12]", "[#15]"],
  "confidence": "high|medium|low",
  "boundaries": ["未做诊断", "未保证效果"]
}
```
解析失败时：① 先做本地修复（去围栏 / 取括号平衡的对象 / 修尾逗号 / 补括号 / 拆信封）；
② 「输出被长度上限截断」要**先救长度**（放大 `max_tokens` 重试），不能去修格式（见 E28/E29）；
③ 仍拿不到可渲染结果，才**再花一次调用**让模型重做（`analysis.repair_retry`，明说「只修格式，不要改结论」）；
④ 再失败则降级为「纯文本五段」渲染并在 UI 标注。

**四级容错的实际顺序**（真机跑过之后定下来的）：

| 情况 | 判据 | 动作 | 额外成本 |
| --- | --- | --- | --- |
| 少写一个 `}` / 尾逗号 / 中文引号 | `loose_repair` 后能解析 | 本地补齐（结构完整才接受） | 0 |
| `finish_reason == "length"` | 服务端明说被截断 | 放大上限到 `min(2×, truncation_retry_max)` 重试一次 | 1 次调用 |
| 结构错位（顶层缺 `steps` 或 `scripts`） | `Analysis.well_formed == False` | 让它重做一次，并在指令里点名「必须与 steps 平级」 | 1 次调用 |
| 实在救不回来 | 上表都不成立 | 纯文本降级，**保留模型原文**在第一页 | 0 |

> **实测补充（E17 / E15 / E28~E32）**：括号扫描在**截断输出**上会捞到内部小对象（例如只剩 `steps.facts`）——
> 必须先判定「含 `steps` 或 `scripts` 才算分析结构」，否则降级保留原文，
> 而不是渲染出一屏「模型未给出」。结构化输出的档位由 llm_client 的三档阶梯决定，
> 实际档位与降级情况都会在界面如实标注。
>
> 真机进一步证明光有上面这条**还不够**：外层少一个 `}` 时括号扫描会**往后找下一个 `{`**，
> 捞回一个「括号平衡但语义错位」的碎片；它恰好含 `scripts.primary.text`，于是被判成「有结果」，
> **修复重试反被绕过**（E30）。所以整段本来就是 JSON 时不再往后找；
> 同时补括号后**顶层结构仍不合契约**的一律降级，不拿残缺结构去渲染。
>
> 「输出被截断」与「模型 JSON 写错」在客户端看来都是解析失败，但处理方式**刚好相反**：
> 截断时让模型「修格式」是白花钱（同一个上限，同一个位置再断一次），只能加大上限；
> 写错时加大上限没用，得让它重做。区分二者的唯一依据是 `finish_reason`（E29）。

### 7.3 成本与时延预算
- config 增加 `max_output_tokens`（默认 **4000**）、`truncation_retry_max`（默认 8000）、`request_timeout_s`、`daily_cost_cap`、`stream: true`。
  - **4000 这个值是真机实测出来的**：五步 + 四版话术的 JSON 需要 3k+ tokens（单次流式 4205 字）。
    给少了的表现不是报错，而是 JSON 写一半被当成「格式错误」，排查成本极高（E28）。
- 超出预算 / 超时 → 明确提示并保留已得部分；调用可取消（取消语义见 E16：流式 2 秒内生效，非流式 best-effort）。
- 每一轮分析在 UI 显示「本次约 X tokens / ¥Y」；**没配单价时显示用量但费用为未知**，并明确告警「每日上限不会生效」——不编金额、不假装在保护用户（E19）。
- 修复重试会**多一次调用**，因此只在渲染不出来时才触发（见 §5.3 / §7.2）。
  真机实测：一次成功约 7000 prompt + 1200 completion（**≈ ¥0.025**）；
  走一次修复重试翻倍（实测 13946 prompt + 2292 completion × 2 次调用 ≈ ¥0.045）。
- **估算会偏乐观**：`est_tokens` 按「中文 1.5 字/token」估算，实测同一份 prompt 估 5257、真机计费 6930（**低估约 30%**）。
  所以预算截断比预期宽松——想让上限真的严，得把系数调保守（已记入遗留）。

---

## 8. 隐私与数据流（新增）

### 8.1 数据分类与流向
| 数据 | 敏感级 | 去向 |
| --- | --- | --- |
| 屏幕截图 | 高 | **仅本机内存，永不落盘** —— 沿用 `jev-chat-src` 硬约束 3：位图全程 numpy/PIL 对象，不 `.save()`、不进日志、不上传。**v1 不提供「临时目录」选项**（与上游红线冲突） |
| transcript 原文 | 高 | 本机 + （云端模式下）**第三方 LLM API** |
| 长期记忆档案 | 中 | 本机 SQLite（`%LOCALAPPDATA%/goutoujunshi`，路径由官方脚本决定）；同意台账另存 `user_data_dir()/memory_consent.json`（只记「同意过哪一版说明」，**不含聊天内容**） |
| 日志 | 低 | 本机；**默认不记录聊天原文**，仅记事件与错误码 |

### 8.2 本地优先
- 默认提供 **本地 LLM（Ollama / 本地 Qwen）** 选项，敏感对话可全程不出机。
- 使用云端 API 时，首次分析弹出明确告知：「对话内容将上传至 <服务商>」并需确认。

### 8.3 日志与保留
- 日志脱敏：不落原文，必要时只留散列/长度。
- 提供「一键清除本机数据」（截图缓存 + 记忆 + 日志）。

### 8.4 凭据存储与配置演进
- **不**把 `api_key` 明文写入 `config.json`。改用 **Windows 凭据管理器（DPAPI）** 或环境变量；`config.json` 仅存非敏感项。
  实测确认 DPAPI 密文**跨进程可解**（同一 Windows 用户），所以「在界面里填一次」之后，命令行探针读同一份配置也能用，不必再传环境变量。
- **配置只写「与默认值不同的键」**（差分保存，E33）。
  早先写全量，后果是：默认值后来怎么改都影响不到已有配置——文件里的旧值会一直压住 `DEFAULTS`，
  而现象是「代码明明改了却没生效」，几乎不会有人先怀疑到配置上。
- **默认值要改就得走迁移**：`config_version` + `MIGRATIONS`（见 `app/config.py`）。
  只在「文件里的值**恰好等于旧默认值**」时才替换成新默认值 —— 用户自己调过的值原样保留；迁移幂等。
  理由：配置文件里的值**分不清**「用户主动设的」和「上一版默认值」，不显式迁移就只能二选一，两个都会错。

### 8.5 采集失败的用户告知（E6 产物）
- **禁止静默失败**：拿到空白帧（`frame_is_blank()`：唯一色 ≤40 且暗像素 ≤2%）时不产出空 transcript，
  必须在 UI 明确提示：「请手动点开微信主窗口并进入有消息的聊天」。
- 原因：微信 4.x 界面在内嵌 Chromium 子窗口，托盘隐藏态被程序强行唤起后子视图不恢复，
  此时任何截图法都只能拿到白帧——这是**环境状态问题**，不是程序 bug，唯一解法是用户手动打开。
- 反例（实测）：强行唤起后帧唯一色 9、暗像素 0.8%、OCR 0 行；用户手动打开后同一探针
  单帧 OCR 112 框、气泡行 72、置信度均值 0.99。

---

## 9. 与 goutoujunshi skill 的契约
- **单一事实来源**：以 `D:\Jev\goutoujunshi`（完整 git 仓库）为唯一权威源；构建时拷贝进 `skills/goutoujunshi`。
  > 注意：上一轮已把同一 skill 安装到 `C:\Users\chen\.workbuddy\skills\goutoujunshi`（供 WorkBuddy 内使用）。**两份拷贝会漂移**。
  > **实测勘误（E3）**：`D:\Jev\goutoujunshi` **没有 `.git`**（不是 git 仓库），`git pull` 无法执行。
  > 同步方案改为：开发期用**目录联接** `mklink /J skills\goutoujunshi D:\Jev\goutoujunshi`（Windows 免管理员）；构建期用 `robocopy /MIR` 拷快照。
  > 无论哪种，写死一条：**应用只读 `D:\Jev\goutoujunshi`**，不回写、不就地改。
- 引用证据用 `[#id]`；回答开头注明会话与时间范围、实际查询边界。
- 可导出/遵循 ChatLab 契约（`messages between --format agent`），让任何装了该 skill 的助手接手分析。
- 安全边界全部沿用（见 §16）。

---

## 10. 配置（config.example.json）

```json
{
  "llm": {
    "provider": "openai-compatible",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key_ref": "env:JEV_LLM_API_KEY",
    "mode": "cloud",
    "max_output_tokens": 1200,
    "request_timeout_s": 60,
    "daily_cost_cap": 2.0,
    "stream": true,
    "structured_output": "auto",
    "stream_read_timeout_s": 45,
    "max_retries": 0,
    "pricing": {}
  },
  "local_llm": { "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b" },
  "capture": { "target_process": ["Weixin.exe", "WeChat.exe"], "title_hint": "微信",
               "window_class_fallback": "", "scroll_frames": 0, "poll_ms": 5000,
               "ocr_min_score": 0.90, "dedup": { "match_ratio": 0.85, "dup_threshold": 0.15, "k": 5 } },
  "transcript": { "max_messages": 60, "token_budget": 3500 },
  "import": { "max_messages": 2000 },
  "memory": { "enabled": false, "max_context_chars": 4000 },
  "analysis": { "question_type": "default", "max_references": 3, "ref_chars": 9000,
                "total_ref_chars": 24000, "repair_retry": true, "wide_context_multiplier": 2.0 },
  "skill_source": "skills/goutoujunshi",
  "laya": { "enabled": false, "model_dir": "" }
}
```
要点：**零绝对个人路径**（`laya.model_dir` 留空，程序自动探测或让用户选择）；密钥走引用而非明文。

`llm.pricing` 默认为空：**没填单价时 UI 只显示 token 用量、不显示金额，并告警「每日成本上限不会生效」**——不编金额、也不假装有限额保护（E19）。要启用费用显示与上限，按自己账单填，例如
`"pricing": { "deepseek-chat": { "in_cny_per_mtok": 2.0, "out_cny_per_mtok": 8.0 } }`（键支持子串匹配，`"*"` 作兜底）。

`llm.structured_output`：`auto` = 三档自适应阶梯（默认）；`off` = 完全不发 `response_format`，只靠提示词约束（E15）。

`api_key_ref` 支持的三种写法（解析顺序即优先级）：
1. `env:NAME` —— 读环境变量（默认，最简；`config.json` 里不出现任何密钥）。
2. `keyring:SERVICE/ACCOUNT` —— Windows 凭据管理器（需 `keyring` 依赖，或用 ctypes 直接调 `CredRead/CredWrite`）。
3. `dpapi:BLOB` —— DPAPI 加密后的密文，绑定当前 Windows 用户。

另：`privacy.screenshots_to_disk` 恒为 `false`（见 §8.1，v1 不开放），`privacy.cloud_upload_confirmed` 由首次上传告知窗写入，不手工改。

> 完整可拷贝示例见仓库根目录 `config.example.json`（首次运行复制为 `config.json`）。

---

## 11. 依赖与打包

### 11.1 依赖分层
- **默认构建**：仅 `requirements.txt`（PySide6、qfluentwidgets、windows_capture、rapidocr_onnxruntime、onnxruntime、opencv-python、Pillow 等）。
- **LLM 客户端零新增依赖**：`app/analysis/llm_client.py` 只用标准库 `urllib`（E18）——不引 requests/httpx/openai，SSE 解析与超时/取消循环自写（已有假服务器 6 条路径测试）。json 校验也自己写（不引 jsonschema），报错文案是给人看的中文。
- **可选 extra**：`requirements-laya.txt`（torch 等），仅当开启 laya 信号才装；**v1 默认包不含**（这是把体积从 2.4 GB 压到 400 MB 以内的唯一杠杆）。
- 与 `jev-chat-windows` 版本对齐，并在文件内**锁定具体版本号**（不写 `>=`）。

### 11.2 PyInstaller one-dir
- 产出 `jev-chat-analyzer.exe` + `_internal/`；`skills/goutoujunshi` 作为 data 整体打入 `_internal`。
- **`--add-data` 的目标必须重复目录名**（E37）：写 `"skills;."` 会把 skill 的**内容**倒进 `_internal/` 根，
  而不是 `_internal/skills/`，于是 `paths.skill_dir()` 找不到它 —— 窗口照开、采集照跑，只有 skill 相关功能全空。
  正确写法是 `"skills;skills"`。
- `scripts/build.ps1` 封装流程，产物输出到 `dist/`，共 8 步：

  1. 从唯一真源 `D:\Jev\goutoujunshi` 同步 skill（robocopy `/MIR`）；
  2. 打包前跑 `validate_skill.py --runtime`，不通过就不往下走；
  3. `--selftest`（源码树，含分析层与记忆层 9 节检查）；
  3b. **扫 skill 的 import 并转成 `--hidden-import`**（E40）：`skills/` 是作为 data 进包的，
      PyInstaller 看不到它里面的 import，官方脚本要的 `sqlite3`/`uuid` 本应用自己又没用到；
  4. PyInstaller one-dir。期间把 `cv2` 里用不到的 `opencv_videoio_ffmpeg*.dll`（29 MB，我们只喂静帧）
     在 site-packages 里**临时改名**，`finally` 还原 —— 见 E39；
  5. 打印总体积 + **`_internal` 分项体积**（只打总数看不出体积去哪了），并写 `dist/SIZE_REPORT.txt`；
  6. 载荷检查：`skills/goutoujunshi/{SKILL.md,scripts/memory_store.py}`、`cv2.pyd`、`*.onnx` 数量；
  7. **打包产物冒烟**（见下）；
  8. 收集 `dist/THIRD_PARTY_LICENSES.txt`（本应用声明的第三方许可 + skill 的 MIT 全文 + `pip list` 快照）。

- **打包后的路径解析**（`app/paths.py`）：`is_frozen()` 为真时 `app_root()` = `sys._MEIPASS`，
  one-dir 下就是 `_internal/`，所以 `skills_dir()` = `_internal/skills` —— 与 `--add-data "skills;skills"` 严丝合缝。
  用户数据仍在 `%APPDATA%/jev-chat-analyzer`（可用 `JEV_DATA_DIR` 覆盖为便携模式）。
- **打包后的记忆层**：frozen 下 `sys.executable` 是 `jev-chat-analyzer.exe` 且没有控制台，
  subprocess 调官方脚本会变成「再开一个 GUI」，所以 `store.run_cli` 自动改走**进程内**调用官方 `main()`
  （E34），并且每次调用后 `gc.collect()` 释放官方 `with connect()` 留下的 sqlite 句柄（E35）。
- **打包后的可观测性**（E36）：`--windowed` 没有 stdout，`print` 会被静默丢弃、启动异常会让进程无声退出。
  所以日志统一走 `app/logging_setup.py`（`logs/app.log`，含 `threading.excepthook`，崩溃时弹窗），
  而两个诊断入口 `--selftest` / `--ocr-check` 的结果**都落盘成文件**，让「打包产物本身」可被验收。

### 11.3 体积目标
- 参考程序 v0.1.9 的发布包高达 **~2.4 GB**（主因 onnxruntime + torch）。
- 本项目默认不含 torch/laya，**目标发布包 < 400 MB**。**实测：首次 one-dir 420.1 MB → 按 E39 砍掉三块死重后 299.7 MB**（留 25% 余量）。
  分项（`_internal`，由 `build.ps1` 每次打印并留档 `dist/SIZE_REPORT.txt`）：

  | 项 | 体积 | 说明 |
  | --- | --- | --- |
  | PySide6 | 92.3 MB | Qt Core/Gui/Widgets/Network/Svg/Xml（Qml/Quick/Pdf/ffmpeg 已随 E39 去掉） |
  | cv2 | 82.3 MB | `cv2.pyd` 为主；ffmpeg 解码 DLL（29.4 MB）已在打包期间隐藏 |
  | onnxruntime | 35.8 MB | |
  | numpy(.libs) | 25.9 MB | |
  | rapidocr_onnxruntime | 15.4 MB | 含 3 个 `.onnx` 模型（15.4 MB） |
  | PIL | 12.8 MB | |
  | **skills** | **1.3 MB** | 官方 skill 整体（含 `references/` 43 份） |
  | 其它 | ~10 MB | shiboken6 / windows_capture / win32 / shapely / pyclipper / VC 运行库等 |
  | `jev-chat-analyzer.exe` | 11.5 MB | bootloader + PYZ + PKG |

- 已排除且**确认过没人加载**的：`scipy`（68 MB，由 `numpy.f2py` 连带）、`torch`/`transformers`/`laya`、
  `matplotlib`/`pandas`、`tkinter`、`qfluentwidgets.multimedia` 及其 Qt Qml/Quick/Pdf/ffmpeg 链。
- **砍体积的原则：只删「运行时真的没人加载」的东西，并且每条都在构建阶段被验证兜住** ——
  删 cv2 的 ffmpeg DLL 之后**紧接着**跑 OCR 冒烟，排除 scipy 之后自检里 rapidocr 仍要能真跑推理。
  不做「看起来大就删」的猜测。

### 11.4 分发给他人（发布包）

`dist/` 是**构建产物**，不是**可分发产物**，两者差三样东西；`scripts/make_release.ps1` 负责把差额补齐：

1. **接收方要的说明文件**。`packaging/` 放面向使用者的《先读我》（`先读我-README-FIRST.txt`）：怎么启动、
   第一次必须自己配 API Key（去哪拿、大概多少钱）、按钮级操作步骤、隐私提示、报错怎么自查、系统要求、怎么卸载。
   脚本**遍历 `packaging/` 整目录拷贝**，所以文件名可以是中文而脚本本身保持 ASCII-only。
2. **许可合规**：`LICENSE`（本应用 MIT）、`THIRD_PARTY_NOTICES.md`、`build.ps1` 产出的
   `THIRD_PARTY_LICENSES.txt`（含 skill 的 MIT 全文与 `pip list` 快照）一并放进包内顶层。
3. **一份可直接解压即用的 zip**：`release/jev-chat-analyzer-v<版本>-win64-<时间戳>.zip`，
   以 `ZipFile::CreateFromDirectory(..., includeBaseDirectory: true)` 生成，**压缩包根就是应用文件夹** ——
   接收方解压后得到一个干净目录，而不是 300 个文件散落在「下载」里。

两条硬约束：

- **隐私门（`== 5` 步，失败即 `throw`）**：发布包里**绝不允许**出现发送方的本地数据 ——
  `config.json`（含 API Key 的 DPAPI 密文）、`*.sqlite3`（长期记忆库）、`logs/`、`selftest.txt`、
  `ocr_check.json`、`llm-cost-*.json`。这几项一旦随包发出就是**同时泄漏 Key 与聊天记录**，
  而且是静默的（包能正常打开、程序能正常运行，没人会发现）。所以它是**断言**而不是清单项：
  「记得检查」迟早会忘。
- **顶层文本统一转成带 BOM 的 UTF-8**（`== 4b` 步）：说明文档原本是无 BOM UTF-8，
  Win11 记事本能嗅探出来，但旧版记事本与部分压缩软件的预览窗格会按 ANSI 解码 → 中文全乱码，
  恰好是接收方的第一印象。只处理顶层 `.txt`/`.md`，绝不碰包内第三方文件。

其余工程要点：

- **只发 exe 是打不开的**：one-dir 的 `jev-chat-analyzer.exe` 离开 `_internal/` 就是废的，
  而双击时的报错对普通用户毫无意义。说明文档第 2 句就点明这一点。
- **接收方必须有自己的 API Key**：本工具不含模型。默认走 DeepSeek（`base_url`/模型可在设置页改，
  任何 OpenAI 兼容端点都行）。Key 由对方自己填，用 DPAPI 存在**他的**机器上 —— 不要代填、更不要把自己的 Key 打包进去。
- **首次运行有 SmartScreen 拦截**：未签名的 exe 会弹「Windows 已保护你的电脑」，属预期现象，
  说明文档给了「更多信息 → 仍要运行」的指引；要根治只能买代码签名证书。
- **传输与校验**：脚本打印 zip 体积与 SHA256，接收方可自行比对。zip 约 130~160 MB（取决于压缩率），
  走微信/QQ 直发或网盘均可。
- **脚本不删除任何东西**：旧 staging 目录退到 `release/_staging_prev_<时间戳>`，
  zip 名带时间戳因此永不撞名 —— 与 `build.ps1` 同一套「不依赖删除权限」的约束。

---

## 12. 测试策略
- **单元测试**：transcript 组装（去重/排序/间隔）、说话人映射、prompt 组装与预算截断、`parse_output` 对样例 JSON 的解析与降级、LLM 客户端的错误分类与结构化输出阶梯、**记忆封装层对官方 CLI 契约的适配**（出错信封、`--confirm`、封顶边界、undo 粒度）。
- **契约测试**：transcript.json / analysis.json schema 校验；ChatLab 导出兼容；**问题类型路由的参考路径必须真实存在**（防 skill 升级导致的漂移）；**类别 × 来源的交叉规则直接复用官方 `validate_delta`**（不抄第二份规则，从根上避免漂移）。
- **假服务器端到端**：`probe/fake_llm.py`（stdlib `http.server`）按脚本返回正常/流式/按档位拒绝/5xx/挂起/**带 `finish_reason=length` 的截断**——**不联网、不花钱**就能覆盖成功与全部降级路径。
- **隔离原则**：凡是会写盘的探针（M4 全部）都把 `GOUTOUJUNSHI_MEMORY_DIR` 与 `JEV_DATA_DIR` 指到临时目录，并在结尾**断言用户真实目录未被改动**。
- **冒烟（慢速）**：`probe/m0_gui_smoke.py` 真建窗口 + 真跑事件循环（含档案页后台刷新）；`probe/*` 在真实微信窗口上跑 OCR/截图/去重，输出准确率报告。
- **真机（真模型）**：`probe/m3_live.py` 用真 Key 跑一轮并产出报告；**没配 Key 时明确跳过**，不伪装成功。
- **真机跑出来的坏输出要回灌成离线用例**（E28–E32 的教训）：假服务器只会返回「永远合规的小 JSON」，
  所以**离线全绿不能证明真机可用**。真机每一类失败（截断 / 结构错位 / 少括号 / 碎片捞取）都要在
  `probe/m3_unit.py` 第 [7] 节留下一条可复现的用例，否则下次改动还会重蹈。
- **跑探针要放宽命令超时**：M4 系列要跑几百次官方脚本子进程，**超过 Bash 默认的 120 秒**；
  卡在 120 秒被 SIGTERM 时表现得像「代码卡住了」，其实是命令超时（本次就误判过一次）。
  当前 `probe/m4_unit.py` 约 3 分钟（151 项，其中第 [8] 节为了对齐两条路径又跑了一轮子进程）。
- **skill 校验**：打包前跑 `python skills/goutoujunshi/scripts/validate_skill.py --runtime` 必须通过。
- **双路径等价性（M5 新增，E34/E35）**：`probe/m4_unit.py` 第 [8] 节把每串命令在
  「子进程」与「进程内」两条路径上**各跑一遍**（各用独立空库），再断言两边 `ok`/`code`/payload
  **逐字段等价**；同时覆盖「中文写入→读回没歪」（进程内是 `StringIO`，不再经过 cp936 那条路）、
  官方错误信封、类别×来源越界、argparse 报错被吞成退出码、`sys.argv`/`sys.stdout` 用完还原、
  **进程内绝不再起子进程**（反向断言：打包后那等于再开一个 GUI），以及 E35 专项
  「连续 3 轮写入堆出 sqlite 引用环后仍能删库」。**离线全绿不能证明真机可用，真机全绿也不能证明打包可用** ——
  这一节就是补后者那个缺口。
- **打包产物必须自己验收（E38）**：`build.ps1` 第 6/7 步不依赖任何源码树里的东西 ——
  查 `_internal/` 里的 skill、`cv2.pyd`、`*.onnx` 是否就位，再用 `Start-Process -Wait` 跑两次 exe：
  `--selftest`（此时 `is_frozen()` 为真，记忆层走进程内路径，**E34/E35 只有在这里才被真正执行**）
  与 `--ocr-check`（真跑一次 OCR 推理）。两个入口的结果都**落盘成文件**再读回来判定，
  因为 windowed 进程根本没有 stdout 可看。任何一步不 PASS 都在构建日志里直接可见。
- **发布包要按「接收方」的处境再验一遍（E41/E42）**：`build.ps1` 验的是 `dist/` ——
  在开发者自己的树里、自己的环境下。而「发给别人能不能用」是另一个问题，由 `scripts/verify_release.ps1` 回答：
  把 zip **解压到干净目录**（不继承任何东西）、断言**压缩包根就是应用目录**、在解压树上**重跑隐私门**、
  再用**私有空数据目录**（`JEV_DATA_DIR` 指到新建空目录，等价于别人电脑上首次运行、`%APPDATA%` 里什么都没有）
  跑打包 exe 的 `--selftest` 与 `--ocr-check`。
  注意判定方式：**只看退出码，不做文案匹配**（E42）。脚本本身也强制纯 ASCII ——
  PowerShell 5.1 会把无 BOM 脚本按 ANSI 解码，任何中文字面量都可能被误解码，
  用来做断言就会出现「全绿报 FAIL」或更糟的「失败报成功」。

---

## 13. 许可与合规（新增）
- **goutoujunshi** 为 **MIT License（Copyright (c) 2026 powerycy）**。打包进应用时**必须**：
  - 随包保留其 `LICENSE` 全文与版权声明；
  - 在 `THIRD_PARTY_NOTICES.md` 中列出。
- 本应用自身需声明 `LICENSE`（建议同样采用与依赖兼容的许可）。
- 复用 `jev-chat-src` 的代码：**已核对**，同为 **MIT License（Copyright (c) 2026 rezoch340）**，须一并署名。
- 参照程序 `jev-chat-windows` 仅作形态与依赖对齐参考，不直接复制其代码；若后续复制需再核许可。
- 合规红线：不规避微信权限、不绕过平台规则；内容仅用于个人咨询参考。

---

## 14. 里程碑与验收（可量化 DoD）

| 阶段 | 内容 | 验收（DoD） |
| --- | --- | --- |
| **M-1 验证** | 复用 `probe/*` 在真实微信截图测 OCR/说话人判定/去重（**已完成 2026-09-23**，见 `probe/m1_report.md`） | ✅ **通过**：OCR 置信度均值 **0.9895**／最低 0.9344／低置信(<0.6) **0 行**；同一帧连跑 3 次一致率 **1.0**；重复采样一致率 **1.0**；跨屏去重 **10/11 = 1.0**（另 1 次是切屏，判 0 正确）；说话人「绿底=我」成立率 **1.0**、与 x 位置自洽率 **1.0**（72 行气泡零冲突）；单帧 OCR 均值 **234 ms**（首帧冷启 931 ms）。**阈值据此标定**：`ocr_min_score` 0.6 → **0.90**；`match_ratio`/`dup_threshold`/`k` 维持 0.85/0.15/5 |
| M0 脚手架 | 结构、config、空 `main_window`、`dev.ps1`（**已完成 2026-09-23**） | ✅ **通过**：`python -m app.main --selftest` 全绿（6/6 依赖、config 读写往返、隐私红线强制 false、skill 与官方记忆脚本就位、窗口发现命中主窗口、空白帧探测）；GUI 冒烟真建窗口 1180×760、左栏下拉填 15 个候选并标出主窗口、3 秒自动退出无异常 |
| M1 采集核心 | 复用 capture/ocr → transcript + 说话人确认 + 导出（**已完成 2026-09-23**） | ✅ **通过**：真机单聊窗口 → 6 条消息带 `#id` 连续编号、会话名 `小新`、schema 校验通过；离线自检 **51/51**（`probe/m1_unit.py`）；GUI 端到端真跑 worker 线程出 transcript（`probe/m1_gui_live.py`）；两轮独立采集逐条一致率 **100%**；说话人建议值与 x 位置一致率 100%，确认后写入配置且重启仍在；导出 JSON+MD 过 schema。**M1 期间另修 3 个硬伤 → E7/E8/E9** |
| M2 导入解析 | txt/html/json 导入统一进 transcript（**已完成 2026-09-23**） | ✅ **通过**：三种格式各真导入 1 份并正确解析——`probe/m2_unit.py` **71/71**（离线：格式探测、GBK 解码、多行合并、占位符类型、乱序重排、无时间承上、ChatLab 两种路线、失败信封、辅助函数、采集路径回归）；`probe/m2_gui_import.py` **14/14**（GUI 端到端：文件选择 → 若无法判定 me 则弹确认框 → transcript 落进左栏并发出信号；取消/失败分支不污染已有结果）。夹具落在 `probe/fixtures/`（7 份，含真实 GBK 编码文件）→ `probe/m2_import_report.md`。**M2 期间另修 5 个硬伤 → E10~E14** |
| M3 分析闭环 | skill_loader + prompt + LLM + 话术卡（**已完成 2026-09-23，含真机**） | ✅ **通过（含真机，DoD 全清）**：`probe/m3_unit.py` **137/137**（skill 索引与路由校验、prompt 预算截断与引用过滤、schema 六类反例、围栏/尾逗号/截断/信封/编造引用五种解析、LLM 客户端 6 条路径[正常·流式·三档降级·无关400·5xx重试·超时·取消·429·404·每日上限]、engine 编排 7 条路径、**第[7]节真机踩出来的坑 25 项**）；`probe/m3_gui_live.py` **31/31**（真起 worker 线程：预检→分析→五步/话术卡渲染→复制当前版本→导出、流式进度、降级横幅与原文保留、缺 Key 禁用与引导、危机横幅与安全提示、取消退出、与左栏信号联动）；`--selftest` 新增分析层 8 项检查全绿。**真机（真模型）已跑通**：`probe/m3_live.py` 用 `deepseek-chat` × 真实采集记录 × 流式 → **结构化输出、schema 硬错误 0、软提醒 0、`finish_reason=stop`**，2 次调用（首次模型结构写错、修复重试救回）16238 tokens / 12.3s / 约 ¥0.045，成品话术可用且**主动标出「说话人映射未确认」的风险**。报告：`probe/m3_report.md` + `probe/m3_live_report.md`。**M3 真机第一跑就撞出 6 条勘误 → E28~E33**（输出上限 1200 撑不下、`finish_reason` 从未被读、`find_json_object` 捞碎片掩盖结构错位、`loose_repair` 不补括号、`validate` 不管顶层 `scripts`、`save()` 写全量导致默认值演进失效）；此前另修 2 个真 bug + 6 条勘误 → E15~E20 |
| M4 记忆 | 封装官方 `memory_store.py` + UI 同意流（**已完成 2026-09-23**） | ✅ **通过**：`probe/m4_unit.py` **119/119**（把官方脚本当黑盒测：出错信封解析、四个破坏性操作的 `--confirm` 要求、`show --subject-id` 旗标、中文读写往返、8 条类别×来源交叉规则、5 条字段级反例、同意门控四态、`split_cites`/截断/`mark_blocked`、**封顶实测**[event 25→20 自动剪最旧、hypothesis 8→5、user 到 31 条报 `MEMORY_LIMIT_REACHED`、总行 ≤200]、undo 一步一退与空栈 `NOTHING_TO_UNDO`、覆盖写入后 undo 恢复旧值、`forget-object` 连撤销栈清空、revoke/clear/暂停恢复、service 编排三分支）；`probe/m4_gui_live.py` **87/87**（真 UI 闭环：同意窗勾选门禁 → 启用 → 手动记一条[归属强制 user、超长截断、字段必填] → 清单与筛选 → 撤销 → 删对象 → 撤回同意 → 清空全部 → 分析面板召回注入[`已召回 1 条长期记忆` 进 warnings] → 「存进记忆」真写入且只写 event/hypothesis → 复核窗勾选/编辑/改坏拦截/越界灰掉）；`--selftest` 新增第 9 节记忆层 8 项检查全绿。**两份探针都指向临时目录并断言 `%LOCALAPPDATA%\goutoujunshi` 未被改动** —— 自检不拿用户数据做实验。**M4 期间另修 4 个真 bug + 7 条勘误 → E21~E27**（真 bug：档案页刷新无限自触发、退出码 0 但非 JSON 被当成功、`[#3]` 漏进 field 标签、`profile.status` 被删导致 M0 冒烟回归挂掉） |
| M5 打包 | `build.ps1` one-dir、离线跑通（**已完成 2026-09-23**） | ✅ **通过（DoD 全清）**：`scripts/build.ps1` 8 步全绿、退出码 0 —— skill 校验通过 → 源码树 `--selftest` 全绿 → 扫出 skill 的 12 个 stdlib import 转 `--hidden-import`（E40）→ PyInstaller one-dir（期间隐藏 cv2 的 ffmpeg DLL 再还原，E39）→ **体积 299.7 MB < 400 MB**（首次 420.1 MB，砍掉 scipy 68 MB + Qt 多媒体链 ~50 MB + cv2 ffmpeg 29.4 MB）→ 载荷检查（skill 脚本/SKILL.md/`cv2.pyd`/3 个 `.onnx` 全部就位）→ **打包产物冒烟**。**三条硬证据**：① `exe --selftest` 退出码 0、报告「全部通过」，其中**记忆层走的是 frozen 专属的进程内路径**（建档→召回→撤销→撤回删库全通），这是 E34/E35 唯一能被真正执行的地方；② `exe --ocr-check` 退出码 0，真跑 OCR 识别出「微信聊天记录2026 / OCR check 12345」；③ 真启动 GUI —— `hwnd=24054230 visible=True class=Qt6112QWindowIcon size=1180x760 title='狗头军师 · 微信对话分析台 v0.1.0'`，`WM_CLOSE` 后正常退出，`logs/app.log` 记下「日志就绪 / 配置：defaults」且全程无 ERROR。**M5 期间发现 7 条勘误 → E34~E40**，其中 E34/E35/E37/E40 属于「源码跑得好好的、一打包就废」，E36 补上了打包产物的可观测性，E38 补上了打包产物的验收手段 |
| **M5.1 发布包** | 把成果交给别人用：`scripts/make_release.ps1` + `scripts/verify_release.ps1` + `packaging/先读我-README-FIRST.txt`（**已完成 2026-09-23**） | ✅ **通过**：`make_release.ps1` 全绿 —— 载荷检查（skill 脚本 / `SKILL.md` / `cv2.pyd` / 3 个 `.onnx`）→ 暂存复制（`dist/` 保持纯净）→ 拷入面向使用者的《先读我》与三份许可文件 → 顶层文本转 UTF-8 with BOM → **隐私门通过**（无 `config.json`/密钥密文/记忆库/日志/费用文件）→ 产出 **`release/jev-chat-analyzer-v0.1.0-win64-<时间戳>.zip`，125.9 MB（源 299.8 MB，压缩率 42%）**，SHA256 `1D03DEC5…540D`。`verify_release.ps1` 全绿 —— 解压到干净目录后**压缩包根就是应用目录**、解压树重跑隐私门干净、用**私有空数据目录**（等价于别人电脑首次运行）跑 exe：`--selftest` 退出码 **0**（报告「全部通过」，记忆层走 frozen 进程内路径）、`--ocr-check` 退出码 **0**（真识别出「微信聊天记录2026」0.9991 /「OCR check 12345」0.975）。**本轮补 2 条勘误 → E41/E42**（可分发产物 ≠ 构建产物；构建脚本不许用中文文案做判定） |


---

## 15. 术语表
| 术语 | 含义 |
| --- | --- |
| transcript | 归一化后的对话记录（带 `#id`、时间、说话人） |
| 说话人锁定 | 用户确认「我」在气泡左侧还是右侧，固定 `me/obj` 映射 |
| 话术卡 | 分析 + 首选回复 + 三版本 + 时机 + 分支 + 观察窗口 |
| 「算着发」 | 即「**计算后发送**」：先给可复制成品，并标注发送时机与代价后再发 |
| 旁路信号 | Laya 输出的概率值，仅展示、不参与生成 |
| 渐进式加载 | 只按需读取 1–3 份 references，不批量全载 |

---

## 16. 安全与边界（与 skill 一致）
- 不读解密微信库、不自动外发消息。
- OCR/导入素材只信可见原文；说话人未确认前不解释关系。
- 分析不强推：用户目标是退出/梳理时不催化行动；明确拒绝即停。
- 家暴/跟踪/胁迫等先确认安全并给法律与危机转介（加载 `knowledge/17`）。
- **记忆相关边界**：
  - 默认**不开启**。首次启用前必须让用户看完说明并主动勾选；`ask=None` 时**拒绝启用而不是替用户同意**；
    说明文案或策略版本一变就重新征求。
  - **模型推断永远只写进「假设」**，标着「模型推断」与置信度，绝不写成对方的事实或用户的个人资料。
  - 写入前逐条复核可勾选、可编辑；改坏了会被拦下并指明是第几行。
  - 暂停 / 撤销上一条 / 删除某对象 / 撤回同意 / 清空全部都在界面上常驻；
    破坏性操作的确认框写明「不可恢复」与**具体影响多少条**，删对象时并告知撤销栈会一并清空。
  - 记忆是便利功能，**不是安全保障**：涉及人身安全的内容以现实求助为先。

---

## 附录 A：变更清单（逐条对照）
1. §3 新增「复用映射表」，模块改为改造 `jev-chat-src`。
2. §14 前置 **M-1 可行性验证** 并给出阈值。
3. §11 依赖分层，默认不含 torch/laya，给出体积目标。
4. §1.3 + §6 明确 v1 一对一，transcript 预留多 sender。
5. §5.4 记忆只走官方 `memory_store.py`，删除双实现。
6. §4.3 标注 `【已存在】/【待创建】`。
7. §8 新增隐私与数据流章节（本地优先、日志脱敏、告知义务）。
8. §8.4 API key 改凭据管理器/环境变量。
9. §10 config 去除绝对路径，密钥改引用。
10. §7.2 新增严格 JSON schema + 约束解码 + 重试降级。
11. §5.2 新增去重/滚动算法与参数。
12. §5.6 明确线程模型（复用 `app/worker.py`）。
13. §7.3 新增成本/时延预算与取消流式。
14. §12 新增测试策略。
15. §14 里程碑改为可量化 DoD。
16. §15 新增术语表。
17. §13 新增许可与合规（MIT 署名）。
18. §5.7 纳入自动更新（复用 `app/update.py`）。
19. **开工前勘误（§0.1，2026-09-23 实测）**：修正窗口匹配方式（E1）、删截图临时目录（E2）、改 skill 同步方案（E3）、核对并署名 `jev-chat-src` 许可（E4）。

## 附录 B：待创建文件清单
`LICENSE`、`THIRD_PARTY_NOTICES.md`、`pyproject.toml`、`requirements.txt`、`requirements-laya.txt`、`config.example.json`、`app/**`、`scripts/{dev.ps1,build.ps1}`。
