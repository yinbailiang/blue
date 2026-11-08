# 世代（generations）：原子激活与回滚

本文是 blue 控制面的**世代机制**设计定稿。它把 `blue apply` 从"发信号 + 原地覆盖
配置文件"改造成 NixOS 式的三阶段流程：**构建 → 原子切换 → 健康验证**，失败自动
回滚到上一个稳定世代。

- 实现：`src/blue/control/artifacts.py`、`src/blue/control/generations.py`
- 相关：`desired.py`（编译与指纹）、`reconciler.py`（激活闸门与自动回滚）、
  `runner.py`（按产物启动）、`cli.py`（apply / up / rollback / generations / gc）

---

## 1. 为什么需要它

改造前三条硬事实决定了**回滚在物理上不可能**：

| 现状 | 后果 |
| --- | --- |
| `desired.py::write_runtime_configs` 原地覆盖 `services/<name>/service_runtime_config.json` | 旧配置被销毁，无处可回 |
| `.blue/state.json` 的 `generation` 每次被新版覆盖 | 系统不知道"上一份有效配置"是什么 |
| `cli.py::_apply --wait` 只等 `state.generation == desired.generation` | 调谐器读到了配置就算成功，服务随后崩进 `CrashLoopBackOff` 也返回 0 |

附带一个原子性漏洞：`write_runtime_configs` 是循环写入，中途 `OSError`（磁盘满）会
留下**一半新一半旧**的配置，而内存里保留的还是旧 desired——磁盘与内存不一致。

世代机制解决的正是这四点。

## 2. 三层分离（最重要的边界）

| 层 | 内容 | 是否随世代回滚 |
| --- | --- | --- |
| **代码**（artifact） | 服务包、blue SDK、契约包、第三方依赖 | ✅ 内容寻址，可回滚 |
| **配置**（generation） | 每个服务的 `service_runtime_config.json` | ✅ 世代内快照，可回滚 |
| **数据**（data plane） | `.blue/sessions.db`、`.rabbitmq/mnesia`、用户文件 | ❌ **永不回滚** |

数据面不进世代，等价于 NixOS 里 `/var` 与 `/nix/store` 的分界。回滚一个世代不会
丢任何会话、消息、附件、队列。

## 3. 目录布局

```text
.blue/
├── current -> generations/0007/          # 唯一的状态指针（符号链接）
├── generations/
│   ├── 0007/
│   │   ├── generation.json               # 元数据 + 每服务的 artifact 引用
│   │   ├── desired.json                  # 编译后的期望状态（密钥已脱敏）
│   │   ├── services/<name>/service_runtime_config.json   # 0600，含真实密钥
│   │   ├── .complete                     # 写完才落：半成品不允许激活
│   │   └── .stable                       # 激活验证通过才落
│   └── 0006/ ...
├── artifacts/
│   ├── _base/<lock_hash>/lib/python3.14/site-packages/   # 第三方依赖（一次/锁）
│   ├── <service>/<artifact_hash>/
│   │   ├── manifest.json                 # 组装成分：src_hash / sdk / 契约 / 锁 / python
│   │   ├── venv/                         # uv venv --relocatable + .pth → _base
│   │   └── .complete
├── wheels/<src_hash>/<wheel 文件名>      # uv build 的产物（按源码指纹去重）
└── state.json / supervisor.lock / logs/
```

`current` 是**唯一**表示"现在跑的是哪个世代"的地方。所有写操作都是"创建新目录"，
只有翻转 `current` 是就地变更，且用 `os.replace` 原子完成。

## 4. 产物（artifact）

### 4.1 指纹 = 产物闭包

```text
artifact_hash = sha256(
    service_src_hash        # pyproject.toml + service.infd + src/** 全部文件
  + sdk.src_hash            # src/blue/** + 根 pyproject.toml
  + 依赖契约的 src_hash      # meta.depends_contracts 里的那些
  + lock_hash               # 根 uv.lock 内容
  + python_version          # 例如 3.14
)
```

**必须包含后四项**：每个 artifact 的 venv 里装的不只是服务包，还有 blue SDK 与契约包。
只算服务目录的话，改了 SDK 或升级依赖后 artifact_hash 不变 → 复用旧 venv → 服务跑
旧 SDK，症状是总线上两个进程的契约版本不一致、字段静默丢失。

