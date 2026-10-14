# memory-pack · 最小可核验的保存与恢复（参考实现）

一份**纯标准库**的小程序：把「当前状态」保存成一份**唯一卡**＋可追溯的事件坐标，
让一个会话在**压缩 / 中断 / 换会话**之后，能**核验**它恢复出来的东西是不是真的
——而不是只知道"程序没报错"。

**它做到什么**

- 写入并回读：状态与卡各自绑 sha256；回执里的哈希必须等于磁盘上的实际字节
- 拒绝迟到候选：拿旧版本号来提交 ⇒ 报错，当前决定不动（不是静默忽略）
- 已接受的决定不可改写，只能追加一条 `supersedes` 新决定
- **收件 ≠ 接受**：收到原文只算"已收"；要写清去向与语义理由才算覆盖
- 原文可追溯：每个事件记 `offset / end / raw_sha256`，可回原生日志逐字节复算
- 投影可修：唯一卡被改/丢 ⇒ 检查报 `check_failed`（**不保留旧绿灯**），`repair` 重投影

**它不做什么**

- 不做模型调用（`organizer.py` 是机械抽取，`model_calls` 恒为 0）
- 不做向量 / 图谱 / 数据库 / 总线
- 不判断正文语义对不对 —— 那是人的判断，程序只证明"这一版确实存在、且是这一版"
- 不证明宿主的自动触发（手动 resume **不等于**自动恢复；见 §9）

> ⚠️ **平台状态照实说**：本包在 **Windows** 上实跑过（Python 3.10.9 与 3.12.14，cmd / PowerShell / git-bash 三种壳）。
> **macOS / Linux 尚未实测**（写这版时手上没有那两台机）。代码里唯一的平台分支是文件锁
> （`msvcrt` / `fcntl`），静态审查见 §11 —— **静态审查不等于跑过**。

---

## 0 先看你用哪个解释器

本包只用标准库，**Python 3.9+** 即可（`Path.is_relative_to` 需要 3.9）。
不需要安装、不需要 pip、不需要联网。

### Windows

| 你打算用 | 命令 | 说明 |
|---|---|---|
| ✅ 推荐 | `py -3` | Windows 官方启动器。本机实测 `py -3 -V` → **Python 3.10.9** |
| ✅ 也可以 | `python` | 本机 PATH 上的 `python` 实测 = **Python 3.10.9** |
| ✅ 指定全路径 | 指向 `python.exe` 的**完整路径**（自己那台的，不要照抄） | 装了多个版本时最稳 |
| ❌ **不要用** | `python3` | 本机 `python3` 是 **Microsoft Store 的占位程序**，实测打印 `Python was not found; run without arguments to install from the Microsoft Store…` —— **它根本不是解释器** |

先自检一句：`py -3 -V`

### macOS / Linux

| 你打算用 | 命令 | 说明 |
|---|---|---|
| ✅ 推荐 | `python3` | macOS 自带 / Homebrew 的都叫这个 |
| ✅ 也可以 | `python3.12` 等具体版本 | 想钉版本时用 |
| ⚠️ 看情况 | `python` | 有些系统上它指向 Python 2 或不存在，**先 `python -V` 确认 ≥3.9 再用** |

先自检一句：`python3 -V`

### 两条共同的规矩

1. **所有命令都带 `-B -X utf8`**：
   - `-B` ⇒ 不生成 `__pycache__`（本包"跑完目录干净"的判据靠它）
   - `-X utf8` ⇒ 强制 UTF-8 的 stdio 与默认编码（中文路径 / 中文输出都靠它）
   - 不想每次都打 `-B`，可设环境变量 `PYTHONDONTWRITEBYTECODE=1`
