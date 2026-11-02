# astrbot_plugin_mem

给 AstrBot 用的**全局记忆**插件：任何私聊、任何群聊、任何号，读写同一份事实库，不按会话隔离。

> 状态：**v0.2.0（写入降噪 + 检索净化）** 在分支 `feat/v0.2-noise`，等点点 review + friend-box 验收。
> v0.1.0 的库**直接兼容**，不用迁移；升级后建议跑一次 `/mem dedupe --dry-run` 看历史重复。

## 为什么需要它

AstrBot 自带的长期记忆有两个问题：注入的是二次加工摘要（会过期），而且按会话过滤（换个会话就失忆）。这个插件换一种存法——存**事实四元组**，同槽新值自动作废旧值：

```
(scope, subject, predicate) → value   + valid_from / valid_to / superseded_by
```

所以「key 存在 A 路径」改成「存在 B 路径」时，旧记录会被打上 `valid_to`，检索只出新的。不会新旧混着一起读出来。

## 能力

- 指令：`/mem add | search | list | forget | stats | dedupe | export | import`
- LLM 工具：`memory_add` / `memory_search`
- **写入降噪**（v0.2.0）：一句判断句、≤120 字、拒叙事体、同槽 / 同义覆盖优先于追加
- **默认不注入**（`auto_inject_n = 0`）：不往 prompt 里塞记忆，避免污染人格、也护住 prompt 缓存
- 向量召回 + 重排：**复用 AstrBot 里已配置的 embedding / rerank provider**，插件自己不碰 key
- 三级降级：向量挂 → 关键词；重排挂 → 向量序；全挂 → 纯关键词
- 安全：拦截 `sk-` / `gho_` / `ibssh_` / `AKIA` / `-----BEGIN` 等敏感串，单条 120 字上限（可配）
- 权限：写操作默认仅管理员与白名单，读放开
- 纯标准库，无第三方依赖

## 写入规范（v0.2.0）

记忆库被写坏的方式只有两种：**记成日记**、**同一个事实记三遍**。v0.2.0 两条都堵了。

规范本身来自 `dd-home/memory-policy.md`（记忆写入规范：什么时候写、怎么写、写在哪），
插件是它的执行层。规则落成代码就是四条：

| 规则 | 落地 | 配置 |
| --- | --- | --- |
| **一句判断句**：`主语 + 谓词 + 值` | `value` ≤ 120 字，超长拒写并提示改成一句事实 | `max_value_len`（默认 120，0 = 不限制） |
| **禁叙事体** | `value` 以「我 / 他 / 她 / 今天 / 昨天 / 昨晚 / 刚才 / 后来 / 然后」开头 → 拒写 | `reject_narrative`（默认 `true`） |
| **同槽覆盖优先于追加** | 同 `(scope, subject, predicate)` → 旧值填 `valid_to`，不新增 | 无（v0.1.0 已有） |
| **同义也算同一件事** | 同 scope + 同 subject，新值向量与旧记录 `cos ≥ dedupe_threshold` → 也走覆盖 | `dedupe_threshold`（默认 0.95，0 = 关闭） |

反例 → 正例：

```
× /mem add 点点 聊天 我今天跟他把记忆分两层讲了，他说会话隔离难受
√ /mem add 框架 长期记忆 有「会话隔离」开关，关掉即全局共享
```

前一条会被拒（叙事体 + 超长），提示改成判断句。后一条长度合法、以名词开头，正常入库。

写入前的归一化（指令和 LLM 工具同一套）：

- `strip()` 首尾空白；全角空格 `　` / 不换行空格 → 半角；连续空白压成一个
- 中文标点**不动**（「，。」是用户写法，改了反而检索不到）
- 主语 / 谓词各限 40 字，同样归一化

> 为什么 `reject_narrative` 默认开：叙事句没有稳定的主语/谓词结构，向量检索抓不住重点，
> 而且它记的是「过程」不是「事实」——过两天就过期，却一直在结果里占位。

## 安装

**WebUI（推荐）**：插件管理 → 安装插件 → 填仓库地址

```
https://github.com/dian-dian233/astrbot_plugin_mem
```

**手动**：

```bash
cd AstrBot/data/plugins
git clone https://github.com/dian-dian233/astrbot_plugin_mem.git
```

然后在 WebUI 里重载插件（或重启 AstrBot）。

## 配置