`service_src_hash` 由 `desired.py::digest_package` 计算，覆盖 `src/` 下**全部文件**
（含 `webui` 的 `web/**` 静态资源），排除 `__pycache__` / `*.pyc` / `.venv` /
`.pytest_cache` 等缓存。

### 4.2 组装方式

```text
1. uv build --package <pkg> --out-dir .blue/wheels/<src_hash>/      # 27 包共 0.14s
2. uv venv <artifact>/venv --relocatable --python 3.14              # 26ms
3. 写 <artifact>/venv/lib/python3.14/site-packages/zz_blue_base.pth
     → .blue/artifacts/_base/<lock_hash>/lib/python3.14/site-packages/
4. uv pip install --offline --no-deps --find-links <各 wheel 目录>
     blue <依赖的契约包...> <本服务包>                                # 74ms
5. 落 manifest.json + .complete
```

实测（本机 btrfs）：

| 步骤 | 耗时 | 占用 |
| --- | --- | --- |
| `uv build --all-packages`（27 包） | 0.14 s | 1.5 MB |
| `uv venv --relocatable` | 26 ms | 76 KB |
| 装 workspace 包（离线） | 74 ms | ~2 MB |
| base 硬链接克隆（一次/锁变更） | 0.7 s | 0（同盘硬链接） |

**为什么用 `.pth` 共享 base 而不是每 artifact 装全套依赖**：`uv pip install --offline`
无法为第三方依赖做解析（缓存里只有已装 wheel，没有 index 元数据），实测报
`websockets>=14.0 needs to be downloaded from a registry`。把第三方依赖固定成一份
base、按 `uv.lock` 指纹命名，既避开联网解析，又保证同一世代内所有服务依赖版本一致。

### 4.2.1 工作区外的 editable 依赖一律拒绝

base 是**开发用 `.venv` 的硬链接克隆**，所以 `.venv` 里装成 `editable` 的包是个陷阱：
`_skip_site_packages_entry` 会跳过所有 `*.pth`，editable 包在 base 里就只剩 dist-info
（模块消失 → 服务启动即 `ImportError`）；而保留指针又等于让产物跟着上游源码漂移，
`artifact_hash` 与实际内容脱钩。

因此克隆前先扫一遍：按 PEP 610 的 `<dist-info>/direct_url.json`（`dir_info.editable`）
与老式 `*.egg-link` 认定 editable 安装，**落在项目根之内**的放行（uv workspace 成员会
以 wheel 装进每个产物），**之外**的直接 `SupervisorError`，`blue apply` 在 build 阶段
就失败——不会留下"少一个模块"的 base。这也解释了 `uv sync` 为什么必须是
`uv sync --all-packages`：服务的依赖（pillow / pyte / uvicorn…）也要在 `.venv` 里，
否则 base 会缺它们。

### 4.3 硬链接跨分区要告警

`uv pip install` 的默认 link mode 与 base 克隆都依赖硬链接。**跨文件系统时 uv 会
静默退化为拷贝**（实测：同盘两个 venv 各装 pillow 合计 20 MB，跨盘 40 MB）。
构建前比较 `st_dev`，不同就告警一次，说明磁盘会成倍增长。

### 4.4 解释器

由 `flake.nix` 锁定 + uv 支持自动下载，所以**不做**解释器存在性预检：建 venv 用
`uv venv --python 3.14` 交给 uv 解析（必要时自动下载），manifest 只记录解析结果
（`sys.version`、`sys.executable`）用于诊断。

## 5. 世代（generation）

### 5.1 `generation.json`

```json
{
  "version": 1,
  "id": 7,
  "generation": "e8b64bb4…",          // DesiredState.generation 内容哈希（去重与展示）
  "parent_id": 6,
  "created_at": 1793881426.8,
  "created_by": "git 2184bc0 (dirty)",
  "reason": "apply",
  "supervisor": {"activation": {"timeout": 120.0}, "generation": {"keep": 5}, …},
  "sdk": {"package_version": "0.1.0", "source_hash": "…"},
  "contracts": {"tool": "…", "session": "…"},
  "deps_hash": "…",                    // uv.lock 指纹
  "services": {
    "agent": {
      "artifact_hash": "…",
      "meta": {"name": "agent", "kind": "agent", "package": "blue-service-agent", …},
      "config_hash": "…",
      "package_version": "0.5.0",
      "source_hash": "…"
    }
  }
}
```

