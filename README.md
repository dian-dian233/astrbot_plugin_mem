# astrbot_plugin_mem

**给 AstrBot 的全局记忆：不按会话隔离，换群、换号、换机器，都还是同一个我。**

---

## 让对话留下记得住的东西

AstrBot 自带的长期记忆有两个毛病，用久了很明显。

一是**它记的是叙事**。「我跟他把记忆分两层讲了，他说会话隔离难受」——这种句子读着像日记，检索时对不上问题；真正有用的只有一句「框架记忆有会话隔离开关」。

二是**它会过期而不自知**。记忆里写着「key 存在 A 路径」，那个路径早迁走了，下次照着一念就指错路。

这个插件的做法很土但管用：**只存事实，一格只留一条，新的自动让旧的作废。**

```
(scope, subject, predicate)  →  value
                                + valid_from / valid_to / superseded_by
```

所以「主力机是 friend-box」改成「主力机是 AstrBot 沙盒」时，旧记录被打上 `valid_to`，检索只返回新的那条，永远不会新旧混着读出来。

## 从记住，到想起来

存下来只是第一步，能不能在需要时被想起来才是难点。检索走三段：

**召回**（向量 top-30 + 关键词 BM25）→ **融合**（RRF，两路都命中的加分）→ **重排**（rerank 模型取 top-5）

每一段都可以缺——向量接口挂了退关键词，重排挂退向量序，全挂就只剩关键词匹配。**任何一级失败都不抛错，只记一条 warning**，回复照常。

## 开始使用

**装**：WebUI → 插件 → 安装，填 `https://github.com/dian-dian233/astrbot_plugin_mem`
或者下载 Release 里的 zip，解压丢进 `data/plugins/` 再重载插件。

**配**：一个都不用填。`db_path` 留空 → 用 AstrBot 的 data 目录；`embed_provider_id` / `rerank_provider_id` 留空 → 自动挑你在 AstrBot 里已经配好的 embedding / rerank provider（插件自己不碰任何密钥）。

**用**：

```
/mem add 点点 主力开发机 friend-box（4核7.8G）
/mem search friend-box
```

换个群再搜一次——搜得到，就说明跨会话通了。