2. 下文 **Windows 用 `\`、macOS/Linux 用 `/`**，两条命令块都给了，照你那台抄。
   （Python 两边都认斜杠，这里只是让 cmd 的复制粘贴不出意外。）

### Windows 控制台 / 中文路径提示（照做能省掉三次踩坑）

1. **路径含中文**时（本机就是），cmd 里先 `chcp 65001` 切 UTF-8；PowerShell 与 git-bash 一般不用管。
2. 🔴 **不要用 `.bat` 装中文**：UTF-8 的 `.bat` 会被 cmd 按 GBK 解码、吃字，报一串
   `'D' 不是内部或外部命令`。要么 `.bat` 里只写 ASCII，要么把 `.bat` 存成 ANSI/GBK。
3. 🔴 **不要用 bash 的 `printf` 生成含路径的脚本**：`\f`、`\v` 会被 printf 变成控制字节（0x0C/0x0B），
   路径当场变脏、python 报 `[Errno 22] Invalid argument`。生成脚本请用编辑器/`write`，别用 printf。
4. 🔴 **不要把输出重定向到 `/tmp`**：没有该目录权限时重定向会失败、**python 根本没跑**，
   你看到的 `rc=1` 是 shell 自己的。把 `> 输出.json` 写进 `--root` 那个状态目录里。
5. 输出是 UTF-8。若终端仍显示乱码，那是**终端编码**的问题，不是程序输出坏了
   （可重定向到文件再看，或在 PowerShell 里 `chcp 65001`）。

---

## 1 复制到新目录，然后把状态目录放在包**外面**

```bat
:: Windows (cmd)
xcopy /E /I memory-pack demo-pack
cd demo-pack
```

```bash
# macOS / Linux
cp -r memory-pack demo-pack
cd demo-pack
```

> 这个包将来要放的仓库名（建议）是 `memory-receipts` —— **目前还没上传**。
> 等你从那个仓库检出时，目录名会是 `memory-receipts`：
> 下面命令里凡出现 `memory-pack` 的地方，换成你的实际目录名即可。

**为什么状态目录要放外面**：所有命令都用 `--root ..\demo-state`（macOS/Linux：`../demo-state`），
状态全落在包外。这样跑完你能亲眼确认——**你手里那份包一个字节都没变**（§9 自查第 3 条）。

下文所有命令都假设：**当前目录 = 你的包目录**（含 `memory.py`）。

---

## 2 造合成 fixture（可重建，不是任何真实会话）

```bat
py -3 -B -X utf8 fixture\make_fixture.py ..\demo-state
```
```bash
python3 -B -X utf8 fixture/make_fixture.py ../demo-state
```

- 生成 `demo-state/fixture-source.jsonl`（合成原生日志：`session_meta` ＋ 2 条公开消息
  ＋ 1 个工具调用对 ＋ **3 条必须被丢弃的记录**：`reasoning` / `developer` 消息 /
  `assistant` 的 `analysis` 通道，三条都带同一个哨兵串）
- 生成 `candidate-initial.json`（合法的第一个候选状态）与 `candidate-bad.json`
  （同样内容，但改写了已接受决定 —— 用来演示"被拒的发布不留半成品"）
- **确定性**：同样输入跑两次，输出逐字节相同；删掉 `demo-state` 可以完全重来
- ⚠️ fixture 里的角色名（`owner`）与允许表版本（`0.162.0`）是照 `memory.py` 的默认常量写的；
  若你按 §8 改了那些常量，这个 fixture 也要跟着改。

---

## 3 四步 —— 这就是「怎么算通过」

每一步：**命令 → 期望退出码 → 期望看到什么**。退出码不等于这里写的值，就是没过。
下面每个成对的命令块都给了 Windows 与 macOS/Linux 两份，照你那台抄。

### ① 写入

```bat
py -3 -B -X utf8 memory.py --root ..\demo-state register --path ..\demo-state\fixture-source.jsonl --session fixture-session-0001 --cwd ..\demo-state --from start
```
```bash
python3 -B -X utf8 memory.py --root ../demo-state register --path ../demo-state/fixture-source.jsonl --session fixture-session-0001 --cwd ../demo-state --from start
```
- **rc=0**；输出里 `"baseline": 0`、`"historical_scope": "included_from_start"`

```bat
py -3 -B -X utf8 memory.py --root ..\demo-state publish --input ..\demo-state\candidate-initial.json --expected 0 --actor bootstrap
```
```bash
python3 -B -X utf8 memory.py --root ../demo-state publish --input ../demo-state/candidate-initial.json --expected 0 --actor bootstrap
```
- **rc=0**，且 `"status": "saved_and_read_back"`、`"version": 1`
- 生成 `demo-state/outputs/memory-card.md`（唯一卡）与 `demo-state/runtime/receipts/<commit_id>.json`（回执）

**通过判据**：回执里的 `state_sha256` / `card_sha256` 必须等于磁盘上那两份文件的实际 sha256。
不用手算 —— 第 ④ 步的 `fixture/verify.py` 会独立复算（它的 A / B 两条就是这件事）。

### ② 中断 / 恢复

**(a) 恢复 = 收件（sync）＋ 检查（check）**

```bat
py -3 -B -X utf8 memory.py --root ..\demo-state resume > ..\demo-state\resume.json
py -3 -B -X utf8 memory.py --root ..\demo-state check
```
```bash
python3 -B -X utf8 memory.py --root ../demo-state resume > ../demo-state/resume.json
python3 -B -X utf8 memory.py --root ../demo-state check
```
- `resume` **rc=0**：`resume.json` 里应有 `"mode": "manual_recovery"`、`"sync": { ... "ok": true ... }`、
  且 `sync.sources[0].events` 应为 **4**（2 条公开消息 ＋ 1 对工具调用记录）
- `check` **rc=0**：`"status": "pending_increment"`、`"pending_public_count": 2`
- 源日志里那 **3 条带哨兵的私有记录没有进来**（收件里只有 2 条公开消息 ＋ 1 对工具调用记录，
  共 4 个事件；第 ④ 步的 C / D 条会逐个核）

**(b) 唯一卡被改/丢 ⇒ 不许留旧绿灯，且能修回来**

```bat
del ..\demo-state\outputs\memory-card.md
py -3 -B -X utf8 memory.py --root ..\demo-state check
py -3 -B -X utf8 memory.py --root ..\demo-state repair
py -3 -B -X utf8 memory.py --root ..\demo-state check
```
```bash
rm ../demo-state/outputs/memory-card.md
python3 -B -X utf8 memory.py --root ../demo-state check
python3 -B -X utf8 memory.py --root ../demo-state repair
python3 -B -X utf8 memory.py --root ../demo-state check
```
- 删卡后的 `check`：**rc=2**、`"status": "check_failed"`、`"error": "current card missing/changed; run repair"`
- `repair`：**rc=0**、`"status": "projection_repaired"`（版本不变）
- 再 `check`：**rc=0**，回到 `pending_increment`、`pending_public_count=2`
- `repair` 不是"随便重画一张卡"：它先核对 head 里记的 `card_sha256`，对不上就拒绝修

**(c) 被拒的发布不留半成品**

```bat
py -3 -B -X utf8 memory.py --root ..\demo-state publish --input ..\demo-state\candidate-bad.json --expected 1 --actor owner
```
```bash
python3 -B -X utf8 memory.py --root ../demo-state publish --input ../demo-state/candidate-bad.json --expected 1 --actor owner
```
- **rc=2**，stderr 里 `accepted decisions are immutable; append a superseding decision`
- 此时 `head.json` 与 `memory-card.md` 的 sha256 与上一步**完全相同**
  （Windows：`certutil -hashfile <文件> SHA256`；macOS/Linux：`sha256sum <文件>`）

### ③ 迟到候选被拒

```bat
py -3 -B -X utf8 memory.py --root ..\demo-state publish --input ..\demo-state\candidate-initial.json --expected 0 --actor owner
```
```bash
python3 -B -X utf8 memory.py --root ../demo-state publish --input ../demo-state/candidate-initial.json --expected 0 --actor owner
```
- **rc=2**，stderr 里 `stale version; candidate kept, current decision unchanged`
- 候选**没有被吞**（文件还在），当前决定**没有被改**（version 仍是 1）

### ④ 原文追溯

```bat
py -3 -B -X utf8 memory.py --root ..\demo-state pending > ..\demo-state\pending.json
py -3 -B -X utf8 fixture\verify.py ..\demo-state
```
```bash
python3 -B -X utf8 memory.py --root ../demo-state pending > ../demo-state/pending.json
python3 -B -X utf8 fixture/verify.py ../demo-state
```
- `pending` **rc=0**；`verify.py` **rc=0**，最后一行 `VERIFY OK: ...`
- `verify.py` **不 import `memory.py`**，自己重读磁盘、自己重算哈希：
  - `A` 回执 ↔ version 文件 / 唯一卡 的实际字节
  - `B` `head.json` ↔ 回执（版本、提交号、两个哈希）
  - `C` 收件里每个事件：`sha256(源文件[offset:end])` == 记录的 `raw_sha256`（逐个 MATCH）
  - `D` 哨兵串**没有**出现在收件里（＝ `reasoning` / `developer` / `analysis` 确实被丢弃）

### 3.1 四个参数到底什么意思

| 参数 | 含义 | 实测行为 |
|---|---|---|
| `--expected N` | **乐观并发控制**：你"认为当前是第 N 版"。不是"要写成第 N 版" | 不等于当前版本 ⇒ **rc=2** `stale version; candidate kept, current decision unchanged`；候选文件保留、当前状态不动（实测） |
| `--actor {owner,bootstrap,worker}` | 谁在发。`owner`＝唯一能发布当前状态的角色；`bootstrap`＝只允许建第 1 版的一次性角色；`worker`＝受限实施者，只能改自己名下的 `tasks`/`current` | 首发用 `bootstrap` ⇒ rc=0（实测）；**`owner` 在第 0 版也能首发**（实测 rc=0），之后再用 `bootstrap` ⇒ rc=2 `only owner publishes current state; bootstrap allowed once`（实测）；`worker` 想改 owner 的字段 ⇒ rc=2 `worker cannot change owner fields`（实测） |
| `--from {start,end}` | **登记基线**（只此一次，不能重置）：`start`＝从头纳入历史；`end`＝以当前文件末尾为基线，之前的历史**不读** | `start` ⇒ `"baseline": 0`、`historical_scope": "included_from_start"`（实测）；`end` ⇒ `"baseline": <文件当前字节数>`、`"historical_scope": "excluded_before_baseline"`（实测）；重复登记同一 session ⇒ rc=2 `source already registered; baseline cannot be reset`（实测） |
| `--cwd DIR` | 登记时该来源的**项目工作目录**：必须与源文件首行 `session_meta` 里的 `cwd` 指向同一处（两边都规范化成绝对路径再比） | 一致 ⇒ rc=0（实测）；不一致 ⇒ rc=2 `source workspace mismatch`（**按代码推得，未构造**）。它同时是钩子白名单：`hook` 只在事件 `cwd` 命中登记的 `cwd`（或 `cwd` 的同义映射）时才附着（实测：换一个未登记 session/cwd 回 `{}`） |