`generation.json` 是**自足的**：调谐器只凭它 + 各服务的
`services/<name>/service_runtime_config.json` 就能重建完整期望状态，**不需要重新
编译 `blue.infd`**。于是：

- 回滚不需要旧版的 `blue.infd`；
- `blue.infd` 改坏了不影响正在运行的世代（只是下一次 apply 无法构建）；
- 世代与 Git 历史解耦，但 `created_by` 记下当时的 commit，便于人肉追溯。

`supervisor` 段是**刻意冻结**的：`activation.timeout`、`generation.auto_rollback`
这些参数决定"这一代怎么激活、怎么回滚"，属于期望状态的一部分。只放在工作区的
`blue.infd` 里会出事——`apply --config other.infd` 改了它们，调谐器却仍在用自己那个
`config_path` 编译出来的旧值（实测：把 `activation.timeout` 从 120s 改成 12s，
日志里仍旧是 120s）。唯一例外是 `state_dir`：它是运行期常量，改了只能重启 supervisor。

### 5.2 状态机

```text
        create            activate           全部 Ready
  (无) ────────► pending ──────────► pending ──────────────► stable
                    │                   │
                    │ 进程崩/超时        │ activation.timeout
                    └───────────────────┴──► failed ──► 自动回滚到上一个 stable
```

- `.complete`：目录写完（缺它 = 半成品，禁止激活）
- `.stable`：健康验证通过（缺它 = 未验证，重启用它做崩溃判定）
- `state.json` 里另有 `activation` 段，供 `blue apply --wait` 与 `blue status` 读取

### 5.3 幂等与去重

`DesiredState.generation` 是内容哈希，包含 config_hash / source_hash / sdk / 契约 /
`deps_hash`。**内容一致就不建新世代**：`blue apply` 直接报告"已是最新，无需变更"。
这与 Nix 的"同样输入 → 同样 store path"一致。

## 6. apply 三阶段

```text
blue apply [--wait] [--timeout S] [--staged] [--no-build] [--no-auto-rollback]

  ①build    编译 blue.infd → 计算 artifact 闭包 → 构建缺失的 artifact
            ── 全程不碰运行中的系统；失败 = 系统一个字节都没变

  ②flip     写 generations/<N>/（含配置、generation.json、.complete）
            然后 os.replace 原子翻转 .blue/current → generations/<N>/
            ── 这是唯一的状态变更点

  ③verify   通知 supervisor 重载 → 等全部服务 Ready（activation.timeout）
            失败 → 自动翻回上一个 stable 世代并重载，返回非 0
```

- `--staged`：只做 ①+② 的第一半（写世代目录，不翻转 `current`），
  之后用 `blue activate <N>` 激活。等价于 NixOS 的 `nixos-rebuild boot`。
- `--no-build`：复用已有 artifact（内容没变时本来就不会重建），只做 flip。
- `--wait`：等到 `state.activation.phase == stable` 或 `rolled_back`。

`blue up` 在没有 `current` 时自动补一次 apply（首次部署、或从旧版本升级）。

## 7. 健康闸门与自动回滚

**切换世代 ≠ 全量重启**。只有"这一项变了"的服务会被重启，判据是
`plan.py::restart_reasons`（调和器与 `blue plan` 共用）：

| 判据 | 触发条件 |
|---|---|
| 服务清单变化 | `service.infd` 的元数据（name/kind/package/module/command/depends_*/restart/…）变了 |
| 配置变化 | `config_hash` 变了 |
| 源码变化 | 服务包 `source_hash` 变了（`src/**` 全部文件） |
| 版本变化 | `pyproject.toml` 的 version 变了 |
| 依赖契约变化 | 自己 `depends_contracts` 里的契约指纹变了 |
| SDK 变化 | blue 核心（`src/blue/**`）变了 → **全部服务** |
| 依赖 SDK 变化 | 自己 `depends_sdk` 里的运行时包指纹变了 |
| **产物变化（兜底）** | `artifact_hash` 变了——`uv.lock`、解释器版本这类"六项都不变但产物变了"的情况 |