**看**：控制台 → 插件 → 全局记忆 → 「memory」页面，表格 + 关系图都能看（见下面「WebUI 管理页」）。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/mem add <主语> <谓词> <值>` | 写一条事实（自动归一化、去噪、查重） |
| `/mem search <关键词> [数量]` | 召回 + 融合 + 重排 |
| `/mem list [数量]` | 最近的有效记录 |
| `/mem sessions [数量]` | 按会话聚合：会话 ID · 条数 · 最近更新 |
| `/mem forget <关键词>` | 作废（填 `valid_to`，不物理删） |
| `/mem stats` | 条数 / provider 状态 / 迁移情况 |
| `/mem dedupe [--dry-run]` | 合并同主语下的重复 |
| `/mem consolidate [--dry-run]` | 语义合并（保留最完整那条，importance 取最大） |
| `/mem export [路径]` / `/mem import <路径>` | 导出 / 合并导入 JSON |
| `/mem import-lm [--dry-run] [--path]` | **从 LivingMemory 直接搬家** |

另外注册了 `memory_add` / `memory_search` 两个 LLM 工具，模型可以自己决定什么时候记、什么时候查——而不是每轮往 prompt 里塞记忆。

## WebUI 管理页

v0.6.0 起带一个管理页，不用记指令也能看库、改库。

**入口**：AstrBot 控制台 → 插件 → 已安装 → 全局记忆 → 页面标签「memory」。
地址形如 `/plugin-view/astrbot_plugin_mem/memory`（页面注册在插件的 `views/memory/index.html`，
每页必须有 `index.html`，见 `astrbot/dashboard/services/plugin_page_service.py:29-30`）。

> 不要用浏览器直接打开 `views/memory/index.html`。页面跑在 dashboard 的 iframe 里，靠
> 父窗口的 `AstrBotPluginView` 桥发请求（带登录态）；直接打开拿不到凭证，会提示「页面桥未就绪」。

![WebUI 管理页截图](docs/webui.png)

*（截图占位：`docs/webui.png` 待补，四张统计卡 + 表格 + 关系图）*

**能干什么**

| 区域 | 功能 |
| --- | --- |
| 顶部 | 搜索框（subject / predicate / value 任一命中，回车或停顿 250ms 生效）、类型下拉、**会话下拉**、新增按钮 |
| 统计卡 | 总数 / 有效 / 已作废 / embedding provider 是否可用 |
| 表格 | id、subject、predicate、value、mtype、importance、更新时间，每行一个「作废」按钮 |
| 操作区 | 同槽去重、同主语合并；带 `dry-run` 勾选（默认勾上），执行完回显一行结果 |
| 关系图 | **簇星图**：按 subject 聚簇，只画条数最多的 16 组主语；每簇一个簇心（`主语 · 条数` + `+N`），簇内最多 4 个记忆点；滚轮缩放 / 拖拽平移 / 悬停出 tooltip |

作废是软删：只填 `valid_to`，行还在库里，`/mem stats` 的「历史」会 +1。

**后端接口**（前缀 `/<插件名>/mem-api/`，只有登录后的控制台能调）

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `list?q=&type=&origin=&limit=&offset=` | GET | 分页列表，返回 `{total, items[]}`；`origin` 空 = 全部会话 |
| `sessions` | GET | `{items:[{origin,label,count,last_updated}], count, total}`，会话下拉的数据源 |
| `stats` | GET | 统计 + provider 状态 |
| `add` | POST | `{subject, predicate, value, scope?, mtype?, importance?}` |
| `forget` | POST | `{id}` 按 id 作废（不是按关键词，不会连带） |
| `dedupe` | POST | `{dry_run}` 同槽去重 |
| `consolidate` | POST | `{dry_run}` 同主语语义合并 |
| `graph` | GET | `{nodes[], edges[]}`，最多 300 个节点 |

参数不合法一律 `400` + 中文提示；单条记录字段缺失只影响那一行，不会让整页报错。
没装老版本核心（没有 `astrbot.api.web`）时插件照常加载，只是没有页面。

## 会话归属（v0.8.0）

每条记忆多记一个 `origin`：写入它的会话 `unified_msg_origin`，形如
`diandian:GroupMessage:738458661`。这样能回答「这条记忆是哪个群说的」，也能看出某个群关心什么。

| 写入通道 | origin |
| --- | --- |
| `/mem add` | 当前事件的 umo |
| LLM 工具 `memory_add` | 当前事件的 umo |
| WebUI「新增记忆」 | 空（没有 event，不伪造） |
| `/mem import` / `/mem import-lm` | 空；同槽覆盖时沿用旧记录的 origin，不抹掉已有归属 |

看：

```
/mem sessions          # 会话 ID · 条数 · 最近更新
/mem sessions 20       # 只看前 20 个
```

```
记忆来源：3 个会话，共 12 条有效记忆
1. diandian:GroupMessage:738458661 · 8 条 · 最近 2026-11-02 10:20
2. diandian:FriendMessage:10001 · 3 条 · 最近 2026-11-02 09:12
3. 未记录来源 · 1 条 · 最近 2026-10-28 18:04
```

老库升级不用做任何事：启动时 `PRAGMA table_info(mem)` 发现缺列就
`ALTER TABLE mem ADD COLUMN origin TEXT NOT NULL DEFAULT ''`，已有记录归到「未记录来源」；
迁移幂等，跑几次都一样，search / list 不受影响。

管理页顶部多了「会话」下拉（数据来自 `GET /mem-api/sessions`），选中后列表按该会话过滤；
详情抽屉里能看到「来源会话」，空值显示「未记录」。

## 关系图（v0.8.1）

v0.8.0 及以前的关系图是「所有节点撒在一个同心圆上」：300 个点、每个点的标签都是自己，最后挤成一圈毛线，读不出任何信息。v0.8.1 换成**簇星图**：

- 按 subject 聚簇，只取**条数最多的 16 个**主语，簇心均分在半径约 200 逻辑像素的圆周上
- 每簇一个簇心：4px 实心点 + 外圈 8px 细环，颜色 `#1D4B45`，标签 `主语 · 条数`（主语超过 10 字省略），余量在标签后跟 `+N`
- 簇内最多画 **4 个**记忆点，颜色按 `mtype`，半径 `3 + importance*3.4`；簇心到成员画极细连线，超过 60px 不画
- 成员点朝**圆心方向**扇形展开、标签朝**外**，两拨人各占一半，互不遮挡；标签带白色底片，压到点上也读得清
- **交互**：滚轮缩放（0.5–3 倍，以指针为锚）、按住拖拽平移、悬停 14px 内出 tooltip（DOM 绝对定位，不是 canvas 画字），`pointerleave` 收起
- 底部注记 `N 条记忆归到 M 组主语，另有 K 条折叠`，接口截断时补一句「只返回前 300 条」
- 画布 470px 高，底下铺 40px 极淡网格；画布、图例、注记三段竖排，不重叠