配置项在 WebUI 插件配置页。**没有必填项**——装上就能用关键词检索；配置里有 embedding provider 就自动升级成向量召回。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `db_path` | 空 | 空 = AstrBot `data/mem.db`（推荐）。填了就用填的；指到插件代码目录会被拒绝（升级会丢数据） |
| `scope` | `global` | 写入归属。默认全局一份，所有会话共享 |
| `embed_provider_id` | 空 | 空 = 自动挑第一个已配置的 embedding provider |
| `rerank_provider_id` | 空 | 空 = 自动挑第一个已配置的 rerank provider |
| `recall_k` | `30` | 召回候选上限（送去重排的条数） |
| `rerank_k` | `5` | 最终返回条数 |
| **`max_value_len`** | `120` | **单条内容字数上限**。超长拒写；`0` = 不限制（不建议） |
| **`reject_narrative`** | `true` | **拒叙事体**：以「我/他/她/今天/昨天/昨晚/刚才/后来/然后」开头的 value 直接拒写 |
| **`dedupe_threshold`** | `0.95` | **写入相似去重**：同 scope + 同主语，新值与旧记录余弦 ≥ 此值 → 覆盖不新增；`0` = 关闭 |
| **`min_score`** | `0.2` | **检索噪声地板**：关键词没中且向量余弦低于此值的候选丢掉 |
| `auto_inject_n` | `0` | 0 = 不注入；N>0 = 每轮注入 top-N 条（按主语去重，每条截 200 字） |
| **`inject_char_cap`** | `600` | **注入内容总字数上限**，超了按 `updated_at` 倒序砍（先丢最旧）；`0` = 不限制 |
| `allow_all` | `false` | 打开后**所有人**都能写（不建议） |
| `allowed_users` | `[]` | 允许写记忆的用户 ID（QQ 号），一行一个 |
| `allowed_groups` | `[]` | 允许写记忆的群号，一行一个 |

`embed_provider_id` / `rerank_provider_id` 填的是 WebUI「服务提供商」里显示的 **ID**
（不是适配器类型名）。留空就自动挑。

> `dedupe_threshold` 和 `min_score` **只在配了 embedding provider 时才有意义**：
> 没配 embedding → 写入去重跳过（不阻塞写入）、检索退关键词（关键词命中不受 `min_score` 影响）。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/mem add <主语> <谓词> <内容>` | 写一条事实；同 `(scope, 主语, 谓词)` 或**同主语同义**自动覆盖旧值（旧值作废但保留历史） |
| `/mem add ... --scope group:123` | 写到指定池（也可 `user:10001`） |
| `/mem search <关键词> [数量]` | 检索：关键词 + 向量召回 → 重排 → 同 `(主语,谓词)` 只留最新 |
| `/mem list [数量]` | 最近的有效记录（默认 10） |
| `/mem forget <关键词>` | 作废命中的有效记录（填 `valid_to`，不物理删） |
| `/mem stats` | 有效/历史条数、分池、provider 状态、最近更新、库路径 |
| **`/mem dedupe [--dry-run]`** | **扫全库同槽重复**：`(scope, 主语, 谓词)` 每组只留 `updated_at` 最新，其余填 `valid_to`；`--dry-run` 只报告不改库 |
| `/mem export [路径]` | 导出 JSON（默认 `~/mem-export.json`） |
| `/mem import <路径>` | 导入并合并：同槽按 `updated_at` 新者胜 |

例子：

```
/mem add 点点 喜欢 看星星
/mem search 星星 3
/mem add 用户的服务器 部署在 阿里云 --scope user:10001
/mem dedupe --dry-run
```

`dedupe` 返回长这样：

```
扫描 42 条，可合并 3 组（涉及 5 条重复）
global|服务器|系统 → 保留 #17、...
（dry-run）没改库；去掉 --dry-run 才会真的作废旧记录。
```

`--dry-run` 只读，不要求写权限；真跑会改库，走写权限（管理员 / 白名单）。
`import` 走的是同步合并、不调 embedding，所以**导入后建议跑一次 `/mem dedupe`** 收尾。

## LLM 工具

- `memory_add(subject, predicate, value)` — 记事实（value 要一句判断句，≤120 字）
- `memory_search(query)` — 查记忆（每条带更新时间，作废的标「已作废」）

两个工具都走和指令一样的归一化 / 长度 / 叙事体 / 敏感串 / 相似去重 / 权限校验。写被拦时工具会返回原因，模型能自己转述给用户。

### 为什么默认不注入（`auto_inject_n = 0`）

1. **污染人格**：技术记忆（路径、端口、仓库名）混进每轮 system prompt，闲聊时会突然
   蹦出「上次那个端口是…」，人格直接崩。
2. **破坏 prompt 缓存**：注入内容随对话变化，前缀一直变，供应商侧的缓存命中率掉光，
   长对话成本翻倍。

要注入就显式配 N>0；不配时模型仍可通过 `memory_search` 按需查。

v0.2.0 起注入本身也做了净化（`auto_inject_n > 0` 才生效）：

- **按主语去重**再截条数：同一个主语只注入 `updated_at` 最新的那条，不重复占位
- **`inject_char_cap`（默认 600）**：注入内容总字数超限就按 `updated_at` 倒序砍，先丢最旧的
- `auto_inject_n = 0` 的行为**一个字符都没变**：一条都不注入

## 检索链路与降级

```
关键词 LIKE 召回 ┐
                 ├→ 按 mem id 去重 → 向量打分 → 噪声地板 → rerank → 同(主语,谓词)留最新 → 输出