---

## 4 跑测试（35 项）

```bat
py -3 -B -X utf8 test_memory.py
py -3 -B -X utf8 test_automation.py
```
```bash
python3 -B -X utf8 test_memory.py
python3 -B -X utf8 test_automation.py
```
- 期望：`Ran 15 tests ... OK` 与 `Ran 20 tests ... OK`，两个 **rc=0**

### fixture 到底建在哪（这条以前看不见，现在会自己打印）

测试的临时 fixture **绝不落进包内**。测试优先用系统临时目录；
🔴 **如果系统临时目录不可写（沙箱里常见），Python 的 `tempfile` 会静默退到"当前目录"**
——若你的当前目录就是包目录，它本来就会落在包里。所以本包的 `scratch_root()` 会**再判一次**：
候选落点在包内就改用**包的父目录**。

现在跑测试会先打印它选了哪：

```
fixture scratch dir = /some/path/outside/the/package
tempfile.gettempdir() = <系统临时目录，或者退化的当前目录>
```

**核对方法**：第一行那个路径必须**不在**包目录里面。
想单独看这条判断走了哪个分支：

```bash
python3 -B -X utf8 -c "import tempfile;print(tempfile.gettempdir())"
```

- 它 = 一个正常的临时目录 ⇒ 走"系统临时目录"分支
- 它 = 你的当前目录（＝包目录） ⇒ 走"退回包的父目录"分支（第二行打印会露出来）
- 两种情况下**包目录跑完都逐字节不变**（`tearDown` 里有一条断言专门守这件事）