顺带的视觉收敛：去掉卡片投影（`--shadow-1:none`）、概览数字换衬线 30px、类型色改低饱和（fact `#4A6A78` / event `#4E7A5E` / plan `#8A6A3A` / preference `#6C5F7A`，图例与堆叠条同步）。

接口也跟着补了字段：`/mem-api/graph` 的每个节点现在带 `subject` / `predicate`，根上带 `subjects`（**全部**有效行的主语组数，不受 300 条截断影响）。之前前端只有 `label`（`主语 · 谓词`）可拆，拆不准，干脆每条记忆自成一簇——这才是那圈毛线的根因。


装过 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 的话，不用导出文件：

```
/mem import-lm --dry-run     # 先看会搬多少，一个字不写
/mem import-lm               # 真搬
```

## 从 LivingMemory 搬家

插件会自己找库（`data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db` 等 6 个候选位置，找不到列出搜过的路径），**只读打开**（`mode=ro`，全程不写它的文件），读主表 `memory_atoms`。

映射规则：`atom_type` → `event / fact / preference / plan`；`reinforcement_count` → `access_count`；epoch 浮点 → ISO 时间；`status != active` 跳过；内容完全重复跳过；切不出主谓宾的长叙事整条塞进 `value`，不丢原文。

实测一个 5599 条的库：导入 3425 条（切出三元组 2360 / 退回整体 1065），跳过归档 604、重复 8，覆盖同槽旧值 1445 条，用时 0.3 秒。

## 记忆的生命周期

不是所有记忆都该活一样久。四种类型，各有 TTL 和衰减曲线：

| 类型 | 默认 TTL | 曲线 | 适合 |
| --- | --- | --- | --- |
| `fact` 事实 | 3650 天 | 指数 | 地址、配置、身份这类不该忘的 |
| `preference` 偏好 | 365 天 | 指数 | 口味、习惯、说话方式 |
| `event` 经历 | 30 天 | 指数 | 今天干了什么 |
| `plan` 待办 | **2 天** | 阶梯 | 明天要交的东西，过期自动下线 |

检索分数是三个因子相乘：

```
最终分 = RRF 融合分 × 衰减系数 × (0.5 + 0.5 × importance)
```

衰减系数按曲线算：指数用半衰期 `exp(-ln2 × 天数 / (ttl/2))`，线性用 `max(0, 1 - 天数/ttl)`，阶梯是「超过 TTL 直接给 0.05 地板」。

再加一层**访问强化**：被搜到过的记忆衰减更慢（最多保护 10 次），每个衰减回合访问计数砍半，避免旧热点永远赖在榜首；`importance ≥ 0.9` 的记录直接豁免。

## 和自带记忆的关系

不是替代，是分工：

- **框架的 RAG 记忆**继续管「我今天跟他聊了什么」这类叙事和情绪上下文；
- **这个插件**管「事实是什么」，一格一条，可覆盖、可追溯；
- 因为它**默认不往 prompt 里注入**（`auto_inject_n = 0`），所以不挤上下文、也不破坏 prompt 缓存——需要时按指令或工具查。

想让它自动带出记忆，把 `auto_inject_n` 调成 1~2 就行，代价是每轮多几百字。

## 边界与代价

- **只归档，不物理删除**：过期的记忆只是不再返回，库里的行还在，后悔能捞。
- **暂时没有定时守护进程**：衰减是检索时现算的，低分记忆不会自己消失，只是排不上来。每日跑一次的「记忆园艺」（合并近重复 + 归档长期未命中）在 v0.5 计划中。
- **纯标准库**：`requirements.txt` 是空的，不引 numpy / faiss / ORM。1 万条以内的余弦暴力算够快。
- **写操作默认只给管理员和白名单**：`allow_all=false`，读放开——别让别人拿你的记忆库乱写。

## 配置速查

