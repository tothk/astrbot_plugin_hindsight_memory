# Hindsight HTTP API 中文文档（v1.2.0）

> 本文是 <https://hindsight.vectorize.io/api-reference> 的中文整理版，面向本项目（AstrBot「Hindsight 记忆储存」插件）的对接需要。
>
> - 上游版本：Hindsight HTTP API **1.2.0**（OpenAPI 3.1.0，Apache-2.0）
> - 主要来源：`https://hindsight.vectorize.io/openapi.json`、`/llms.txt`、`/llms-full.txt`、`/developer/api/retain`、`/developer/retrieval`
> - 抓取日期：2026-11-10
> - 术语对照：bank = 记忆库；retain = 写入/摄取；recall = 召回；reflect = 反思作答；observation = 归纳观察；mental model = 心智模型；knowledge page = 知识页

---

## 1. 基础信息

| 项目 | 值 |
|---|---|
| 默认 Base URL | `http://localhost:8888` |
| 控制台 Web UI（Docker 镜像） | `http://localhost:9999` |
| 路径前缀 | `/v1/default/banks/{bank_id}` |
| 请求/响应格式 | `application/json` |
| 鉴权 | `Authorization` 请求头，**可选**（每个端点 `required: false`） |
| 健康检查 | `GET /health`（就绪）、`GET /health/live`（存活） |

`{bank_id}` 是记忆库标识，由调用方自定，例如 `user-123`。一个 bank = 一个隔离的记忆空间。路径里的 `default` 是租户/命名空间占位，自建服务一般保持 `default`。

### 鉴权说明（本项目重点）

- Token **不是必须的**。本地自建、未开鉴权的服务，客户端应**完全不发送** `Authorization` 头——不要发送空值（`Authorization: Bearer `），部分服务端会把「有头但值为空」判为非法而返回 401/400。
- 服务端开启鉴权时，形式为 `Authorization: Bearer <token>`。

```python
headers = {"Content-Type": "application/json"}
if token:
    headers["Authorization"] = f"Bearer {token}"
```

---

## 2. 核心概念

Hindsight **不保存原始对话**。它把输入内容交给 LLM 抽取成结构化事实，并连成知识图谱。

| 类型 | 含义 | 例子 |
|---|---|---|
| **world fact**（世界事实） | 关于外部世界的客观陈述 | “Alice 在 Google 工作。” |
| **experience fact**（经历事实） | 记忆库自身做过的事 | “我向 Bob 推荐了 Python。” |
| **observation**（观察） | 由多条事实归纳出的信念，带证据与历史 | “用户原本偏爱 React，现在转向了 Vue。” |
| **mental model**（心智模型） | 针对常见问题手工整理的摘要 | — |
| **knowledge page**（知识页） | 记忆库为自己撰写的活文档，成树状组织 | — |

事实不是平铺的列表：每条事实都链接到它提到的实体，以及共享这些实体的其他事实。这正是「Alice 在哪工作」能由两条从未一起存储的事实回答的原因。

### 三个核心操作

| 操作 | 作用 | 端点 | 用 LLM |
|---|---|---|---|
| **Retain** | 写入内容，抽取事实 | `POST /{bank}/memories` | 是（抽取） |
| **Recall** | 多路检索，返回记忆列表 | `POST /{bank}/memories/recall` | 否 |
| **Reflect** | 结合 bank 人格设定推理作答 | `POST /{bank}/reflect` | 是（生成） |

---