最后一行是必须的：`artifact_hash` 里含 `deps_hash` 与解释器版本，它们变化时上面
七项全都不变，但进程跑的是**旧产物**。少了这条兜底，新世代声明的 `artifact_hash`
与实际运行的永远对不上，依赖升级也不生效。老快照没有 `artifact_hash`（空串）时
跳过该判断，避免升级即全量重启。

实测（真机）：只改 `weather` 的 `timeout`（15.0 → 16.0）后 `apply --wait`——
18 个产物全部复用（未重建），日志只有一行 `将重启 weather：配置变化`，
其余 17 个服务的 PID 一个没动，全程 5.2 s。

因此激活期是**新旧世代进程共存**的：闸门只等"受影响的那几个"就绪，本来就 READY
的旧进程也算数。这与 NixOS `switch` 只重启受影响 unit 是同一个取舍。

调谐器（`reconciler.py`）负责验证，因为只有它看得到进程与就绪探针：

### 7.1 冒烟：验证「行为对」，不只是「起来了」

健康闸门只看就绪探针。一个能启动、探针通过、但工具全没注册成功的世代照样会被标成
`stable`——而自改进最容易造出的就是这种「半对不对」的错（本项目实测过：改前端资源
不生效、服务一直 Ready）。所以闸门通过后还会跑一遍**每个服务自己声明的**冒烟检查，
失败就按激活失败处理（标记 `failed` + 自动回滚到上一个 stable）。

声明写在服务自己的 `service.infd` 里，控制面只负责执行，**不含任何业务知识**：

```infd
service {
  name = "weather"
  smoke_tools = ["get_weather"]     # 这个工具必须出现在注册表里
}

service {
  name = "terminal"
  smoke_min_tools = 1               # 注册多个工具时：至少注册上 1 个
}

service {
  name = "session"
  smoke_command = ["python", "-m", "blue_service_session.smoke"]   # 自定义检查
}
```

| 声明 | 语义 |
| --- | --- |
| `smoke_tools` | 这些工具名必须出现在工具注册表里（最精确：服务自己最清楚） |
| `smoke_min_tools` | 本服务至少要注册几个工具（零维护，抓「一个都没注册」） |
| `smoke_command` | 检查命令，退出码 0 通过；首元素 `python` 换成**该服务产物的解释器**，cwd 是项目根，环境带本世代的 `BLUE_CONFIG_FILE` |
| `smoke_timeout` | 该命令的超时（默认 20s） |

没声明的服务直接跳过（显式优于默认拦截）。`supervisor.activation.smoke.enabled = false` 整体关闭，
`apply --no-smoke` 单次跳过。

为什么不在控制面里硬编码探针（比如"数工具个数""给 session 写读一条"）：那等于把业务
知识塞进控制面——加一个服务就要改控制面，且判据永远是二手的。现在 session 的写读检查
（往 `blue-smoke` 探针会话写一条再读回、最后 `session.reset` 删掉，零残留）写在 session
自己的包里，weather 只需一行 `smoke_tools`。

实测（真机）：正常世代 `冒烟通过：13/13 通过（smoke_command×1、smoke_min_tools×1、
smoke_tools×11，13 个服务）`；把 `weather` 的 `smoke_tools` 改成不存在的名字后
`12/13` 失败 → 自动回滚到上一世代、该世代标 `failed`、命令退出码 1。

1. 重载到新世代 → `state.activation = {phase: "pending", generation_id: N, deadline: now + activation.timeout}`
2. 每轮 `_tick` 检查：所有服务 `READY` → 落 `.stable`，`phase = "stable"`
3. 超过 deadline 仍未全绿 → 若 `auto_rollback` 开且存在上一个 stable：
   翻转 `current` 回该世代 → 重载 → `phase = "rolled_back"`（附失败服务与原因）
4. 关闭自动回滚时 `phase = "failed"`，等人处理

**崩溃兜底**：调谐器启动时若发现 `current` 指向的世代**没有 `.stable`**（说明上次
激活没走完），自动回退到最新的 stable 世代。等价于 NixOS 的"上次启动失败就选旧的
boot entry"，不需要人值守。

**例外：execv 自重启**。`apply` 改到控制面代码时会 `execv` 自己来换实现，PID 不变，
刚激活的世代自然还没验证。此时 `state.json` 里的 `supervisor_pid` 与当前进程一致，
调谐器据此判断为自重启，**继续验证该世代**而不是回滚；真正的崩溃重启（`blue up` /
进程管理器拉起）一定伴随新 PID，仍然按上面的兜底回退。

