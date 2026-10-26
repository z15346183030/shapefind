# 灵感来源：[ast-grep/ast-grep](https://github.com/ast-grep/ast-grep)

- 星标：16,350
- 许可证：MIT
- 语言：Rust
- 它承诺解决：用「写代码」的方式搜索代码——按语法树结构匹配，而不是按文本匹配。

## 它做对了什么

1. **模式就是代码本身。** 不引入正则，不引入 DSL，用户写 `console.log($ARG)` 就表示「找一次 console.log 调用，把参数抓出来」。学习成本几乎为零，这是它最大的设计胜利。
2. **`$` 前缀的元变量语法足够小。** `$A` 一个节点、`$$$A` 一串节点，两个符号覆盖了绝大多数搜索需求，比正则的捕获组直觉得多。
3. **tree-sitter 让它一次性支持二十多种语言。** 语言只是一份语法定义，匹配引擎完全复用，所以新语言接入的成本被压得很低。
4. **search / lint / rewrite 三件事共用一套匹配核心。** `sg run` 用模式，`sg scan` 用 YAML 规则，但底层都是同一棵树上的同一套 matcher，没有两套语义。
5. **匹配与重写分离得很干净。** matcher 只负责「这一段像不像」，replacer 只负责「把捕获拼回去」，两者的边界清楚，测试也好写。

## 它在哪里让人痛苦

### 1. 一个叫 `strictness` 的旋钮，四个取值，没人说得清该用哪个