---

## 5 全部 12 个子命令（`--help` 实测）

`memory.py --help` 实测列出的就是这 12 条。
用法：`<PYTHON> -B -X utf8 memory.py --root <状态目录> <子命令> [参数]`。
下表退出码凡标"实测"的，都是**本机 Windows 上真跑出来的**（Python 3.10.9 与 3.12.14）。

| 子命令 | 干什么 | 退出码 | 状态 |
|---|---|---|---|
| `register` | 登记一个来源（原生日志路径＋session id＋cwd＋基线）；**一次性**，基线不能重置 | 0；重复登记 2（实测） | ✅ §3① 走过 |
| `publish` | 用候选状态发新版本：写不可变版本文件 → 更新 head → 重画唯一卡 → 写回执 | 0；`--expected` 不符 2；改写已接受决定 2；`--input` 不存在 2（全实测） | ✅ §3①③②(c) 走过 |
| `sync` | **只收件**：按游标把新增公开记录收进 `runtime/inbox/`。不动状态、不做语义判断 | 0（实测：输出 `"ok": true`、`events`、`offset`）；**失败路径未构造** —— 按代码任一来源失败 ⇒ `"ok": false` ⇒ rc=2 | ✅ 实测（`resume` 内部也调它） |
| `check` | **只读检查**：head↔卡是否一致、有无未覆盖公开项、已接受任务的证据版本是否还在；不保留旧绿灯 | 0（`pending_increment`，实测）；`check_failed` ⇒ 2（实测）；`covered` **未构造** | ✅ §3② 走过 |
| `resume` | 恢复入口 = `sync` ＋ `check` ＋ 打印当前状态与未处理公开原文 | 0（实测）；内部 check 失败 ⇒ 2（**按代码推得，未构造**） | ✅ §3②(a) 走过 |
| `repair` | 按 head 里的 `card_sha256` **重投影**唯一卡（不能改状态） | 0（`projection_repaired`，实测）；head 与卡定义不符 ⇒ 2（**按代码推得，未构造**） | ✅ §3②(b) 走过 |
| `pending` | 列出"已收到但还没写覆盖理由"的公开事件（含原文坐标） | 0（实测） | ✅ §3④ 走过 |
| `export` | 打印当前状态 JSON（就是 head 指向的那一版） | 0（实测） | ✅ 实测 |
| `view` | 打印**短视图**文本（给宿主注入用）：版本、恢复入口、收件计数、当前球 | 0（实测） | ✅ 实测 |
| `hook` | 从 **stdin** 读宿主事件 JSON，按 session/cwd 白名单决定是否回 `additionalContext` | 0；未登记会话回 `{}`（也是 0）（均实测） | ⚠️ **用合成事件实测过**（已登记会话回 `hookSpecificOutput`、上下文 741 字符；未登记 session 回 `{}`）。**"宿主真的触发、模型真的采用"没验** —— 程序自己也只标到 `handler_ran_host_trigger_unverified` |
| `organize` | **内部工人**：把增量机械抽成 `unreviewed` 草稿放进复核队列。**绝不发布状态** | 0（`consumed_into_review_queue`；再跑一次 `no_increment`）（均实测） | ⚠️ 实测过；"草稿对不对"是**人的判断**，程序只保证"没发布、没模型调用（`model_calls: 0`）" |
| `organization-status` | 看草稿队列状态（消费了几个事件、几个 job） | 0（实测） | ⚠️ 同上（内部件） |