## 3. 端点总表

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 就绪检查（数据库可达）。数据库不可达返回 503 |
| GET | `/health/ready` | 同上，`/health` 的别名 |
| GET | `/health/live` | 存活检查，不访问数据库 |
| GET | `/version` | 版本与特性开关 |
| GET | `/metrics` | Prometheus 指标 |
| **POST** | **`/v1/default/banks/{bank_id}/memories`** | **写入（retain）** |
| **POST** | **`/v1/default/banks/{bank_id}/memories/recall`** | **召回** |
| **POST** | **`/v1/default/banks/{bank_id}/reflect`** | **反思作答** |
| GET | `/v1/default/banks` | 列出记忆库（支持 `q` 子串搜索、分页） |
| **PUT** | **`/v1/default/banks/{bank_id}`** | **创建或更新记忆库**（空 body = 全取默认值，幂等） |
| DELETE | `/v1/default/banks/{bank_id}` | 删除记忆库及其全部内容 |
| GET/PATCH | `/v1/default/banks/{bank_id}/config` | 读取 / 局部更新该库的配置（body 需 `{"updates": {...}}`） |
| GET | `/v1/default/banks/{bank_id}/graph` | 记忆图谱数据 |
| GET | `/v1/default/banks/{bank_id}/memories/list` | 分页列出记忆单元（可按类型、文档、实体、时间窗过滤） |
| GET | `/v1/default/banks/{bank_id}/memories/{memory_id}` | 取单条记忆 |
| PATCH | `/v1/default/banks/{bank_id}/memories/{memory_id}` | 人工修订记忆 |
| GET | `/v1/default/banks/{bank_id}/memories/{memory_id}/history` | 观察类记忆的变更历史 |
| POST | `/v1/default/banks/{bank_id}/memories/dry-run-extract` | 预演抽取，不落库 |
| POST | `/v1/default/banks/{bank_id}/prompts/preview` | 预览实际会发给 LLM 的 prompt，不调模型 |
| GET | `/v1/default/banks/{bank_id}/stats` | 统计 |
| GET | `/v1/default/banks/{bank_id}/health/llm` | 探测该 bank 的 LLM 连通性 |
| GET | `/v1/default/banks/{bank_id}/stats/memories-timeseries` | 摄取时间序列 |
| GET | `/v1/default/banks/{bank_id}/entities` | 实体列表 |
| GET | `/v1/default/banks/{bank_id}/entities/graph` | 实体共现图 |
| GET | `/v1/default/banks/{bank_id}/entities/{entity_id}` | 实体详情 |
| POST | `/v1/default/banks/{bank_id}/entities/{entity_id}/regenerate` | 重新生成实体观察（已废弃） |
| GET/POST | `/v1/default/banks/{bank_id}/mental-models` | 列出 / 创建心智模型 |
| GET/PATCH/DELETE | `/v1/default/banks/{bank_id}/mental-models/{id}` | 心智模型增删改查 |
| GET | `/v1/default/banks/{bank_id}/mental-models/{id}/history` | 历史 |
| POST | `/v1/default/banks/{bank_id}/mental-models/{id}/refresh` | 刷新（异步任务） |
| POST | `/v1/default/banks/{bank_id}/mental-models/{id}/dry-run-refresh` | 预演刷新 |
| POST | `/v1/default/banks/{bank_id}/mental-models/{id}/clear` | 清空内容 |
| GET/POST | `/v1/default/banks/{bank_id}/knowledge-base/tree`、`/folders`、`/pages` | 知识库树、建文件夹、建页面 |
| GET | `/v1/default/banks/{bank_id}/knowledge-base/export` | 导出为 markdown 包 |
| GET | `/v1/default/banks/{bank_id}/knowledge-base/search` | 知识页混合检索（BM25 + 向量） |
| GET/PATCH/DELETE | `/v1/default/banks/{bank_id}/knowledge-base/pages/{page_id}`、`/nodes/{node_id}` | 页面/节点读写 |
| GET | `/v1/default/banks/{bank_id}/directives` | 列出指令 |

> 所有端点都接受可选的 `authorization` 头；`422` 表示请求体校验失败。

---

## 4. Retain：写入

```http
POST /v1/default/banks/{bank_id}/memories
Content-Type: application/json

{
  "items": [
    {
      "content": "……"
    }
  ]
}
```

请求体外层是 `RetainRequest`，只有一个字段 `items`（`MemoryItem` 数组）。

