# astrbot_plugin_nickname_mapper

给同一个用户在不同对话里取不同的名字，而且**只改 LLM 看到的那一面**。

AstrBot 的上下文按 `unified_msg_origin`（会话）隔离，但同一个会话下的多条独立对话（conversation）共享同一个用户身份。本插件在此之上补了一层：按 `(user_id, session_id, conversation_id)` 记录昵称，在请求发往模型前把身份信息替换掉。

- 不改平台侧资料，群名片、QQ 昵称、好友备注一律不动
- 不影响其他用户、其他对话
- 纯请求侧改写，模型看到的是你设定的名字

## 特性

| 能力 | 说明 |
| --- | --- |
| 对话级隔离 | 同一个群里，A 对话叫「小猫」，B 对话可以叫「研究员」，互不干扰 |
| 只影响 LLM | 仅改写 `ProviderRequest` 中的身份字段与 `event.message_obj.sender.nickname` |
| 自动建对话 | 首次设置时若当前会话还没有独立对话，会自动创建一条 |
| 兼容旧键 | 早期只按会话记录的键（无 conversation_id）仍可读到，不会失效 |
| 原子写入 | 数据落盘走临时文件 + `os.replace`，断电或中断不会写坏 JSON |
| 多平台 | 基于 `unified_msg_origin`，QQ / Telegram / Discord 等平台通用 |

## 安装

1. 把本目录放进 AstrBot 的 `data/plugins/` 下
2. 在 WebUI 的「插件管理」里重载插件
3. 无需安装额外依赖（`requirements.txt` 为空）

也可以直接在 WebUI 里填仓库地址安装：

```
https://github.com/alva-lorenz-nl/astrbot_plugin_nickname_mapper
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `/设置昵称 <昵称>` | 为**当前对话**设置昵称 |
| `/查看昵称` | 查看当前对话昵称，并列出本会话下所有已设置的记录 |
| `/昵称列表` | `/查看昵称` 的别名，输出完全一致 |
| `/删除昵称` | 删除当前对话的昵称 |
| `/删除昵称 <ID>` | 按对话 ID（可只填前 4 位）删除 |
| `/删除昵称 <序号>` | 按 `/查看昵称` 列表里的序号删除 |
| `/删除昵称 default` | 删除旧版「会话级」记录 |
| `/删除昵称 all` | 清空本会话下当前用户的全部记录 |

`/查看昵称` 的返回示例：

```
当前对话 [晚饭吃什么(a1b2)] 昵称: 小猫

📋 当前会话已设置的对话昵称列表：
1. 晚饭吃什么(a1b2) [当前] → 小猫  (ID: a1b2)
2. 论文修改(c3d4) → 研究员  (ID: c3d4)

💡 提示: 可用「/删除昵称」删当前对话，或「/删除昵称 <ID/序号>」清理指定记录
```

## 配置

`_conf_schema.json` 只暴露一个开关：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 关掉后所有命令直接返回提示，`on_llm_request` 也不再改写 |

## 数据存放

昵称映射写在 AstrBot 数据目录下：

```
data/plugin_data/astrbot_plugin_nickname_mapper/nicknames.json
```

结构为 `{"(user_id,session_id,conversation_id)": "昵称"}`。备份或迁移时把这个文件一起带走即可；删除它等于清空所有昵称。

## 实现说明

插件挂在 `@filter.on_llm_request(priority=5)`，在请求构造完成后、发往 provider 前介入：

1. 用 `conversation_manager.get_curr_conversation_id()` 只读取当前对话 ID
2. 依次尝试 `(user_id, session_id, cid)` → `(user_id, session_id)` → `session_id` 三种键
3. 命中后替换 `extra_user_content_parts` 与 `req.prompt` 中的 `Nickname:` 字段；若整段 prompt 里都没有身份行，则在开头补一行 `[User ID: ..., Nickname: ...]`

替换采用逐行匹配，只动同时含 `User ID:` 与 `Nickname:` 的行，其余文本原样保留。

## 注意

- 昵称只对**后续请求**生效，历史消息里的旧称呼不会被回改
- 若某个平台不提供 `unified_msg_origin`，会退化为按 `get_session_id()` 记录
- 关闭 `enabled` 不会清除已有数据，重新打开后立即恢复

### 关于「全局改写」的说明（有意设计）

`on_llm_request` 除了改写 `ProviderRequest`，还会同时把 `event.message_obj.sender.nickname` 替换成映射后的昵称。

这是一个**有意为之的全局副作用**：排在优先级 5 之后的其他插件（记忆、日志、身份类等）在读取 `sender.nickname` 时，拿到的也会是对话线级别的专属昵称，从而保证同一条对话线上各插件看到的人设称呼是统一的。

需要注意的代价：该替换在事件对象上是持久的，若某个插件依赖**平台原始昵称**（如群名片、好友备注），应改为读取平台侧的原始字段，而不要用 `sender.nickname`。

### 关于「自动新建对话」的说明

`/设置昵称` 首次调用时，若当前会话还没有独立对话，会通过 `new_conversation` 创建一条并切换过去。

由于 AstrBot 的 `new_conversation` 语义本身就会把当前会话切到新对话，因此这一步相当于**主动为用户开启一条新对话线**——新昵称只在这条线上生效，原对话线的上下文与称呼不受影响。若你希望昵称绑在“原有对话”上，请先手动创建对话再设置昵称。

## 许可

[MIT](./LICENSE)