全部有默认值，不填也能跑：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `db_path` | 空 | 空 = AstrBot data 目录下的 `mem.db` |
| `lm_db_path` | 空 | 空 = 自动探测 LivingMemory 的库 |
| `scope` | `global` | 全局共享；也可用 `group:群号` 分池 |
| `embed_provider_id` / `rerank_provider_id` | 空 | 空 = 自动挑已配置的同类 provider |
| `recall_k` / `rerank_k` | 30 / 5 | 召回条数 / 重排后保留条数 |
| `max_value_len` | 120 | 单条上限，超长拒写 |
| `reject_narrative` | `true` | 拒写「我/他/今天/昨晚」开头的日记体 |
| `dedupe_threshold` | 0.95 | 同主语相似度阈值，超过视为一件事走覆盖 |
| `min_score` | 0.2 | 噪声地板，低于此分丢弃 |
| `auto_inject_n` / `inject_char_cap` | 0 / 600 | 每轮注入条数与字数上限 |
| `rrf_k` / `agreement_bonus` | 60 / 0.02 | RRF 融合参数 |
| `protected_importance` | 0.9 | 免疫衰减的阈值 |
| `access_cap` / `access_halve` | 10 / 0.5 | 访问强化上限与衰减回落 |
| `allow_all` / `allowed_users` / `allowed_groups` | false / [] / [] | 写权限 |

## 排错

| 现象 | 原因 |
| --- | --- |
| `/mem add` 没反应 | 不是管理员、群也不在 `allowed_groups` 里 |
| 搜得到旧的、搜不到新的 | 检查 `mtype` 是不是 `plan`（2 天就过期） |
| 提示「像是日记」写不进去 | `reject_narrative` 生效了，改成「主语 + 谓词 + 值」 |
| 检索结果一堆弱相关 | 调高 `min_score`；或确认 embedding provider 挑没挑到 |
| `记忆库读不了` | `db_path` 指向的文件损坏或无权限，换个路径重试 |
| 搬家后第一次搜很慢 | 搬家不预先算向量，第一次检索会现算 |
| 管理页提示「页面桥未就绪」 | 直接用浏览器开了 `index.html`；要从控制台插件页的「memory」标签进 |
| 管理页接口 404 / 一片空白 | 插件是升级上来的、没重载（路由在 `initialize()` 里注册），在控制台点一次「重载」 |
| 关系图只画了 300 个点 | 单页上限 300，用搜索或类型下拉缩小范围 |
| 关系图只剩十来个簇 | v0.8.1 起只画条数最多的 16 组主语，余量在标签后用 `+N` 表示；底部注记会写折叠了多少条 |
| 关系图滚轮/拖拽没反应 | 老页面缓存；强刷一次控制台（Ctrl/Cmd+Shift+R）。仍不行说明插件没重载，路由还是旧的 |
| 悬停不出 tooltip | tooltip 是画布上的 DOM 层，鼠标得停在点或簇心的 14px 内；缩得太小（<0.5x）时命中按屏幕像素算，可以放大再悬停 |
| 管理页没有「memory」标签 | 核心太老（没有 `astrbot.api.web` / 插件页面能力），指令功能不受影响 |
| 升级后老记忆都显示「未记录来源」 | v0.7 及更早写入时没存 origin；升级后新写的才会带上 |
| `/mem sessions` 只剩「未记录来源」 | 记忆都是 WebUI / 导入写的，没有事件上下文；用 `/mem add` 或聊天里让模型记一条就会分出来 |

## 开发

实现与验收分开：改动由 tutu 开分支提交 PR，构建和自测由维护者在干净的 `instabox-friend-box` 上跑一遍原始输出才合并。自测全程离线——假 `astrbot.api` 模块桩 + 假 embedder / reranker，不打真实接口。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 selftest.py --offline   # 当前 536 条断言（v0.8.0 的 497 条 + v0.8.1 新增 39 条）
```


## 目录结构

v0.5.0 起按职责分层，根目录只留入口和元数据：

```
astrbot_plugin_mem/
├── main.py              Star 子类：指令、LLM 工具、注入钩子、WebUI 路由注册
├── core/
│   ├── store.py         sqlite 存取、覆盖语义、导入导出
│   ├── retrieve.py      召回 / RRF 融合 / 重排 / 降级
│   ├── lifecycle.py     TTL / 衰减 / 访问强化
│   ├── livingmemory.py  从 LivingMemory 搬家
│   ├── webapi.py        WebUI 的纯标准库逻辑层（校验 / 分页 / 关系图布局）
│   └── textnorm.py      归一化与三元组切分
├── views/memory/index.html  WebUI 管理页（单文件，零构建）
├── _conf_schema.json    配置项
├── metadata.yaml        插件元数据
├── selftest.py          离线自测（假 astrbot 桩）
└── README.md
```

`core/` 里不许 `import astrbot`，业务逻辑保持可脱离框架单测——`core/webapi.py` 同理。

## License

MIT