**失败时的通用形状**（三条都实测）：参数写错（如 `--actor nobody`）⇒ argparse 用法 + **rc=2**；
`publish` 的 `--input` 不存在 ⇒ stderr 一行 JSON（`"status": "failed"`、`"success_receipt": false`）+ **rc=2**；
`check` 在从没登记过的空状态目录上 ⇒ **rc=2** `check_failed`，但错误话术是
"卡缺失/被改，跑 repair"——对空目录而言不准确，算一条已知粗糙处（§9.8）。

---

## 6 `hook_entry.py` 怎么跑（`--code-sha256` 从哪来）

**它是什么**：`memory.py hook` 外面再包一层**代码版本钉**。宿主钩子定义里写死一个摘要；
每次触发先算一遍现有三份源码的摘要，不一致就**只停用"自动面"**、不阻塞会话。

**用法**（`hook_entry.py --help` 实测：`--root ROOT --code-sha256 CODE_SHA256`，两个都必填；
不带参数实测 rc=2 并打印用法）：

```bat
py -3 -B -X utf8 tools\code_sha256.py
py -3 -B -X utf8 hook_entry.py --root ..\demo-state --code-sha256 <上一步打印的值> < event.json
```
```bash
python3 -B -X utf8 tools/code_sha256.py
python3 -B -X utf8 hook_entry.py --root ../demo-state --code-sha256 <上一步打印的值> < event.json
```

- **这个值是什么**：`sha256(memory.py 的字节 ＋ organizer.py 的字节 ＋ hook_entry.py 的字节)`，
  就按这个顺序拼接 —— 与 `hook_entry.py` 自己算的方式完全一样，`tools/code_sha256.py` 直接打印它
- 事件 JSON 从 **stdin** 读，形状与 `memory.py hook` 相同，例如：
  `{"session_id": "fixture-session-0001", "cwd": "<状态目录>", "hook_event_name": "SessionStart", "source": "startup"}`