### 4.1 MemoryItem 字段全表

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `content` | string | **是** | 原始文本。**唯一必填项**。Hindsight 会切块、逐块交给 LLM 抽取事实，**原文本身不保存**。一段 `content` 可能产生很多条记忆 |
| `context` | string | 否 | 短标签，描述来源或场景，如 `"team meeting"`、`"slack"`、`"support ticket"`。**会直接注入 LLM 抽取 prompt**，因此显著影响抽取质量。同样的句子在 `"performance review"` 与 `"product roadmap"` 语境下会产出不同记忆 |
| `timestamp` | string \| null | 否 | 事件**实际发生**的时间。三种取值见下表 |
| `document_id` | string | 否 | **幂等键**：同一 ID 会 upsert（先删旧文档及其全部记忆，再重新处理）。省略则随机 UUID，重复摄取会产生重复记忆 |
| `update_mode` | string | 否 | `"replace"`（默认）或 `"append"`。见 4.3 |
| `metadata` | object | 否 | 任意字符串键值对，如 `{"source":"slack","channel":"engineering"}`。会进抽取 prompt，也会挂在每条记忆上随 recall 返回，便于客户端过滤。**值必须是字符串**；值为 `null` 的键会被丢弃（需要保留就传空串） |
| `entities` | array | 否 | 强制识别的实体列表，元素形如 `{"text": "Alice", "type": "PERSON"}`。`type` 可为 `PERSON` / `ORG` / `CONCEPT`（默认 `CONCEPT`） |
| `resolve_entities` | bool | 否 | 默认 `true`。`true` 时你传入的实体名会与库中已有实体做归并（`Dr. Waller` 与 `Dr Waller` 合一）；`false` 时只做大小写不敏感精确匹配，你给的名字之间**永不互相合并**。只作用于**你显式传入的**实体，LLM 自动抽取的实体始终参与归并 |
| `tags` | array[string] | 否 | **可见性作用域**。召回时只有标签与过滤条件相交的记忆才会返回。常见约定：`user:<id>`、`session:<id>`、`room:<id>`、`topic:<name>` |
| `document_tags` | array[string] | 否 | 文档级标签（作用同上，粒度是文档） |
| `observation_scopes` | string \| array | 否 | 决定这条记忆参与哪些观察的归纳。见 4.4 |

#### `timestamp` 的三种取值

| 值 | 行为 |
|---|---|
| 省略 / `null` | 取**摄取时刻** |
| ISO 8601 字符串，如 `"2024-01-15T10:30:00Z"` | 使用给定时刻 |
| `"unset"` | **完全不存时间**。适用于参考文档、书籍、虚构作品等没有真实事件时间的材料。此时 prompt 里显示 `Event Date: Unknown`，抽取出的 `when` 字段为 `N/A` |

时间戳会注入抽取 prompt，模型据此解析文中的相对时间（「上周一」→ 具体日期）。提供真实时间戳，才能让「去年春天发生了什么」这类时间检索正常工作。

### 4.2 `document_id` 的分组规则

| 请求形态 | 结果 |
|---|---|
| 所有 item 都**没有** `document_id` | 全部进入**同一个**新文档。若一次请求超出单批上限（`HINDSIGHT_API_RETAIN_BATCH_TOKENS`）会被切分，每份各成一个文档 |
| 部分有、部分没有 | 没有 `document_id` 的 item **各自**成立独立文档，永不被折叠进别人的文档（因此替换/删除那个文档不会牵连它） |
| 多个 item 共享同一个 `document_id` | 按请求顺序进入同一个文档 |

**建议**：想让每条独立，就给每条不同的 `document_id`；想让多条进同一文档，就给相同的 `document_id`。

### 4.3 `update_mode`：replace 与 append

| 值 | 行为 |
|---|---|
| `"replace"`（默认） | 删除旧文档及其全部记忆，从头重新处理新内容。这就是标准 upsert |
| `"append"` | 把新内容**拼接到旧文档正文之后**，再重新处理整篇。增量摄取会自动跳过未变化的 chunk，只有新部分触发 LLM 抽取 |

- **`append` 必须带 `document_id`**，否则没有已存在的文档可供追加。
- **适用场景**：内容增量增长——日志、日记、逐条到达的聊天记录。不必每次重发全史，只发新增部分即可。

```json
{
  "items": [
    {
      "content": "要追加到已有文档的新内容。",
      "document_id": "my-growing-doc",
      "update_mode": "append"
    }
  ]
}
```

### 4.4 `observation_scopes`：观察的作用域