## 8. 回滚

```bash
blue rollback                 # 回到上一个 stable 世代（整机）
blue rollback --to 5          # 回到指定世代
blue rollback --service agent # 只回滚一个服务（默认取它最近一个不同产物的世代）
blue generations              # 列出全部世代与当前指针
```

- **整机回滚**：`current` 指向旧世代 + 重载。
- **单服务回滚**：创建一个**新世代** N+1，内容是"当前世代，但 `agent` 的
  artifact 引用与配置换成旧世代里的那份"。于是历史仍是线性的、可审计的，
  不会出现"指针在两个世代之间来回跳"的歧义。
- 回滚后仍要过健康闸门；再失败就继续往更早的 stable 世代退（不会无限循环：
  最多退到最老的 stable）。

## 9. 垃圾回收

```bash
blue gc                 # 保留 generation.keep（默认 5）个最新世代 + 回滚目标
blue gc --keep 3
blue gc --keep 1 --dry-run   # 先看要删什么
```

保留集合 = **最近 N 个** + **当前** + **最新两个 stable**：

- 为什么要两个 stable：自动回滚的目标是"上一个 stable"。只留一个的话，gc 之后
  就没有回滚目标了——等于把机制刚需的东西回收掉。
- artifact：只删除**没有任何保留世代引用**的；
- wheels：只删除没有任何保留 artifact 引用的。保留键从保留产物的 `manifest.json`
  里读（blue SDK / 各 sdk 包 / 各契约 / 服务自己各有一把）——**清单读不出来或结构
  认不出时只告警、整轮 wheel 回收跳过**，宁可留垃圾也不误删活 wheel；
- `_base`：只保留保留世代里出现过的 `deps_hash`（不是"只留当前那个"，否则存活
  的旧世代会因为找不到依赖基座而启动失败）；
- 数据面（`sessions.db`、`rabbitmq/`）永不清理。

两条安全阀：构建中的 `<指纹>.tmp-<pid>` / `<锁指纹>.tmp-<pid>` 目录一律跳过
（那是另一个进程正在写，删了就是把它的 build 打崩）；`service_dir.rmdir()` 这类
竞态失败只忽略、不中断整轮回收。

更重要的是**互斥**：`apply` 与 `gc` 共用 `state_dir/maintenance.lock`。构建中的
wheel 目录还没被任何产物引用（manifest 要等构建末尾才写），并发回收会把它们当
垃圾删掉；同理刚建好、还没被新世代引用上的产物也会中招。`gc` 拿不到锁就跳过
本次（退出码 1），`apply` 则最多等 5 分钟。

实测（本项目真机）：`blue gc --keep 1` 删掉 6 个世代 + 108 个产物 + 2 个基座，
`.blue/artifacts` 从 231 MB 降到 148 MB，系统仍在跑、无服务重启。

## 10. 配置项（`supervisor.inft` 模板）

`supervisor` 块的字段、约束与说明集中在项目根的 `supervisor.inft` 里，按职责分七组。
`blue.infd` 只写要覆盖的项，其余取模板默认；结构与 `SupervisorSettings` 模型**同构**
（配置里叫什么、模型上就叫什么，中间没有映射层）。

```infd
!from p"supervisor.inft" import Supervisor, Activation, Generation

supervisor Supervisor(
  activation = Activation(
    timeout = 120.0,          # 激活后等全部 Ready 的上限
  ),
  generation = Generation(
    keep = 5,                 # gc 保留多少个世代
    auto_rollback = true,     # 健康验证失败是否自动回滚
  ),
)
```

> 世代里冻结的是这份配置的**分组快照**，所以 `apply --config other.infd` 改到的
> `activation.timeout`、`generation.auto_rollback` 会随世代一起生效，不会出现
> 「配置改了、调谐器还用旧值」。

## 11. 与既有机制的关系

