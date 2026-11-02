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

## 指令

| 指令 | 作用 |
| --- | --- |
| `/mem add <主语> <谓词> <值>` | 写一条事实（自动归一化、去噪、查重） |
| `/mem search <关键词> [数量]` | 召回 + 融合 + 重排 |
| `/mem list [数量]` | 最近的有效记录 |
| `/mem forget <关键词>` | 作废（填 `valid_to`，不物理删） |
| `/mem stats` | 条数 / provider 状态 / 迁移情况 |
| `/mem dedupe [--dry-run]` | 合并同主语下的重复 |
| `/mem consolidate [--dry-run]` | 语义合并（保留最完整那条，importance 取最大） |
| `/mem export [路径]` / `/mem import <路径>` | 导出 / 合并导入 JSON |
| `/mem import-lm [--dry-run] [--path]` | **从 LivingMemory 直接搬家** |

另外注册了 `memory_add` / `memory_search` 两个 LLM 工具，模型可以自己决定什么时候记、什么时候查——而不是每轮往 prompt 里塞记忆。

## 从 LivingMemory 搬家

装过 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 的话，不用导出文件：

```
/mem import-lm --dry-run     # 先看会搬多少，一个字不写
/mem import-lm               # 真搬
```

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

## 开发

实现与验收分开：改动由 tutu 开分支提交 PR，构建和自测由维护者在干净的 `instabox-friend-box` 上跑一遍原始输出才合并。自测全程离线——假 `astrbot.api` 模块桩 + 假 embedder / reranker，不打真实接口。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 selftest.py --offline   # 当前 308 条断言
```


## 目录结构

v0.5.0 起按职责分层，根目录只留入口和元数据：

```
astrbot_plugin_mem/
├── main.py              Star 子类：指令、LLM 工具、注入钩子
├── core/
│   ├── store.py         sqlite 存取、覆盖语义、导入导出
│   ├── retrieve.py      召回 / RRF 融合 / 重排 / 降级
│   ├── lifecycle.py     TTL / 衰减 / 访问强化
│   ├── livingmemory.py  从 LivingMemory 搬家
│   └── textnorm.py      归一化与三元组切分
├── _conf_schema.json    配置项
├── metadata.yaml        插件元数据
├── selftest.py          离线自测（假 astrbot 桩）
└── README.md
```

`core/` 里不许 `import astrbot`，业务逻辑保持可脱离框架单测。

## License

MIT
