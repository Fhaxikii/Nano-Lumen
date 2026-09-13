# 04 · 写一个技能

**这篇讲什么**：技能的协议、目录约定、完整写法，以及部署与调试方式。
**读完你能做什么**：新增一个可被模型调用的工具，并验证它能被正确加载和调用。
**前置**：`02-architecture.md`。

> 语言：中文 · [English](../en/04-writing-a-skill.md)

---

## 技能是什么

技能（Skill）是一个单独的 Python 文件，实现一个可被模型调用的工具。
把文件放进 `skills/` 目录即可被发现和加载，不需要修改框架代码。

这是向本项目添加能力最直接的方式，也是外部贡献风险最低的入口。

## 目录约定

| 目录 | 含义 |
|---|---|
| `skills/` | 普通技能。界面上可以禁用和删除 |
| `skills/official/` | 官方基础技能。界面上按钮存在但后端会拒绝禁用和删除 |
| `skills/disabled/` | 被用户禁用的技能 |
| `skills/deleted/` | 被用户删除的技能 |

"是否官方"完全由文件所在目录决定，代码中不存在对应字段。
加载时 `skills/official/` 最后扫描，同名时优先级最高。

## 模板

仓库提供四份模板，位于 [`skill_template/`](../../skill_template/)：

| 文件 | 适用 |
|---|---|
| `no_args_template.py` | 无参数的普通技能 |
| `with_args_template.py` | 带参数的普通技能 |
| `no_args_template_official.py` | 无参数的官方技能 |
| `with_args_template_official.py` | 带参数的官方技能 |

完整的协议说明写在 [`skill_template/no_args_template.py`](../../skill_template/no_args_template.py) 顶部，
以模板为准，不要照抄本篇的片段。

## 一个技能必须实现三个方法

以 [`skill_template/no_args_template_official.py`](../../skill_template/no_args_template_official.py) 为例：

### `get_manifest()`

返回给模型看的工具声明：名称、描述、参数结构。
描述决定模型在什么情况下会调用它，应当写清触发场景而不是实现细节。

### `get_spec()`

返回 `SkillSpec`，声明这个技能的性质。关键字段：

| 字段 | 含义 | 取值来源 |
|---|---|---|
| `purpose` | 一句话职责 | 自由文本 |
| `required_inputs` / `optional_inputs` | 输入定义 | `InputDef` |
| `data_output_keys` | 输出字典中会出现哪些键 | 自由文本 |
| `side_effects` | 副作用类型 | `core/schema.py` 的 `SideEffect` |
| `permission_level` | 所需权限档 | `core/schema.py` 的 `PermissionLevel` |
| `not_responsible_for` | 明确不做什么 | 自由文本 |
| `lifecycle` | 生命周期 | `core/schema.py` 的 `Lifecycle` |

`side_effects` 的可选值包括 `NONE`、`FILE_READ`、`FILE_WRITE`、`FILE_DELETE`、
`NETWORK`、`EXTERNAL_API`、`SHELL`、`SEND_MESSAGE`、`OS_CONTROL`。

`permission_level` 的可选值包括 `READONLY`、`WORKSPACE_WRITE`、
`NETWORK_ALLOWED`、`EXTERNAL_ACTION`、`DANGEROUS`。

这两个字段不是文档，它们参与运行时判断。声明得比实际宽松会绕过闸门，
声明得比实际严格会导致技能被拒绝执行。

### `run()`

异步方法，返回 `SkillResult`。三个字段：

- `success`：布尔值。
- `text`：给模型看的一句话结果。
- `data`：结构化输出，键应与 `data_output_keys` 一致。

失败时不要抛出异常，返回 `success=False` 并在 `text` 中说明原因。
异常会中断整轮对话，而返回失败结果模型可以据此调整。

## 文案的语言

技能里存在两类文本，读者不同，语言也不同：

- **模型读的**（`get_manifest()` 的描述、`SkillResult.text`）：英文。
- **用户读的**（`purpose`、`not_responsible_for`、界面上展示的名称）：
  写进技能的是什么语言，用户看到的就是什么语言——没有翻译机制。
  用你的用户读的语言书写；`skill_template/` 模板中的示例值是英文占位。

判断依据是"谁读这段文字"，不是"这段文字在哪个文件里"。

## 注释

技能源码可以被用户在界面上直接查看（工具面板中每个工具的「查看源码」）。
因此技能文件中只保留必要的接口说明，不要写开发过程记录、
调试笔记或历史问题说明。这类内容属于开发历史，不属于用户可见的源码。

参照 `skills/official/` 下现有文件的注释密度。

## 部署

普通技能可以通过界面部署。官方技能需要手工放入 `skills/official/` 并重启，
启动日志中会显示已加载的官方技能列表。

## 调试

1. 启动后查看日志，确认技能被加载。加载失败会记录具体原因。
2. 在界面左侧工具面板中确认该技能出现，状态为 `READY`。
3. 通过对话触发它，展开工具卡查看实际传入的参数和返回结果。

技能的发现与加载逻辑在 `core/registry.py`，协议校验在 `core/schema.py`。

---

## 怎么验证你改对了

1. 启动后日志中出现该技能，无加载错误。
2. 工具面板中该技能状态为 `READY`。
3. 触发一次调用，工具卡中参数与结果符合预期。
4. 制造一次失败（例如传入非法参数），确认返回的是失败结果而不是异常中断。
5. 运行 `bash run_tests.sh`。