| 既有机制 | 变化 |
| --- | --- |
| `write_runtime_configs` | 写进世代目录；工作区副本不再写（唯一权威来源是世代） |
| `runner.build_command` | 有 artifact 时 `<artifact>/venv/bin/python -m <module>`；否则退回 `uv run` |
| 服务进程 cwd | 固定为**项目根**（原来是被启动服务的目录）——相对路径的语义与文件工具一致 |
| `BLUE_CONFIG_FILE` | 指向 `.blue/generations/<N>/services/<name>/service_runtime_config.json` |
| `SIGHUP` | 语义变成"按 `current` 指向的世代重载"，不再重新编译 `blue.infd` |
| `SIGUSR1`（execv 自重启） | 仍需保留：**控制面代码不进世代**（见 §13） |
| `blue apply --config X` | 真正切换的是**整份期望状态**（含 supervisor 设置、产物、配置）；世代里冻结的那份说了算 |
| 服务拉起失败 | `restart = "never"` 时直接标记 FAILED，不再按退避重试（曾经会重试 10 次刷日志） |
| `blue plan` | 对比 `blue.infd` 编译结果与 `current` 世代，输出不变 |

## 12. 测试策略

| 层 | 用例 |
| --- | --- |
| 单元 | `tests/test_artifacts.py`：闭包指纹对源码/SDK/运行时包/契约/锁/python 敏感；组装（venv + `.pth` + manifest + `.complete`）；增量复用；跨分区告警；GC 与 dry-run |
| 单元 | `tests/test_generations.py`：创建/去重/翻转原子性/`.stable` 语义/`to_desired` 重建/单服务回滚/控制面设置冻结/GC 保留规则 |
| 集成 | `tests/test_activation.py`：pending → stable；超时 → `rolled_back`（含"回滚后服务恢复就绪"）；`auto_rollback=False` → `failed`；启动时缺 `.stable` → 自动回退；`blue.infd` 编译失败不影响运行中的世代 |
| 集成 | `tests/test_reconciler.py`：拉起失败 + `restart="never"` 不重试 |
| CLI | `tests/test_cli.py`：`apply --staged` / `activate` / `rollback` / `rollback --service` / `generations` / `gc` / `apply --wait` 的失败回报 |
| 清单 | `tests/test_service_manifests.py`、`tests/test_sdk_fingerprint.py`：新增配置项与运行时包指纹 |
| 真机 | 见下 |

**真机验证记录**（本项目，2026-11-05）：

| 场景 | 结果 |
| --- | --- |
| 首次 `apply --staged` 构建 18 个产物 | 2.3 s（含共享 base 克隆）；`.blue/artifacts` 128 MB（19 份 venv 硬链接去重后 ≈ 一份依赖） |
| 产物解释器直跑服务 | `clock` 用 `<artifact>/venv/bin/python -m blue_service_clock` 启动、注册 `now`、优雅退出 |
| 注入启动即崩的 canary 服务 | 健康闸门超时 → **自动回滚**到上一个 stable 世代，`current` 回到旧世代，坏世代标 `failed`，18 个服务恢复 Ready |
| execv 自重启（改控制面代码） | 日志出现"检测到控制面 execv 自重启…继续验证，不回滚"，未误回滚 |
| `blue gc --keep 1` | 删 6 个世代 + 108 个产物 + 2 个基座，`.blue/artifacts` 231 MB → 148 MB，无服务重启 |

## 13. 已知边界（写清楚，别让人误以为是完整回滚）

1. **控制面代码不进世代**。`src/blue/**` 是 SDK，被每个 artifact 装进 venv（服务用它），
   但**调谐器自己**仍从工作区 venv 运行。因此"回滚带 SDK 变更的世代"意味着：
   服务回到旧 SDK，调谐器留在新 SDK。协议兼容由契约包版本保证；`blue status` 会
   显式提示 SDK 指纹与世代记录不一致。
   要彻底解决需要"控制面 shim + 世代化调谐器"，属后续阶段。
2. **非 Python 产物**（`mcp_servers/kwin_mcp/run.sh`、外部 node/二进制）不在 artifact
   里，世代只能记录它们的路径与目录指纹，回滚不保证可用。
3. **系统库**（Firefox、geckodriver、RabbitMQ）由 NixOS 提供，世代只记引用。
4. **数据面**永不回滚（见 §2）。
5. **`uv pip install --offline` 不能解析新的第三方依赖**：`uv.lock` 变了必须先在
   开发环境 `uv sync` 一次，让 `.venv` 与 base 同步；否则 artifact 构建会失败并
   提示这一点。