- **实测三种结果**：

  | 情况 | 输出 | rc |
  |---|---|---|
  | 摘要对 ＋ 已登记会话 | `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "项目记忆恢复：…"}}` | 0 |
  | 摘要**不**对 | `{"systemMessage": "项目记忆代码已变更；需重新审查钩子定义，自动面停用。", "continue": true}` | 0 |
  | 少给参数 | 用法提示 + `error: the following arguments are required: --root, --code-sha256` | 2 |

- **本包当前摘要**：`d33b589d2623f72f619708ed1924f554da12a39db9c2d524e8f01582ab2591de`
  （你只要改了 `memory.py` / `organizer.py` / `hook_entry.py` 里任何一个字节，这个值就变，
  自动面会自己停用 —— 这是设计不是故障。改完重跑 `tools/code_sha256.py` 写进钩子定义即可）
- **和两个 `hooks.example.*.json` 的关系**：示例里挂的是 `memory.py hook`（**没有**版本钉）。
  想要版本钉，把那条 `command` 换成
  `<PYTHON> -X utf8 <PACKAGE_DIR>/hook_entry.py --root <PROJECT_ROOT> --code-sha256 <值>`。

---

## 7 目录与状态

| 路径 | 是什么 |
|---|---|
| `memory.py` | 全部逻辑（状态/版本、覆盖、发布、唯一卡、回执、追溯） |
| `organizer.py` | 机械抽取草稿队列；**只产 `unreviewed` 草稿，绝不发布状态** |
| `hook_entry.py` | 钩子入口（带代码摘要钉，见 §6） |
| `test_memory.py` / `test_automation.py` | 反例测试（35 项） |
| `hooks.example.windows.json` | Windows 宿主钩子**模板**（占位符必须替换） |
| `hooks.example.posix.json` | macOS/Linux 宿主钩子**模板**（同上；**未实测**） |
| `fixture/make_fixture.py` | 造合成 fixture |
| `fixture/verify.py` | 独立核验器（A/B/C/D 四条） |
| `tools/code_sha256.py` | 打印 `hook_entry.py` 要的代码摘要 |
| `LICENSE` | MIT（署名见 §12） |
| `给试用者.txt` / `给Mac试用者-自测清单.txt` | 给人看的一页说明 |

运行期状态（都是**活件**，已被 `.gitignore` 排除，别进版本库、别随包分发）：

| 路径（相对 `--root`） | 是什么 |
|---|---|
| `runtime/head.json` | 指向当前版本文件 + 它的 sha256 |
| `runtime/versions/<v>-<commit>.json` | 不可变的状态版本 |
| `runtime/receipts/<commit>.json` | 保存回执（`saved_and_read_back`） |
| `runtime/cursors/<session>.json` | 逐来源游标（`offset` + `prefix_sha256`） |
| `runtime/inbox/<session>-<start>-<end>.json` | 收到的公开事件（含原文坐标） |
| `runtime/organization/` | 草稿队列（jobs / drafts / review / index） |
| `runtime/sources.json` | 已登记来源（**含本机绝对路径**，别外发） |
| `outputs/memory-card.md` | 唯一卡（由 `publish` 生成，**不要手改**） |

`--root` 指到哪，状态就落在哪。接真实项目时：

```bash
python3 -B -X utf8 /path/to/memory-pack/memory.py --root /path/to/your-project register ...
python3 -B -X utf8 /path/to/memory-pack/memory.py --root /path/to/your-project resume
```

---

## 8 接你自己的宿主：要改的点

| 改什么 | 常量/位置 | 说明 |
|---|---|---|
| 原生日志格式 | `ADAPTER`、`register()`、`sync()` | 允许表钉死 `codex-rollout-0.162-response-item-v1`；换宿主必须改这里 |
| 什么算"公开输入" | `Memory.public()` | 现规则：`user`/`assistant` 的文本（`channel`/`phase` 限 `final`/`commentary`）＋工具调用/结果；`reasoning`、`developer`、`system`、`analysis` 一律丢弃 |
| 谁能发当前状态 | `PUBLISHER` / `WORKER` / `BOOTSTRAP` | 默认 `owner` / `worker` / `bootstrap` |
| 状态放哪、卡叫什么 | `BASE_DIR` / `CARD_FILE` | 默认 `runtime` / `outputs/memory-card.md` |
| 默认根目录 | `DEFAULT_ROOT` | 默认＝包目录自己；真实使用建议显式给 `--root` |
| 钩子命令 | `hooks.example.windows.json` / `hooks.example.posix.json` | 把 `<PYTHON>`、`<PACKAGE_DIR>`、`<PROJECT_ROOT>` 换成你自己的路径 |

