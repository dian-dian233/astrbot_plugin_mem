# astrbot_plugin_mem

给 AstrBot 用的**全局记忆**插件：任何私聊、任何群聊、任何号，读写同一份事实库，不按会话隔离。

> 状态：**开发中**，代码分支 `feat/mem-plugin`。本仓库当前只有骨架与规格，实现由 tutu 提交。

## 为什么需要它

AstrBot 自带的长期记忆有两个问题：注入的是二次加工摘要（会过期），而且按会话过滤（换个会话就失忆）。这个插件换一种存法——存**事实四元组**，同槽新值自动作废旧值：

```
(scope, subject, predicate) → value   + valid_from / valid_to / superseded_by
```

所以「key 存在 A 路径」改成「存在 B 路径」时，旧记录会被打上 `valid_to`，检索只出新的。不会新旧混着一起读出来。

## 计划能力

- 指令：`/mem add | search | list | forget | stats | export | import`
- LLM 工具：`memory_add` / `memory_search`
- **默认不注入**（`auto_inject_n = 0`）：不往 prompt 里塞记忆，避免污染人格、也护住 prompt 缓存
- 向量召回 + 重排：**复用 AstrBot 里已配置的 embedding / rerank provider**，插件自己不碰 key
- 三级降级：向量挂 → 关键词；重排挂 → 向量序；全挂 → 纯关键词
- 安全：拦截 `sk-` / `gho_` / `ibssh_` / `AKIA` 等敏感串，单条 500 字上限
- 权限：写操作默认仅管理员与白名单，读放开

## 分工与验收

| 角色 | 职责 |
| --- | --- |
| tutu（小弟） | 写实现、开分支 `feat/mem-plugin`、push、写自测 |
| 点点 | 看 diff → 在 friend-box 用 `fbci.sh` 跑构建与自测原始输出 → 判定合不合并 |

只开 PR，不直推 main。

## 许可

MIT
