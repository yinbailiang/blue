# 服务配置的单一真相源：从模板生成模型

本文是「服务配置」的设计定稿。目标是消灭 `.infd` 模板与 pydantic 模型**两份手写
定义之间的一致性漂移**，并让控制面在编译期就能校验业务配置。

- 实现：新建 `src/blue/codegen/`；改造 `services/*/service.infd`、`services/*/src/*/config.py`
- 相关：`control/desired.py`（编译与拆包）、`control/model.py`（`ServiceMeta`）

---

## 1. 问题：同一份契约，两份手写定义

每个服务的配置契约现在**手写两遍**：`service.infd` 里的 `~ServiceTemplate` 声明
字段与约束，`src/<pkg>/config.py` 里的 pydantic 模型声明字段与校验。两者靠人同步。

实测（2026-11-07）已经漂移了：

| 服务 | 模板字段数 | 模型字段数 | 模型有、模板没有（配不了） |
|---|---|---|---|
| `terminal` | 14 | 17 | `prompt` / `isolate_rc` / `cjk_font_path` |
| `session` | 11 | 12 | `outbox_claim_timeout` |
| `agent` | 24 | 24 | — |
| `bus` | 16 | 16 | — |

试着写进去，编译期直接拒绝：

```
ERROR service.prompt: 模板 'Base' 收到未知命名参数 'prompt'
```

也就是说这 4 个配置项**模型支持、但用户根本写不了**，只能吃默认值。这是
「配置项不可达」——与 `idempotency` 曾经静默失效是同一类问题。

同时，业务配置在控制面完全不校验：

```python
# control/model.py
class DesiredService(BaseModel):
    meta: ServiceMeta                 # 强校验
    config: dict[str, Any]            # ← 原样透传，不校验
```

`grep CONFIG_MODEL src/` 无命中，说明控制面从不读服务导出的配置模型。于是校验
散在三处，且**最该早报的那一处（编译期）缺席**：

| 阶段 | 校验什么 | 缺失时 |
|---|---|---|
| `.infd` 编译期 | 模板声明过的字段 | 没声明的字段直接报错（不可达） |
| 控制面（`load_desired`） | 无 | — |
| 服务进程启动 | `XxxConfig.model_validate` | 服务起不来才报 |

---

## 2. 方向：模板是真相源，模型是产物

两个候选方向：

- **A. 模型 → 模板**：pydantic 模型为真相源，生成 `.infd` 模板。
- **B. 模板 → 模型**：`.infd` 模板为真相源，生成 pydantic 模型（protobuf 式）。

**选 B。** 依据：

1. **模板表达力 ≥ pydantic。** `infd` 有 `regex()`、`range()`、`size()`、`in()`、
   `each()`、`positive()`，还有 pydantic `Field` 表达不了的跨字段约束 `when()` /
   `field()` / `position()` / `has()`。反向映射会**有损**（`pattern` 无处安放）。
2. **模板是规格，模型是实现。** 模板描述"用户能写什么"，是契约；模型是它的
   运行时投影。让实现反向定义契约，语义上倒置。
3. 实测确认模板 AST 完整可用（见 §4.1），反向生成没有技术障碍。

### 2.1 前提条件：`Config` 必须先纯化

B 方案要求**模型是纯数据的**——一旦模型上挂着运行时决策，它就不能被生成。

`terminal` 曾是反例：`resolve_shell()` 要读 `$SHELL`、`shutil.which()`、实际跑
`bash -c "bind -v"`——它探测的是**这台机器**，不是配置。这类方法已搬到模块级
纯函数（见 §6 阶段 0）。纯化后的判定标准：

> **字段级校验（拒绝非法值 / 规范化输入）留在 `Config`；
> 运行时决策（探测环境、解析路径、多源回退）一律搬出去。**

### 2.2 生成静态类，不需要 `.pyi`

最初设想是"动态生成模型 + `.pyi` 存根"。既然生成物**提交进 git**，直接生成
**静态 `.py` 类定义**即可，pyright 原生识别：

```python
class TerminalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cols: int = Field(default=100, ge=..., le=...)
```

比起动态方案省掉一个 `.pyi`，而且 traceback 指向真实源码、能直接断点。
`.pyi` 只在"源码动态、类型静态"时才必要——这里源码本身就是静态的。

---

## 3. 架构

```
services/<name>/service.infd
    ~ServiceTemplate { ... }            ← 真相源：字段 + 类型 + 约束 + 默认值
            │
            │  uv run blue codegen
            ▼
services/<name>/src/<pkg>/config.py     ← 生成的字段类（提交进 git）
            │
            │  手写（仅当需要 validator）
            ▼
services/<name>/src/<pkg>/config.py     ← 子类追加 validator，CONFIG_MODEL 指向它
            │
            │  手写
            ▼
services/<name>/src/<pkg>/__init__.py   ← 运行时逻辑（resolve_* 等纯函数）
```

三个要点：

- **系统字段不进模板生成**。`ServiceMeta`（`name` / `kind` / `package` /
  `depends_on` / `stop_timeout` / `ready_settle` / `smoke_*`…19 个）由控制面手写
  模型管，模板只声明**业务字段**。这与现状一致——`service.infd` 的
  `~ServiceTemplate` 本来只写业务字段。
- **生成物提交进 git**，否则构建链要内嵌生成器、且产物不可 review。
- **一致性由测试保证**：重新生成的结果必须与仓库里的文件逐字节相同。

---

## 4. 生成器

### 4.1 输入：模板 AST（已实测可用）