侧信道：`hook()` 只认**已登记**会话，且 `cwd` 必须命中该来源的 `cwd`（或 `hook_cwds`）——
它**永远不会**附着到别的项目上。

---

## 9 已知坑 · 接宿主前先自查

1. **手动 resume 不证明自动恢复。** `check()` 的 `automation` 只会有
   `unconfirmed` 或 `handler_ran_host_trigger_unverified`：钩子"跑过"与"宿主真的触发、模型真的采用"
   是两件事，后者必须由你另行取证。
2. **收件 ≠ 接受。** `sync` 成功只说"已收到"。覆盖要在 `coverage[]` 里给 `destination` ＋ 语义理由。
3. **不完整的行不提交。** 源文件最后一行没有换行符 ⇒ 那行留给下一次；
   源文件被截断/替换 ⇒ 游标**不动**并报 `check_failed`。
4. **卡不是状态。** 卡是投影；`head.json` 才是锚。改卡 ⇒ `check_failed`；`repair` 只能重投影，不能改状态。
5. **`hooks.example.*.json` 只是示例。** 宿主是否信任这个钩子、生命周期是否真的触发，**必须你自己证**。
6. **状态目录含本机绝对路径**（`runtime/sources.json`、事件里的 `path`）。要外发前先脱敏。
7. **`check` 不负责收件。** 收件是 `sync`（`resume` / `hook` 会调它）。
   只跑 `check` 的目录其 `runtime/inbox/` 会是空的 —— 这不是错误，是"还没收"。
8. **空状态目录上 `check` 的报错话术不准确**（会说"卡缺失/被改，跑 repair"，其实是"从没登记过"）。

**自查三问（能答"是"才算过）**

- 我在**一个新的空目录**里，只照这份 README，四步都过了吗？（rc 都对上了吗？）
- 我有没有依赖任何作者的会话、私有文件或活件？（应当没有）
- 跑完，我拿到的那份包目录**变了吗**？（应当没变 —— 状态目录是放在包外的）

---

## 10 环境不满足时会怎样

| 情况 | 你会看到 |
|---|---|
| Python < 3.9 | `Path.is_relative_to` 不存在 ⇒ 测试 `tearDown` 与涉及证据路径的代码会抛 `AttributeError` |
| 用了 Microsoft Store 的 `python3` 占位 | 打印 `Python was not found; run without arguments to install from the Microsoft Store…`，**rc 非 0**，什么都不会发生（不是本包的错） |
| 系统临时目录不可写（沙箱） | 测试改用包的父目录，并**打印**它选了哪（§4） |
| 包目录只读 | `publish` / `repair` 写文件时抛 `PermissionError`；`check` 只需读，仍能跑 |
| 源日志被截断/替换 | `sync` 报 `source truncated/replaced; cursor unchanged`，`check` 变 `check_failed`（**不动游标**） |
| 源日志版本不在允许表内 | `register` 报 `unverified rollout version; adapt and test before registering` |
| 源文件最后一行不完整 | 那行**不提交**，留给下一次 `sync` |

---

## 11 平台分支静态审查（逐条 file:line）

🔴 **这是"读代码 + 静态扫描"的结果，不是跑过的结果。macOS / Linux 未实测。**

