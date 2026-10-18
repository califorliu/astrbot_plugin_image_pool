# AGENTS.md — astrbot_plugin_image_pool

> **本文件属于发布层。** 本目录既是插件仓根，也是打包根；它写的是**仓库级、长期稳定**的规则，
> 面向任何在本仓工作的 AI 协作者。改动本目录前先读它。
>
> 同一类事实只设一个权威来源，本文件不复制别处正文：
> 用户文档 [README.md](README.md) ｜ 版本变更 [CHANGELOG.md](CHANGELOG.md) ｜
> 开发环境 / 架构 / 代码规范 / 模板 / 自检用法 / 提交规范 [CONTRIBUTING.md](CONTRIBUTING.md)。
>
> 若本目录位于**维护者的开发工作区**中（即上一层还有一份 `AGENTS.md`），那份描述本机环境、
> 工作区布局与文档路由，**不随包发布**。两层分工是：本层管「这个插件仓库的规则」，
> 上层管「这台机器 / 这个工作区的规则」。**同一条规则不要写在两层** —— 重复会浪费上下文预算，
> 并且迟早分叉。公开克隆本仓的读者没有上一层，忽略即可。

## 1. 这是什么

AstrBot 的 **Star 插件**：按分类（= tag）管理图片 —— 收录（多分类与引用）、随机出图、
分类整理（增删改 / 别名 / 屏蔽 / 去重）、可选群内自动采集、群级三档权限、
保留期与容量清理，并提供 WebUI 管理页。

- **版本权威链**：`metadata.yaml: version` = `main.py` 里 `@register(...)` 的版本字面量
  = `CHANGELOG.md` 里的 `## vX.Y.Z` 小节。**当前版本号只在上面这三处写**（本文件不重复抄写，
  免得静默过期）。
  ⚠️ 本项目自检**没有**强制这条一致性的断言（`@register` 用的是字面量而非常量），
  发布前必须人工核对这三处，并由打包守卫兜底 —— 见 [CONTRIBUTING.md](CONTRIBUTING.md) §7.5。
- **对端**：`astrbot_plugin_x_monitor`，**只**通过本插件公开方法 `ingest_files` 调用。
- **支持平台**：`aiocqhttp`（多图合并转发依赖协议端实现）。

## 2. 硬性不变量

1. `UNIQUE (char_name, seq)` 与 `UNIQUE (char_name, sha256)` —— 结构上保证分类内 id 与内容唯一；
   `seq` 即用户可见 id，**删除后不回收、不重排**。
2. **任何写入路径都绝不覆盖已存在的文件**：文件名由 `_next_free_dest()` 探测磁盘空位分配，
   写入与复制走 `_create_exclusive()`、搬移走 `_move_exclusive()`，两者都用 `open(dest, "xb")`
   拿到原子独占。
3. **同一物理文件可能被多行引用**（多 tag、老库的折叠分类）：删除或搬移前必须查
   `_reference_index()`，并且这条守卫要铺到**全部会 unlink 的路径** ——
   包括**自动触发**的保留期清理（漏一条就会静默删掉别人的图）。
4. **折叠冲突**（仅大小写不同的两个分类共用同一物理目录）时**拒绝删除 / 迁移**，
   提示先用 `/图库 别名 收编` 合并。
5. **破坏性功能默认关闭**（`allow_purge: false`、`auto_collect: false`、`retention_days: 0`）；
   清理类功能取到非法配置时 **fail-close**（宁可不清，也不误删人工收录的图）。

> **断言覆盖到哪里**：第 1、4、5 条有自检用例；第 3 条只覆盖了部分路径；
> **第 2 条「绝不覆盖」这条性质没有直接断言** —— 自检里检索不到
> `_next_free_dest` / `_create_exclusive` / `_move_exclusive`。
> 独立变异实测（2026-10-10）：让 `_next_free_dest` 不看磁盘占用、让 `_create_exclusive` 改成
> `"wb"` 覆盖、让 `_reference_index` 恒返回 `{}`，自检**都是 464 PASS / 0 FAIL**；
> 而把 `_create_exclusive` 改成**永不写文件**会立刻变红（4 FAIL）——
> 也就是说「写得进去」被间接守着，「**不覆盖**」没人守。
> 改动写入路径时**不要指望自检会变红**，必须逐条路径人工核对。
> 自检覆盖范围的完整清单见 [CONTRIBUTING.md](CONTRIBUTING.md) §6.4。