归纳（consolidation）时，每条记忆可以参与多个作用域的归纳，每个作用域独立跑一遍，产出只带该作用域标签的观察。作用域匹配用 `all_strict`（标签必须完全相等），因此各作用域相互隔离。

以带标签 `["student:alice","teacher:bob","session-id:s1"]` 的课堂记录为例：

| 取值 | 行为 | 适用 |
|---|---|---|
| `combined`（默认） | 用全部标签一起归纳一遍，产出一条带完整标签集的观察 | 这条记忆只有作为整体才有意义，不需要按单个标签查询 |
| `shared` | 忽略自身标签，归纳到**一个无标签**的全局作用域（等价于 `[[]]`，注意**不是** `[]`——空数组表示零个作用域，会静默回退到 `combined`） | 标签只是「一次调用的来源标记」（如 session id），不想让它成为归纳边界 |
| `per_tag` | 每个标签各归纳一遍，各自独立生长 | 多主体内容（对话、课堂、客服会话）的常见选择 |
| `all_combinations` | 所有子集各一遍（3 个标签 = 7 遍） | 需要每个粒度都有观察 |
| 自定义列表，如 `[["student:alice"],["teacher:bob"]]` | 只跑列出的这些作用域 | 明确知道哪些组合有意义 |

自定义作用域支持通配：标签里含 `*` 即按 glob 匹配记忆自身的标签（`topic:*` 匹配任意 `topic:xxx`）。某个作用域若一个标签都没匹配上会被跳过；若全部被跳过则回退到 `combined`，永远不会变成无标签。

> ⚠️ 被作用域排除的标签**不会出现在观察上**。`[["topic:*"]]` 会把 Alice 和 Bob 的账单事实合并进同一条观察，之后按 `user:alice` 过滤就查不到它了。

### 4.5 对话该怎么格式化

**一段完整对话应作为一个 item 提交**，逐行写成：

```
Name (timestamp): text
```

例：

```python
conversation = "\n".join([
    "Alice (2024-03-15T09:00:00Z): Hi Bob! Did you end up going to the doctor last week?",
    "Bob (2024-03-15T09:01:00Z): Yes, finally. Turns out I have a mild peanut allergy.",
    "Alice (2024-03-15T09:02:00Z): Oh no! Are you okay?",
    "Bob (2024-03-15T09:03:00Z): Yeah, nothing serious. Just need to carry an antihistamine.",
    "Alice (2024-03-15T09:04:00Z): Good to know. We'll avoid peanuts at the team lunch.",
])

client.retain(
    bank_id="my-bank",
    content=conversation,
    context="team chat",
    timestamp="2024-03-15T09:04:00Z",
    document_id="chat-2024-03-15-alice-bob",
)
```

LLM 能解析任意格式（纯文本、JSON、Markdown），前提是**清楚表达谁在何时说了什么**。上述格式能让模型正确归属说话人，并解析文中跨消息的相对时间引用。

### 4.6 响应

| 字段 | 说明 |
|---|---|
| `success` | 是否无错完成 |
| `bank_id` | 接收内容的记忆库 |
| `items_count` | 处理的 item 数 |
| `async` | 是否异步处理 |
| `usage` | `{input_tokens, output_tokens, total_tokens}`，**仅同步执行时返回** |

> 注意：Hindsight 的写入通常是**异步**的（LLM 抽取需要时间）。HTTP 200 只表示请求被接受，不代表事实已经抽取完成。需要确认落地请查 `/stats`、`/memories/list` 或操作状态端点。

---

## 5. Recall：召回

```http
POST /v1/default/banks/{bank_id}/memories/recall
Content-Type: application/json

{ "query": "Alice 在 Google 做什么？" }
```

### 5.1 RecallRequest 主要参数