向量召回        ┘
```

三级降级（任何一级失败只写 `logger.warning`，回复照常）：

| 情况 | 结果 |
| --- | --- |
| 向量通 + 重排通 | 向量 + 重排 |
| 向量通、重排挂（超时/报错/空返回） | 按向量排序 |
| 向量挂（404/超时/模型不存在） | 关键词（+ 能重排就重排） |
| 都没配 / 全挂 | 纯关键词 |

**噪声地板**可配（`min_score`，默认 0.2）：一个关键词都没命中、向量相似度又低于此值的候选会被丢掉，
避免 `/mem search 月亮` 把「小满 也喜欢 看星星」当结果返回。宁可空手而归，不给假结果。

**输出净化**（v0.2.0）：按 `(subject, predicate)` 分组，每组只留 `updated_at` 最新的一条。
同一条事实在多个 scope / 多次导入后可能有多条有效记录，一起返回会让模型同时看到新旧两个值、
还分不清哪个是现在的。每条结果都带更新时间，作废的记录标「已作废」。

### 向量缓存

- `vec` 表按 `(mem_id, model)` 存 float32 向量；`model` = `provider_id|模型名|维度`。
- 内容哈希 `sha256(subject|predicate|value)` 落库校验：内容没变就不重复调 embedding。
- 换模型 / 换维度 → `model` 变了，旧缓存视为过期，先走关键词，能补算的再补算。
- 写入相似去重用的是**只对 value 算的向量**，单独一个缓存键（`...|value`，哈希 `sha256("value:"+value)`），
  和检索用的整行向量互不干扰。

> 规格里 `vec` 是四列 `(mem_id, model, dim, vec)`，实现多了一列 `text_hash` ——
> 不加这一列没法判断「内容有没有变、缓存还能不能用」，见 `store.py` 顶部注释。

## 存储

sqlite，默认 `<AstrBot data>/mem.db`（**不写插件代码目录**，升级插件不会丢数据）：

```sql
mem(id, scope, subject, predicate, value, confidence, source,
    created_at, updated_at, valid_from, valid_to, superseded_by)
