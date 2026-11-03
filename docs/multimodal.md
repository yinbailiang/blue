# 多模态附件

> 方案 A（prompt 内联 base64，一次往返）+ **内容寻址 + 哈希外键 + 独立表**。
> 目标：用户可以把图片（以及任意文件）交给 agent，模型侧拿到的是标准
> ``data:`` URL，界面上看到的是缩略图；同一张图在库里只存一份。

## 1. 为什么是内容寻址

消息 JSON 里**不存 base64**，存伪 URL `blue+attachment://<sha256>`，真实字节落在会话库的
`attachments` 表（主键就是内容哈希）：

- 同一张图在多个会话/多条消息里只存一份（去重由主键保证）；
- 消息 JSON 保持精简，历史回放不必搬着几 MB 的 base64 走；
- 会话被 `prune` / `reset` 删除时，映射行随消息级联消失，**附件行保留**（可能被别的会话引用），
  再由 GC 按「无引用 + 超过宽限期」回收；
- WebUI 的取图地址是内容寻址的，因此可以 `Cache-Control: immutable` 长缓存。

## 2. 数据模型（`sessions.db`，`PRAGMA user_version` 1 → 2）

```sql
CREATE TABLE attachments (
    hash TEXT PRIMARY KEY,      -- sha256(bytes)
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    data BLOB NOT NULL,         -- 原始字节
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL  -- GC 依据
);

CREATE TABLE message_attachments (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    hash TEXT NOT NULL REFERENCES attachments(hash),
    position INTEGER NOT NULL,  -- 在本条消息内的出现次序
    PRIMARY KEY (message_id, position)
);
```

迁移是「补表」而不是重写：所有建表语句都是 `IF NOT EXISTS`，老库打开后自动升级；
如果库版本高于本程序支持（例如回滚版本），启动直接失败而不是冒险读写。

## 3. 一次带图对话的链路

```text
浏览器：选择/粘贴/拖入 → canvas 降采样（长边 ≤1568）→ base64
   │  WebSocket {type:"prompt", text, attachments:[{mime,data,name}]}
   ▼
WebUI ── session.prompt(text, attachments) ──▶ agent
                                                  ├─ build_user_message(): 算哈希、生成伪 URL
                                                  ├─ session.append(messages, attachments=[blob])
                                                  │     └─ 同一事务：写 attachments（去重）+ 写消息 + 写映射
                                                  ├─ materialize(): 伪 URL → data URL（按哈希缓存）
                                                  └─ 模型请求（DeepSeek 只接受 data:/http(s)）
历史回放 ── serialize_message(): parts=[text, image(hash,url=/api/attachments/<hash>)]
   └─ 浏览器 <img src="/api/attachments/<hash>"> ── 命中 ──▶ session.attachment.get(hash)
```

## 4. 契约增量（`blue-contracts-session`）

| 名称 | 说明 |
| --- | --- |
| `SessionAttachment` | prompt 内联附件：`mime` / `data`（裸 base64 或 data URL）/ `name?` |
| `SessionAttachmentBlob` | 落库负载：`hash` / `mime` / `data`，与消息同批提交 |
| `SessionPromptRequest.attachments` | 与 `text` 至少一个非空（模型校验） |
| `SessionAppendRequest.attachments` | 消息引用的附件负载，服务端校验哈希与内容一致 |
| `session.attachment.get` / `.reply` | 按哈希取附件（base64 + mime + size） |
| `ATTACHMENT_URL_PREFIX` 等工具函数 | `attachment_url` / `parse_attachment_url` / `collect_attachment_hashes` / `normalize_base64` / `sha256_hex` |

## 5. 校验与预算

服务端（session）：

- 单轮附件数 ≤ `max_attachments_per_message`（默认 4）；
- 单张 ≤ `max_attachment_bytes`（默认 2 MiB）；
- base64 必须合法，且 `sha256` 必须与声明的哈希一致（防投毒）；
- 消息里引用了未落库的哈希：**只告警不关联**，消息照存（不因附件缺失丢正文）。

前端：

- 只接受 PNG / JPEG / GIF / WebP（DeepSeek 也按内容嗅探）；
- 长边 > 1568px 时用 canvas 降采样，PNG 保持 PNG、其它转 JPEG(0.9)；
- 一次最多 4 张，超限提示而不是静默丢。

## 6. 失败语义

| 场景 | 行为 |
| --- | --- |
| 附件被 GC / 不存在（materialize 取不到） | 该内容块**降级成文本占位**，绝不把伪 URL 发给模型（会 400） |
| 取附件 RPC 抛错 | 记 warning，同样降级 |
| 附件超限 / 哈希不符 | session 回 `success=false`，agent 报错，消息不入库 |
| `sanitize_history` 遇到多模态消息 | 算「有内容」（旧实现按 `isinstance(str)` 判空会把它丢掉，已修） |

## 7. 工具返回的图片

工具可以把自己的图片放进返回对象的 `attachments` 字段（`blue.runtime.tool_attachment`
负责 base64 编码），`ToolWorker` 会把它从正文里摘出来放进 `tool.result.attachments`，
agent 再转成内容寻址的会话附件、以**多模态 tool 消息**回灌模型：

```python
from blue.runtime import tool_attachment

class Screenshot(BaseModel):
    text: str
    attachments: list[dict[str, str | None]]

def screenshot(url: str) -> Screenshot:
    png = render(url)                      # 你自己产出的图片字节
    return Screenshot(text="截图如下", attachments=[tool_attachment(png)])
```

- 正文里不会出现 base64（否则模型的上下文会被 base64 撑爆）；
- `image/*` 走 `image_url` 内容块，其它走 `file` 内容块；
- `session.stream` 的 `tool_result` 帧带 `attachments: [hash]`，前端在工具卡片里直接
  用 `/api/attachments/<hash>` 渲染；
- **MCP 桥**：`image` 内容块不再降级成 `[image: …]` 占位，而是转成同样的结构化附件，
  因此「MCP 工具返回截图 → 模型真的看见」这条链路已经通了。

## 8. 已知边界

- 无图片编辑/裁剪，无多图拼合，无 OCR 专用通道（交给模型）；
- 音频/视频等非图片二进制仍降级为占位说明（模型侧不支持）；
- 未做 EXIF 方向纠正与色彩空间处理；
- 附件只有 GC，没有「用户手动删除某张图」的入口。