| 参数 | 说明 |
|---|---|
| `query` | 自然语言查询，必填 |
| `types` | 只返回指定类型：`world` / `experience` / `observation`。省略则全返回 |
| `budget` | 搜索深度：`low` / `mid` / `high`，默认 `mid`。影响候选池大小与图遍历深度 |
| `max_tokens` | 返回内容的 token 预算，默认 `4096`。只计记忆正文，元数据不计；放不下的整条会被跳过并继续装下一条；若一条都放不下，仍会完整返回排名第一的那条（不会返回空列表） |
| `include_chunks` | 是否附带生成该记忆的**原始文本块**（chunk）。当摘要丢失了细节时有用 |
| `max_chunk_tokens` | chunk 的 token 预算 |
| `include_entities` / `max_entity_tokens` | 是否附带实体信息及其预算 |
| `tags` / `tags_match` | 按可见性标签过滤；`tags_match` 可选 `any` / `all` / `exact` |
| `query_timestamp` | 查询所代表的时间点，用于新鲜度加权；省略则用服务器当前时间 |
| `min_scores` | 最低分数过滤：`min_scores.reranker` 或 `min_scores.final`（使用决策模型排序时只接受 `final`，传 `reranker` 会 400） |
| `trace` | 调试用，返回各阶段（如 `rerank_prefilter`）的诊断信息 |

### 5.2 四路并行检索

| 策略 | 擅长 | 说明 |
|---|---|---|
| **Semantic**（语义） | “Alice 的工作” → “Alice 是软件工程师” | 向量相似度 |
| **Keyword**（BM25） | 专有名词、技术术语、URL 等精确匹配 | 可插拔 5 种后端：`native`（PG tsvector，实为 TF-IDF）、`vchord`、`pg_textsearch`、`pgroonga`、`pg_search`（ParadeDB，唯一支持 Citus 水平扩展的真 BM25；可配 `jieba`/`chinese_compatible`/`ngram` 分词） |
| **Graph**（图） | 多跳间接关系：Alice → Google → 产品 | 沿实体链接扩展 |
| **Temporal**（时间） | “2023 年发生了什么” | 解析时间表达式成窗口，窗口内按**相关度**选（不是按新旧），并在窗口范围内**分桶铺开**，避免全挤在 12 月 |

### 5.3 排序流水线

1. **RRF 融合**：`score(d) = Σ 1/(60 + rank_i(d))`。只看**名次**不看原始分数，因此不同策略的分数量纲差异不再影响结果；多路都靠前的记忆胜出。四路权重默认相等。
2. **Cross-encoder 重排**：把 query 与候选成对送入模型打相关度分。重排前按 RRF 名次截断到 **300** 条（`HINDSIGHT_API_RERANKER_MAX_CANDIDATES`）。分数已是 [0,1] 的外部重排器（Cohere、Jina 等）原样保留，否则用 sigmoid 归一化。
3. **乘性加成**：`final = CE × recency × temporal × proof_count`，每项中心为 1.0，`boost = 1 + α × (signal − 0.5)`：
   - 新鲜度 α=0.2（±10%），365 天线性衰减，`clamp(1 − days/365, 0.1, 1.0)`，无日期的记忆取 0.5
   - 时间接近度 α=0.2（±10%），仅当查询含时间引用时生效
   - 证据数 α=0.1（±5%），`clamp(0.5 + ln(n)/10, 0, 1)`
   - 三项同时极端时约 +27% / −23%，刻意保守，不让辅助信号压过相关度
4. **Token 截断**：按 `final` 从高到低装入，直到 `max_tokens` 用尽。

`budget` 对应的候选预算：`low`=100、`mid`=300（默认）、`high`=1000（固定模式下，可由 `HINDSIGHT_API_RECALL_BUDGET_FIXED_*` 覆盖）。

### 5.4 budget 与 max_tokens 是正交的两件事

| 参数 | 控制什么 | 影响 |
|---|---|---|
| `budget` | 检索得多深（候选池、图跳数） | 搜索耗时 |
| `max_tokens` | 最终返回多少内容 | 下游 LLM 处理耗时 |

推荐组合：

| 场景 | budget | max_tokens |
|---|---|---|
| 聊天机器人回复 | low | 2048 |
| 文档问答 | mid | 4096 |
| 研究型查询 | high | 8192 |

---

## 6. Reflect：反思作答

```http
POST /v1/default/banks/{bank_id}/reflect
Content-Type: application/json

{ "query": "我们应该在后端采用 TypeScript 吗？" }
```

`recall` 返回**记忆**，`reflect` 返回**答案**。它跑的是一个 agentic 循环：