> "the default matching algorithm for patterns doesn't ignore comments, which is surprising for AST matching. If it cannot be changed now due to backward compatibility, it would be nice to have an option in config or CLI, so that we don't have to add `strictness` to every `pattern`."
>
> — [#2824 Configuration of the default strictness](https://github.com/ast-grep/ast-grep/issues/2824)

这段话信息量很大：默认行为**不忽略注释**，用户觉得「这对 AST 匹配来说很意外」，而修法不是改默认值，是**再加一个配置项**。于是同一个问题变成了「在每条 pattern 上重复写一遍 `strictness`」。

问题的根源在于：`strictness` 把「什么算同一段代码」这个语义决策推给了用户，而且是在每一个 pattern 上重复推。用户想要的其实是一句「注释不算代码」，而不是在 `cst / smart / ast / relaxed` 四个档位里做选择题。

### 2. 注释能影响匹配（同一个病的急性发作）

> "[bug] Comments influence matching of AST node"
>
> — [#2212](https://github.com/ast-grep/ast-grep/issues/2212)

标题本身就是控诉。一个宣称「按语法树匹配」的工具，被一段注释改变了匹配结果，说明它的匹配实际上是在 CST（带 trivia 的具体语法树）上做的，而不是 AST。CST 有它的好处（能原样重写），但代价是每个用户都得理解 trivia 是什么。

### 3. 模式片段不是合法程序时，要自己声明 context 和 selector

`return $X`、`except: pass`、`case $P:` 这类片段单独拿出来都不是合法程序。ast-grep 的解法是让用户写：

```yaml
rule:
  pattern: $A
  inside:
    kind: function_definition
```

或者用 `context` + `selector` 手工指定「先按这个上下文解析，再从里面挑这个节点」。这要求用户知道目标语言在 tree-sitter 里的节点名字——**用 Python 写模式的人，得先学会 tree-sitter 的 Python 语法节点表**。这是把实现细节泄漏成了用户接口。

### 4. 重写之后缩进会错

> "[bug] Python replacement indentation wrong in indented class"
>
> — [#1405](https://github.com/ast-grep/ast-grep/issues/1405)

> "[bug] Uneeded indentation is added during transform"
>
> — [#1935](https://github.com/ast-grep/ast-grep/issues/1935)

两个独立 issue，同一个症状：替换文本进到一个已经有缩进的位置时，续行的缩进不对。Python 里这不是排版问题，是语法问题——缩进错了代码就跑不起来。对一门用缩进表达结构的语言，「重写时缩进算错」等于「重写功能不可用」。

### 5. 名字被 semgrep 抢了

> "The semgrep program now includes a shortcut called 'sg' ... so it would be better to rename your 'sg' to 'ag'."
>
> — [#778](https://github.com/ast-grep/ast-grep/issues/778)

工具本身没问题，但命令名 `sg` 和 semgrep 撞车，装了两个工具的人得靠 `which sg` 猜今天调用的是谁。

### 6. 配置文件的体积说明了复杂度

仓库 `schemas/` 目录里，`rule.json`（规则语言本身的 schema）是 26,110 字节，单是 `python_rule.json` 就有 34,252 字节，`languages.json` 是 89,898 字节。一个「让搜索变简单」的工具，需要 26KB 的 schema 来描述它的规则语言——这已经是一门语言了，不是一组参数。

## 我们的取舍

**砍掉的：**

| 砍掉的东西 | 为什么 |
|---|---|
| YAML 规则语言（`rule` / `kind` / `has` / `inside` / `not` / `all` / `any`） | 90% 的搜索只需要一个模式。剩下的 10% 用 `--check` 加管道组合更直接 |
| `strictness` 四档旋钮 | 只保留一种语义：注释和空白永远不算代码。不提供选择，就不需要解释 |
| `context` + `selector` 手工声明 | 自动在 `def` / `class` / `while` / `try` 四种上下文里重试解析，用户写片段就行 |
| 二十多种语言 | 只做 Python，用标准库 `ast`，不引入 tree-sitter |
| 插件系统、LSP、napi/pyo3/wasm 绑定 | 一个命令行工具不需要成为平台 |
| `sg` / `ast-grep` 双命令名 | 只有一个入口 `shapefind.py` |

**保留的：**

| 保留的东西 | 为什么 |
|---|---|
| `$NAME` / `$$NAME` 元变量 | 这是原版最成功的设计，换掉只会更差 |
| 「模式就是代码」 | 用户不需要学新语法，这是整个工具存在的理由 |
| search / rewrite 双模式 | 找得到就得改得动，否则还要手工搬 |
| 捕获结果可编程消费 | `--json` 输出每个捕获的源码片段，能接管道 |

**加上的：**

| 加上的东西 | 解决什么 |
|---|---|
| 自动上下文重试 | `return $X`、`except: pass` 直接能写，不用查节点名 |
| 标识符位置的元变量 | `def $F`、`class $C`、`self.$ATTR` 都能捕获——Python 的 AST 把这些存成字符串而不是节点，不特殊处理就匹配不上 |
| 重写时的缩进对齐 | 多行捕获按落点列重新缩进，嵌套类里改写不再破坏语法 |
| `--check` | 一行命令当 CI 检查用，不需要写规则文件 |
| 零依赖单文件 | 复制一个 `.py` 就能用，不需要 cargo / npm / pip |

## 核心算法

匹配是**结构同构**判定，不是文本比较：

1. **模式预处理**：把 `$X` 换成合法标识符 `__sf_s_X`、`$$X` 换成 `__sf_m_X`。字符串字面量和注释里的 `$` 跳过不动（用一个小扫描器跟踪引号状态）。
2. **模式解析**：先按模块解析；失败则依次在 `def` / `async def` / `class` / `while True` / `try` 里重试。这就是「不用写 context」的全部秘密——猜四到五次比让用户查文档便宜。
3. **递归比对**：类型必须相同（`Call` 对 `Call`），然后逐字段比。列表字段用带回溯的序列匹配，因为 `$$X` 可以吃掉 0 到 N 个元素。
4. **捕获一致性**：同名元变量第二次出现时，比较结构指纹而不是文本，所以 `$X == $X` 认 `a == (a)`，不认 `a == b`。
5. **重写**：捕获的源码片段按落点列重新缩进，然后从后往前替换，保证前面的偏移量不失效。

一个具体例子，模式 `$L == $L` 扫这三行：

```python
a == a        # 匹配：两个捕获都是 Name('a')
b == c        # 不匹配：捕获值不同
x == (x)      # 匹配：括号在 AST 里根本不存在
```

## 与原版的差异

| 维度 | ast-grep | shapefind |
|---|---|---|
| 安装 | cargo / npm / pip / brew，二进制 10MB+ | 复制一个 811 行的 `.py` |
| 语言 | 20+，靠 tree-sitter | 1 种（Python），靠 `ast` |
| 注释是否影响匹配 | 默认影响，需 `strictness` 调 | 永远不影响 |
| 片段模式 | 需 `context` + `selector` 或 `inside` | 自动重试四种上下文 |
| 规则复用 | YAML 规则 + `sgconfig.yml` | 无规则文件，模式即规则 |
| 重写缩进 | #1405 / #1935 报过错 | 按落点列对齐，有回归测试 |
| 退出码检查 | `sg scan` + 规则 | `--check` |
| 配置文件 | `sgconfig.yml` + `rules/*.yml` | 0 个 |

## 实测

Python 3.13 标准库，920 个文件、386,762 行（排除 `site-packages`）：

| 查询 | 结果 |
|---|---|
| `grep -rn 'print('` | 970 行 |
| `shapefind 'print($X)'` | 246 处（恰好一个参数、无关键字参数） |
| `shapefind 'print($$ARGS)'` | 368 处（任意参数个数、无关键字参数） |
| 全量扫描耗时 | 3.7 秒 |

`print` 调用节点总数 611，其中 176 个带 `sep=` / `end=` 等关键字参数——所以 `print($$ARGS)` 是 368 而不是 611。这个差值不是漏匹配，是模式确实没那么说。

## 许可证说明

参考项目 ast-grep 采用 MIT 许可证。本项目**没有复用其任何代码**：语言不同（Rust / Python），匹配引擎不同（tree-sitter CST / 标准库 ast），规则系统不同（YAML / 无）。阅读范围限于 README、目录结构、issue 讨论和 schema 文件体积，用于理解其设计取舍。

本项目采用 MIT 许可证，见 [LICENSE](LICENSE)。