## 3. 硬性规则

- **运行时数据只走 `StarTools.get_data_dir(PLUGIN_NAME)`**
  （= `data/plugin_data/astrbot_plugin_image_pool/`），**绝不写进插件目录** ——
  插件目录会被升级整体替换。
- **权限用插件自己的群级三档**（`whitelist` / `post` / `none`）+ `plugin_admins` / `group_admins`；
  **不要**用 AstrBot 的全局管理员当插件权限模型（那是提权）。
- **配置项只在 `_conf_schema.json` 里声明一次**，代码读到的键必须是它声明过的键。
  布尔配置一律经 `to_bool()`：配置里写字符串 `"false"` 会被判为真，
  那样 `allow_purge`（安全开关）与 `auto_collect`（隐私开关）会**反向打开**。
- **名单类配置严格区分「群号」与「用户 ID」两个命名空间**：
  `plugin_admins` 只按发送者 ID 匹配，`open_groups` / `post_groups` 只按群号或 UMO 匹配。
- **跨插件只暴露公开方法**（`ingest_files`）；本插件也绝不直写对端数据库。
  该接口的**基础设施失败必须抛异常**，不得伪装成 `skipped`。
- **被动采集绝不回拉引用消息**：否则任何一条群消息都能诱导机器人去取任意 `message_id`。
- **加固类改动优先「移动 / 复制」而非「删除」**；文件操作与事务的顺序要能解释清楚，
  不允许出现「行回滚了、文件已搬走」的悬空行。
- **注释与文档用中文**；面向公开的内容不写本机私有事实（实例名、群号、绝对路径、邮箱），
  也不写开发期的审查轮次编号与阶段代号（它们会随包发布）。
- **未获用户明确授权，不执行 commit / push / 发布。**

## 4. 验证要求

- 改完必跑 `python tests/offline_check.py`（Windows 控制台需先 `PYTHONUTF8=1`）。
- **离线自检是变更探针，不是验收证据**：绿灯只说明「没弄坏已知断言」。
- **退出码 1 既可能是断言失败，也可能是在跑到汇总前就中断**（本自检没有区分两者的专用码）
  —— **以末行输出为准**。
- 声称修好之前必须做一次**证伪**（把改动改回旧行为，对应断言必须变红），
  且**作者不得自验** —— 改动者不能用自己的自检结论结案。
- 需要真机 / 协议端才能确定的结论（合并转发的实际形态、WebUI 在真实 Dashboard 的表现、
  群级三档权限的实际效果、与对端的联动）**必须标 `待核实`**。
- 命令、判据与断言模板见 [CONTRIBUTING.md](CONTRIBUTING.md) §6。

## 5. 文档权威链

| 事实 | 权威来源 |
|---|---|
| 项目用途与用户用法 | [README.md](README.md) |
| 版本变更记录 | [CHANGELOG.md](CHANGELOG.md) |
| 开发环境、架构、代码规范、模板、自检用法、提交规范 | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 仓库级规则 | 本文件 |
| 配置项定义与默认值 | `_conf_schema.json` |
| 版本号 | `metadata.yaml` |
| 跨插件接口签名与语义 | `main.py` 里 `ingest_files` 的 docstring |
| 权限三档与名单语义 | `main.py` 的 `MODE_CAPS` 与 `_in_id_list()` |
| 清理语义 | `main.py` 的 `_db_sweep_retention()` 与 `_conf_schema.json` |

改动时保持「一类事实一个权威来源」：别处只写简短摘要和链接，不复制正文。