1. 先检索 experience（对话与事件）
2. 再检索与问题相关的 world facts
3. 再取 observations 与 mental models（bank 提炼出的观点）
4. 按优先级校验来源：心智模型 / 知识页 → 观察 → 原始事实
5. 由 LLM 组织成答案

主要参数：`query`、`budget`、`include_facts`（是否返回依据）、`response_schema`（要求结构化输出）、`tags` / `tags_match`。

响应：

| 字段 | 说明 |
|---|---|
| `text` | 自然语言答案 |
| `based_on.memories` | 实际引用的记忆，只能引用真正检索到的 ID |
| `structured_output` | 传了 `response_schema` 时才有，是对 `text` 的结构化投影 |
| `structured_output_error` | 结构化抽取失败的原因。**有 `text` 无 `structured_output` 且无此字段**，说明答案里本就没有符合 schema 的内容 |

schema 规则：必须是对象且至少一个属性；属性类型限 `string`/`number`/`integer`/`boolean`/`array`/`object`；`required` 列必填项；非法 schema 会在调用前被拒绝。

### 让两个 bank 对同一问题给出不同答案

| 设定 | 作用 |
|---|---|
| **Mission**（使命） | 用自然语言描述 bank 的身份与偏好，决定它优先关注什么 |
| **Directives**（指令） | 硬性规则，永不违反 |
| **Disposition**（性格） | 怀疑、字面、共情三个维度，各 1–5 分，影响解读方式 |

这三者**只影响 reflect**，`recall` 谁问都一样。

---

## 7. 常见错误与处理

| 状态码 | 含义 | 处理建议 |
|---|---|---|
| `401` / `403` | 鉴权失败 | 分两路文案：token 为空 → 提示去配置；token 非空 → 提示无效或过期 |
| `404` | bank 不存在（多数端点） | 先创建 bank，或检查 `bank_id`。**服务端不会因为 retain/recall 自动建库**，详见 8.5 |
| `422` | 请求体校验失败 | 打印响应体里的 `detail`，通常是字段类型或必填项问题 |
| `503` | 数据库不可达 | 服务端问题，重试 + 退避 |

---

## 8. 本项目（AstrBot 插件）如何对接

结合已定的写入策略（每 5000 字符一批、每日 00:05 补投、同 `document_id` 追加、接受重复），落地要点如下。

### 8.1 关键结论：不要自己拼 JSON Lines

原计划是用「三段一组：sent / content / time」序列化。但 Hindsight 的官方推荐格式是**把一段对话作为一个 item 的 `content`**，逐行 `Name (timestamp): text`。

两者其实描述的是同一件事，区别在于**谁做序列化**：

- 自己拼 JSON Lines → LLM 看到的是一坨 JSON，虽然能解析，但说话人与时间的归属信号变弱；
- 按官方格式逐行渲染 → 抽取质量最高，且这正是 `Alice (2024-03-15T09:00:00Z): 文本` 的结构。

**建议**：数据库里一行记录 → 渲染成一行 `"{sender} ({message_sent_time}): {raw_message}"`，多行用 `\n` 连接，整体作为一个 item 的 `content`。`sent` / `time` 的信息不丢，只是换成了模型最擅长的表达。

### 8.2 请求骨架

```python
items = [{
    "content": "\n".join(lines),          # 逐行 "{sender} ({time}): {text}"
    "context": "陪伴型 AI 私聊对话",        # 固定短语，显著影响抽取质量
    "document_id": document_id,           # 与本地 rawmessagesqlite.document_id 对齐
    "update_mode": "append",              # 同一 document_id 后续提交即追加
    "timestamp": latest_time,             # 本批最后一条的发生时间
    "metadata": {"umo": umo, "role_scope": "private"},
}]
```

`update_mode="append"` 有硬性前提：**必须带 `document_id`**，且该 bank 内该文档此前存在过。第一次提交时可以也用 `append`（没有旧文档时行为等同新建）。

### 8.3 分批规则（与本地实现对齐）