| 类别 | 位置 | 事实 |
|---|---|---|
| **唯一的真平台分支**（文件锁） | `memory.py:69-83` | `if os.name == "nt":` ⇒ `msvcrt.locking(f.fileno(), LK_NBLCK/LK_UNLCK, 1)`（`memory.py:70,71,80`）；`else:` ⇒ `import fcntl` ＋ `fcntl.flock(f, LOCK_EX\|LOCK_NB)` / `LOCK_UN`（`memory.py:73,74,82`）。**POSIX 那一支在本机（`os.name == 'nt'`）跑不到 ⇒ 未实测** |
| 原子替换（跨平台） | `memory.py:55` | `os.replace(tmp, path)` —— 三平台都是原子重命名 |
| 落盘路径写法 | `memory.py:144,374,388,403,447,488,498,501`；`organizer.py:83,86`；`fixture/make_fixture.py:33,79` | 一律 `as_posix()` ⇒ 状态文件里存**正斜杠**，跨平台可读 |
| 路径拼接 | `memory.py:99-106,245` 等 | 全用 `pathlib` 的 `/` 与 `relative_to`；**没有** `os.path.join`、**没有**把手写 `\` 或 `/` 当分隔符塞进字符串 |
| `os.path` 用量 | `memory.py:54,55,69,79` | 只用了 `os.fsync` / `os.replace` / `os.name`；**全文没有 `os.path.*` 调用** |
| 换行：判完整行 | `memory.py:141,200` | `endswith(b"\n")` ⇒ LF 与 CRLF 都算完整（CRLF 以 `\n` 结尾） |
| 换行：切行 | `memory.py:199-200` | `splitlines(keepends=True)` + `endswith(b"\n")` ⇒ CRLF 行同样通过 |
| 换行：取首行 | `memory.py:193` | `data.split(b"\n",1)[0]`；CRLF 时行尾带 `\r`，`json.loads` 容忍尾随空白（**未在 CRLF 源文件上实测**） |
| 换行：**写出** | `memory.py:39,335` | 状态与卡都用 `"...".join(...).encode("utf-8")` **二进制写出** ⇒ 产物永远是 LF，不受平台影响。（关键：用 `write_text` 会被 Windows 翻译成 CRLF —— 本包重建时**实测踩过**，见 §13 备注） |
| 换行：fixture | `fixture/make_fixture.py:53,74` | `open(..., newline="\n")` 显式 LF ⇒ 三平台字节一致 |
| 换行：测试内部 | `test_memory.py:44,55,56,144` 等 | 测试用 `write_text()`（默认 `newline=None`）⇒ **Windows 上造 CRLF、POSIX 上造 LF**；两种都被上面的"判完整行"接受。属测试内部差异，不影响包产物 |
| 编码 | `memory.py:43`、`fixture/verify.py:34` | 读 JSON 用 `utf-8-sig`（容忍 BOM） |
| 编码：写出 JSON | `memory.py:39`、`fixture/make_fixture.py:55,75` | 统一 UTF-8（`ensure_ascii=False`） |
| **打印的恢复命令** | `memory.py:96` | `resume_cmd()` 用 **`sys.executable`**（当前正在跑的解释器）拼命令 ⇒ Windows 上是 `…python.exe`、mac/Linux 上是 `…python3`，**不用你猜** |
| Python 版本门槛 | `memory.py:245`、`test_memory.py:25,50` | `Path.is_relative_to` ⇒ 需 **3.9+** |

> 这一节的存在理由：**"没跑过"不等于"能跑"。** 上面每一条都只是"代码看起来是这样"，
> 只有 Windows 那一支被真跑过。Mac 朋友跑通四步 = `fcntl` 那一支第一次被真跑过（见 `给Mac试用者-自测清单.txt`）。

---

## 12 许可

**MIT** —— 见 `LICENSE`（标准 MIT 全文）。

版权署名：**记性好的大肥鱼姐姐**（`LICENSE` 的版权行就是这个名字；**没有占位字样**）。

🔴 **将来要放的仓库名（建议，尚未上传、还没有公开地址）＝ `memory-receipts`**。
⚠️ 注意：zip 里的目录名是 `memory-pack`，**与仓库名不必一致**；
从仓库检出后，把本文档命令里出现的 `memory-pack` 换成你的检出目录名即可。

## 13 第三方归属

- 本包**不含**第三方库或第三方代码：`memory.py` / `organizer.py` / `hook_entry.py` 只用 Python 标准库；
  测试只用 `unittest` 与 `unittest.mock`；`fixture/*.py` 与 `tools/code_sha256.py` 同样只用标准库。
- 这三份核心文件里**没有**版权声明、作者署名或来源说明。在原始工程树里按名检索
  `readme/license/licence/copying/notice` **均未命中** ⇒ **没读到**任何归属或来源信息。
  `organizer.py` 与 `memory.py` 同目录、复用 `memory.py` 的原子写与哈希工具，
  **没读到**任何"来自第三方"的标注。
- ⇒ 归属只能由**权利人**补写；在补写之前，不要对外声称"来源已核"。

> 备注（§11 提到的实测教训）：本包在 Windows 上重建时，`Path.write_text` 曾把 3 个源文件的
> LF **静默改成 CRLF**（整文件重写、diff 从 148 行虚报成 1,017 行）。修掉之后，
> 所有重建脚本改为 `read_bytes().decode()` / `write_bytes(text.encode())` —— **字节忠实，不做换行翻译**。