`infinity_data` 的 `CompilationResult.document.templates[*]` 暴露结构化 AST，
**不需要重新解析 `.infd` 文本**：

```python
tpl.fields: list[Field]
    f.name            # "default_width"
    f.constraints     # list[ConstraintIdent | ConstraintCall | ConstraintLiteral]
    f.default_value   # 字面量或 None
```

以 `browser_ops` 为例，实际拿到的约束结构：

| 字段 | `constraints` |
|---|---|
| `driver_bin` | `[Ident('str')]` |
| `browser_bin` | `[Call('any', [Ident('str'), Ident('?')])]` |
| `timeout` | `[Ident('float'), Ident('positive')]` |
| `default_width` | `[Ident('int'), Call('range', [320, 3840])]` |

### 4.2 约束映射表

| infd 约束 | pydantic |
|---|---|
| `int` / `float` / `str` / `bool` | `int` / `float` / `str` / `bool` |
| `path` | `Path` |
| `positive` | `gt=0` |
| `range(ge, le)` | `ge=…, le=…` |
| `range(ge)` | `ge=…` |
| `size(ge, le)` | `min_length=…, max_length=…`（str）/ `min_length=…`（list） |
| `regex("re")` | `pattern=r"re"` |
| `in("a", "b")` | `Literal["a", "b"]` |
| `list` + `each(T)` | `tuple[T, ...]` |
| `any(T)` | `T \| None` |
| `any(T, U)` | `T \| U` |
| `?` | 字段可缺省（`= None`） |

### 4.3 不生成到模型的部分

| 模板能力 | 处理 | 原因 |
|---|---|---|
| `when(...)` / `field(...)` / `position(...)` / `has(...)` | **不生成**，只留在模板层 | 跨字段约束在**编译期**就拦住了，比等到服务启动更早；pydantic 没有等价物 |
| 值变换（`normalise_color`） | 手写 validator 保留 | 声明式约束只能**拒收**，不能**改写** |
| `description` | 由模板注释 / 源行生成 docstring | 保持可读性 |

这三条是 B 方案的**结构性优势**：校验发生在最左侧（编译期），模型只负责
"类型对了没有"。

---

## 5. 加载流程

### 5.1 服务侧（不变）

`service_runtime_config.json` 的 `config` 段 → `XxxConfig.model_validate(...)`。
生成后字段与模板严格一致，这一层不需要改。

### 5.2 控制面（新增编译期校验）

现在 `DesiredService.config` 是 `dict[str, Any]`。改为**尽力校验**：

```python
# control/desired.py
model = _config_model_for(service)          # 懒加载服务的 CONFIG_MODEL
if model is not None:
    config = model.model_validate(raw_config).model_dump()
```

`_config_model_for` 用 `try: importlib.import_module(...) except ImportError: return None`
包住——控制面**不硬依赖** 服务包，独立发布 `blue` 时仍然可用（退化为不校验）。

收益：配置写错在 `blue apply` 的编译阶段就报，而不是等服务起不来。

---

## 6. 迁移路径

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 纯化 `terminal` 的 `Config`（3 个方法搬成模块级函数） | **已完成** |
| 1 | 纯化 `agent`（`resolve_api_key`）、`session`（`resolve_database_path`） | 待做 |
| 2 | 写生成器 + `agent` 单服务试点（24 字段，含 `Literal` / `tuple` / `int \| None` / 9 个 `compact_*`） | 待做 |
| 3 | 铺开到其余 7 个服务；加"重新生成 == 仓库内容"的一致性测试 | 待做 |
| 4 | 删除 24 个 `~ServiceTemplate` 的手写字段声明，改为 import 生成的模板 | 待做 |

阶段 3 的护栏测试是**防回归的关键**，无论后续是否继续用生成器都值得保留：

```python
def test_generated_config_is_up_to_date() -> None:
    """模板改了但没重新生成 → 红。"""
    for svc in services():
        assert regenerated(svc) == (service_dir / "config.py").read_text()
```

---

## 7. 未决问题

1. **validator 与生成类的共存形态**。需要 validator 的服务（目前只有 `terminal`
   的 `_validate_color` / `_validate_palette`）有两种写法：
   - (a) 生成 `TerminalConfigFields`，手写 `class TerminalConfig(TerminalConfigFields)`；
   - (b) 生成器支持"跳过指定类的生成"，该服务继续全手写。
   倾向 (a)：多一层薄继承，但仍是单一真相源。需确认无 validator 的服务（`clock` /
   `echo` 等）生成后可直接当 `CONFIG_MODEL` 用，不引入多余的类。

2. **`ServiceMeta` 是否也纳入**。它是控制面强校验的 19 个系统字段，用户**不直接
   写模型**（写 `service { }` 由模板 + 模型共同约束）。目前结论是保持手写、不生成。
   若要统一，需要先解决它的 `pattern` 与 `depends_*` 图关系（后者模板表达不了）。

3. **代码生成器放 `blue` 还是 `infinity_data`**。生成逻辑要知道"如何把 infd 约束
   翻译成 Python"，属于 blue 的服务约定；但 AST 来自 `infinity_data`。倾向放
   `src/blue/codegen/`，只依赖 `infinity_data` 的公开 API。

4. **`.infd` 模板的注释如何保留**。生成器从模板的 `source` 区间可以取到字段上方的
   注释，但跨行注释的归属判定需要规则。若取不到，退化为不生成注释——可读性会下降，
   这是 B 方案最主要的体验代价。