- 按字符数累计到 `sync_batch_chars`（默认 5000）就切一批；若某一条自身就超过上限且当前批为空，**单独成批提交**，否则会永久阻塞。
- 分批允许跨 `document_id`（追加语义下无妨），但每批内仍要保证 `document_id` → 内容行的映射正确：一个 `items` 数组里可以放多个 item，每个 item 各自带自己的 `document_id`。**不要**把两个 `document_id` 的内容混进同一个 item 的 `content`。
- 提交成功后（HTTP 200 且 `success: true`）再标记本地 `flushed = 1`。接受「成功但未标记就崩溃」导致的重复——Hindsight 侧是追加，重复内容会产生冗余事实但不致命。

### 8.4 每日 00:05 补投

定时任务扫描 `flushed = 0` 的记录，如果为空就什么都不做（不要发空 `items` 数组，那会返回 422）。补投失败要记日志，且**不要**把 `flushed` 置位。

### 8.5 记忆库不存在时要主动创建

服务端对「不存在的 bank」有两条不一致的行为，实测（1.2.0）：

| 操作 | 对不存在的 bank |
|---|---|
| `POST .../memories`（retain） | **200**，并顺带把 bank 建出来 |
| `POST .../memories/recall` | **404** `Bank 'xxx' not found` |

后果：一个刚装好的插件，第一次对话必然走在 retain 之前，于是召回永远 404，表现为「有记录、但 AI 一直记不住你」。日志里只有一行 `记忆召回失败：请求被拒绝（HTTP 404）：Bank 'default' not found`。

正确做法是在召回侧兜底：

```python
try:
    return await client.recall(bank_id, query, max_tokens=1024)
except HindsightBankNotFoundError:          # 404 单独成一个异常类型
    await client.ensure_bank(bank_id)        # PUT /v1/default/banks/{bank_id}，body 传 {}
    return await client.recall(bank_id, query, max_tokens=1024)   # 只重试一次
```

三个容易写错的点：

1. **建库用 `PUT /v1/default/banks/{bank_id}`，body 传 `{}`**。传字段会覆盖用户在该库上做过的配置（`retain_mission`、`retain_chunk_size` 等）。空 body 表示「缺什么补默认值」，且该接口本身就幂等（重复 PUT 返回 200）。
2. **不能靠「列出所有 bank 再找」来判断存在性并不可靠** —— `GET /v1/default/banks` 没有按 id 取单条的路由（`/v1/default/banks/{bank_id}` 只开放 DELETE/PATCH/PUT），只能用 `?q=` 过滤。而 `q` 是**子串**匹配：查 `dev` 时 `dev-test` 也会命中，必须对返回的 `bank_id` 再做一次精确比较。
3. **只重试一次**。建完库还 404，说明问题不在「库缺失」（可能是 PUT 被权限拒绝、bank_id 非法），再重试就是死循环。

### 8.6 本项目的字段映射

| 本地 `rawmessagesqlite` | Hindsight |
|---|---|
| `raw_message` | `content` 里每行的正文部分 |
| `sender_name`（为空时退回 `sender_id`） | 每行的说话人前缀（`Name` 位置）。**必须用昵称**：传 `10001` 这类数字串时，Hindsight 抽出的实体名会退化成 `user`，人格化事实（「她喜欢看海」）就建立不起来 |
| `message_sent_time` | 每行括号内的 `timestamp`，同时作为 item 的 `timestamp`（取文档内第一条） |
| `role` | 用于区分说话人显示名（`user` → 用户昵称，`llm` → bot 人格名） || `document_id` | 原样作为 `document_id`（本项目格式 `日期:umo`） |
| `record_date` | 不传（已隐含在 `document_id` 里），或塞进 `metadata` |

---

## 9. 参考链接

- API 参考：<https://hindsight.vectorize.io/api-reference>
- OpenAPI 规范：<https://hindsight.vectorize.io/openapi.json>
- 精简索引：<https://hindsight.vectorize.io/llms.txt>
- 全文文档：<https://hindsight.vectorize.io/llms-full.txt>
- Retain：<https://hindsight.vectorize.io/developer/api/retain>
- Recall：<https://hindsight.vectorize.io/developer/api/recall>
- Reflect：<https://hindsight.vectorize.io/developer/api/reflect>
- 检索原理：<https://hindsight.vectorize.io/developer/retrieval>
- 快速开始：<https://hindsight.vectorize.io/developer/api/quickstart>
