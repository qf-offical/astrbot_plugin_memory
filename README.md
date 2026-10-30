# 三层记忆插件（astrbot_plugin_memory）

给 AstrBot 用的记忆插件：自动吸收对话、按需检索、注入系统提示。

**零第三方依赖**，只用 Python 标准库。

---

## 它做什么

机器人默认「记不住」跨会话的事。这个插件补上这一环：

| 层 | 存什么 | 何时用 |
|---|---|---|
| **短期** | 最近 N 条原始对话（默认 20） | 参与检索，刚说的话下一轮就能查到 |
| **中期** | 超过 1 小时的对话，按会话压成摘要，留 7 天 | 参与检索 |
| **长期** | 事实与偏好，不过期（有上限） | 参与检索，靠 `/记忆` 命令写入 |

三层都只是**检索候选池**；每轮真正注入的只有命中的那几条（默认 3 条、最多 800 字符）。

检索用「关键词重合 + 时间衰减 + 访问频率」打分，前几条注入到系统提示末尾。

### 长期记忆怎么进来

两条路：

| 方式 | 开启 | 说明 |
|---|---|---|
| **手动** | 默认可用 | 发 `/记忆 主人养了只猫叫团子` |
| **自动提炼** | `auto_learn_semantic: true` | 每 10 分钟用模型扫一遍近期对话，自动提取「稳定信息」写进长期层 |

自动提炼只记**稳定事实**——名字、称呼、喜好、厌恶、约定。一次性的闲聊、情绪、临时状态不会被记。

**成本**：每个有对话的会话，每 10 分钟最多一次模型调用（约 300~600 token）。没有新对话时不调用。

**预筛**：`嗯`、`在吗`、`哈哈哈` 这类寒暄在送模型之前就被挡掉，不产生费用。

---

## 安装

把仓库放进 AstrBot 的插件目录，重启即可：

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/qf-offical/astrbot_plugin_memory.git
```

也可以直接下载 zip 解压到 `<AstrBot>/data/plugins/astrbot_plugin_memory/`。

本插件**零第三方依赖**，不用 `pip install` 任何东西，也不需要额外配置 Python 路径。

重启 AstrBot 后，日志里应看到：

```
Plugin astrbot_plugin_memory (0.2.0) by dafeiyu: 三层记忆插件...
[astrbot_plugin_memory] [mem] 已启用，数据目录 .../data/plugin_data/astrbot_plugin_memory
```

---

## 配置

在 AstrBot WebUI → **插件** → 三层记忆插件 → 设置里改，或直接编辑：

```
<AstrBot>/data/config/astrbot_plugin_memory_config.json
```

| 配置项 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `working_size` | 20 | 短期**每会话**保留条数（1~200） |
| `max_sessions` | 200 | 短期最多保留多少个会话（10~5000） |
| `episodic_retention_days` | 7 | 中期保留天数（1~365） |
| `semantic_max_entries` | 1000 | 长期条数上限（10~100000） |
| `recall_top_n` | 3 | 每次注入几条（1~10） |
| `recall_max_chars` | 800 | 注入块最大字符（50~4000） |
| `inject_header` | 【记忆】 | 注入块标题 |
| `auto_learn_semantic` | false | 自动提炼长期记忆（见下方说明） |
| `maintenance_interval_min` | 10 | 后台整理间隔，单位分钟（1~1440） |

---

## 命令

| 命令 | 作用 |
|---|---|
| `/记忆 <内容>` | 把一句话写进当前会话的长期记忆 |
| `/记忆查看` | 看当前会话记住了多少条 |
| `/记忆清空` | 清空当前会话的长期记忆 |

`/记忆` 直接写；也可以打开 `auto_learn_semantic` 让插件自己提炼（见上文）。

---

## 存储位置

```
<AstrBot>/data/plugin_data/astrbot_plugin_memory/
├── working.jsonl     短期
├── episodic.jsonl    中期
└── semantic.jsonl    长期
```

都是 JSONL（一行一条），可以直接 `cat` 查看、手工修。

**清空记忆**：

```bash
rm -rf <AstrBot>/data/plugin_data/astrbot_plugin_memory/*.jsonl
```

插件下次请求会自动重建。

---

## 设计上的几个关键决定

**1. 只改 `system_prompt`，绝不碰 `contexts`**

AstrBot 的 `contexts` 会被 `_save_to_history` 当作真实对话落库。往里注入记忆等于把记忆写进会话历史，下一轮读回来再插一遍，无限累积。

**2. `priority=-1`，排在别的插件之后**

AstrBot 的 priority 数值大者先跑。我们最后执行，这样记忆追加在最末尾；即便整体被截断，最先牺牲的也是记忆，不动别人的内容。

**3. 会话隔离**

记忆按 `unified_msg_origin`（私聊/群聊各自独立）分开存、分开查。**A 群的记忆不会注入到 B 群。**

**4. 任何异常都不阻断回话**

记忆是增强功能。检索超时、文件损坏、写入失败 —— 全部吞掉记 warning，照常让请求发出去。

**5. 单条超长时截断这条，而不是整块丢弃**

早先的写法是「超限就停」，结果 `recall_max_chars` 调小时整个记忆块变空，记忆静默失效。现在改成截断单条内容，至少保留前 N 字。

---

## 已知限制

- **自动提炼有延迟**：`auto_learn_semantic` 开启后，新事实最多等一个维护周期（默认 10 分钟）才进长期层。需要立刻记住就用 `/记忆 <内容>`。
- **不做语义检索**：只用关键词 + 时间衰减。换一种说法问同一件事可能检索不到。
- **中文分词是字符二元组**：比真正的分词器粗糙，长词召回率一般。
- **不做 cron 场景**：AstrBot 的定时主动任务（`llm_generate`）不触发 `on_llm_request`，所以机器人主动发言不会被记录。
- **与平台「群聊上下文注入」（group_icl）内容重叠**：平台开启 ICL 时会把最近群聊记录塞进 `extra_user_content_parts`，本插件把检索到的记忆塞进 `system_prompt`。两者位置不同、不会互相破坏，但**都在给模型讲「之前聊过什么」**，模型可能看到两份相似信息。如果觉得啰嗦，在 AstrBot 设置里关掉 `provider_ltm_settings.group_icl_enable`（默认就是关的），或在插件里调小 `recall_top_n`。

---

## 自测

```bash
cd <插件目录>

# 单元测试（需要 pytest）
python -m pytest tests/ -q

# 端到端检查（不需要 AstrBot 运行）
python e2e_check.py
```

`e2e_check.py` 会验证：吸收 → 巩固 → 检索 → 会话隔离 → 渲染 → 异常防护。

---

## 故障排查

| 现象 | 检查 |
|---|---|
| 插件没加载 | 日志搜 `astrbot_plugin_memory`，看有没有 traceback |
| 记忆不生效 | 日志搜 `[mem] 注入`，看有没有命中 |
| 记忆串群了 | 检查 `semantic.jsonl` 里的 `chat_id` 字段 |
| 存储涨太快 | 调小 `episodic_retention_days` 和 `semantic_max_entries` |
| 注入太多挤掉对话 | 调小 `recall_top_n` 和 `recall_max_chars` |

看日志：

```bash
grep '\[mem\]' <AstrBot>/logs/*.log
```