vec(mem_id, model, dim, vec BLOB, text_hash)
```

- **同槽覆盖**：同一 `(scope, subject, predicate)` 只保留一条 `valid_to IS NULL`；
  写新值时旧行填 `valid_to` + `superseded_by` 指向新 id。
- **同义覆盖**：同 scope + 同主语、值向量余弦 ≥ `dedupe_threshold` 的记录，走同一条覆盖路径。
- **不物理删除**：`forget` / `dedupe` 只是填 `valid_to`，`export` 能看到完整历史。
- 检索只读有效记录；LIKE 里的 `%` / `_` 会转义，不会一条 `/mem forget %` 把库清空。

## 权限与隐私

| 操作 | 谁能做 |
| --- | --- |
| `add` / `forget` / `import` / `dedupe`（真跑） | 管理员、`allowed_users`、`allowed_groups`、`allow_all` |
| `search` / `list` / `stats` / `dedupe --dry-run` | 所有人（读放开，dry-run 不改库） |
| `export` | 同写权限（它会落盘且导出全量数据，比规格多加一层保险） |

- 写入拦截凭证特征串：`sk-`、`gho_`、`ibssh_`、`AKIA`、`-----BEGIN` → 拒写。
- 长度上限：内容 `max_value_len`（默认 120 字）、主语/谓词各 40 字，超长拒写。
- 记忆库是本机 sqlite 文件，插件**不上传任何数据**；embedding / rerank 走你配置的
  provider，文本会发到对应服务商（和你平时用知识库一样）。
- 库打不开/损坏：指令回「记忆库读不了」，不抛 traceback，不影响其他插件。

## 排错表

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `/mem add` 回「内容太长（xx 字，上限 120）」 | 写成了段落 | 压成一句事实；确实要长内容就调 `max_value_len`（不建议超过 300） |
| `/mem add` 回「这条像日记（以「我」开头）」 | 叙事体 | 改成「主语 + 谓词 + 值」；确实要存就关 `reject_narrative` |
| 写了两次，库里只有一条 | 这是设计：同槽 / 同义都走覆盖 | 想留两条就换 `predicate`（槽位不同）或加 `--scope` 分池 |
| 相似去重没生效，同主语攒了一堆近义记录 | 没配 embedding provider，或 `dedupe_threshold=0` | `/mem stats` 看 embedding 那行；配 provider 或把阈值调回 0.95 |
| 明明是新记忆，检索却只出一条 | 检索按 `(主语,谓词)` 只留最新 | 正常；要看历史用 `/mem export` |
| `/mem search` 什么都搜不到 | `min_score` 调太高 / 关键词全不匹配 | 把 `min_score` 调回 0.2，或直接用关键词里出现过的字 |
| `/mem dedupe` 说可合并 0 组 | 没有同 `(scope,主语,谓词)` 的重复有效记录 | 正常；相近但不是同槽的重复要靠 `dedupe_threshold` 在写入时收 |
| `/mem search` 总是「仅关键词（向量/重排都不可用）」 | 没配 embedding provider，或 provider ID 填错 | `/mem stats` 看 embedding 那一行；留空让它自动挑，或填 WebUI 里的 ID |
| `stats` 显示 dim 是 `?` | provider 没实现 `get_dim()` 或读不到 | 不影响使用，缓存键会退化成 `id|模型|0` |
| 换模型后第一次检索变慢 | 缓存按模型隔离，旧向量全过期 | 正常，第一次会现算；算不出来就只走关键词，不报错 |
| 回复「记忆库读不了：file is not a database」 | `db_path` 指到了非 sqlite 文件 / 文件损坏 | 改 `db_path` 或把坏文件挪走；插件不会覆盖它 |
| 回复「没权限写记忆」 | 不在白名单 | 把自己的号加进 `allowed_users`，或把群号加进 `allowed_groups` |
| 升级插件后记忆没了 | `db_path` 填在了插件代码目录 | 默认会拒绝这种路径并回落到 data 目录，检查日志里的 `[mem]` 提示 |
| 日志刷 `记忆注入失败，本轮跳过` | `auto_inject_n>0` 但检索链路出错 | 不影响聊天；按上面的 provider 排查，或把 `auto_inject_n` 设回 0 |

## 自测

离线跑，不打任何模型接口（假 astrbot 桩 + 假 embedder / reranker）：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 selftest.py --offline
```

期望输出最后一行：

```
125/125 通过
```

覆盖：add→search、同槽覆盖、forget、超长/敏感词、库损坏、白/黑名单、注入开关、
embedding 404/超时、rerank 超时、换模型、缓存命中、参数解析、export/import、db_path、
handler 签名检查（防 `*args` 那种 `_empty() takes no arguments` 坑）；

v0.2.0 新增（39 条）：120/121 字边界、叙事体拒写、归一化（全角空格/连续空白/中文标点）、
相似去重（cos 0.97 合并、0.8 不合并、无 provider 跳过、embedding 挂了照样写、
`dedupe_threshold=0` 关闭）、检索 `(主语,谓词)` 去重 + 更新时间展示 + 作废标注、
`min_score` 双阈值对比、`/mem dedupe` 的 dry-run 与真跑、注入按主语去重 + `inject_char_cap` 裁剪。

## 兼容性

- 需要 AstrBot **>= 4.9.0**；API 在 **4.29.3** 源码上逐条核对（`Star` / `filter.command` /
  `filter.llm_tool` / `on_llm_request` / `Context.get_provider_by_id` /
  `EmbeddingProvider.get_embedding` / `RerankProvider.rerank`）。
  低版本若没有 embedding / rerank provider 相关接口，会自动退化成关键词检索，不报错。
- 配置项 `type: float` 合法（`astrbot/dashboard/services/config_service.py:61`），
  `dedupe_threshold` / `min_score` 用 float 存。
- rerank 调用签名来自 `astrbot/core/provider/provider.py:431`：
  `async def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[RerankResult]`；
  返回结构 `RerankResult(index, relevance_score)` 在 `astrbot/core/provider/entities.py:609`。
- 单进程 sqlite（未开 WAL），多进程/多实例共用一个 `db_path` 不在支持范围内。
- **0.1.0 → 0.2.0 无需迁移**：表结构没动，新逻辑只影响新写入与检索输出。

## 分工与验收

| 角色 | 职责 |
| --- | --- |
| 点点·小弟 | 写实现、开分支 `feat/v0.2-noise`、push、写自测 |
| 点点 | 看 diff → 在 friend-box 用 `fbci.sh` 跑构建与自测原始输出 → 判定合不合并 |

只开 PR，不直推 main。

## 许可

MIT
