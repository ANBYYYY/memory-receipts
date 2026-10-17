# dsh-memory-adapter · 让 memory-pack 读得懂 DSH 的会话日志

> **一句话**：DSH（DeepSeek Harness）的会话日志是 **zstd 多帧**，而所有常规解压 API 都**只解第一帧且不报错** ——
> 这个转接头负责把它正确解开、翻成 [memory-pack](https://github.com/ANBYYYY/memory-receipts) 认识的形状。
>
> **上游仓库一个字不改。** 全部工作都在包外面。
>
> ⚠️ 但**你自己那份 memory-pack 副本上要打一个两行补丁** —— 把它的「原生日志格式允许表」
> 改成如实接受 DSH。原因见 §3.0：不打这个补丁，接通就只剩"让转接头自称是 Codex"这一条路，
> **那等于在回执链的第一环写一句假话**。上游 README §8 本来就写着"换宿主必须改这里"，
> 所以**改这里不是破坏契约，这就是契约给的扩展点**。

---

## 1 为什么需要它（这是个会骗人的坑）

DSH 的会话日志 `session.v4.jsonl.zstd` 是**多个 zstd 帧串联**的。实测：

| 方法 | 5,236,005 B 的日志解出 | 比例 |
|---|---|---|
| `zlib.zstdDecompressSync(整个 buffer)` | **187 B**（1 行） | **0.001 %** |
| `zlib.createZstdDecompress()` 流式 | **187 B**（1 行） | **0.001 %** |
| 按帧魔数 `28 B5 2F FD` 切分后逐帧解 | **18,146,111 B**（3,726 行） | **100 %** |

🔴 **最毒的地方：那 187 B 也是一行合法 JSON。** 所以任何走常规 API 的实现都会"看着完全正常"地丢掉 99.999%，
**不报错、不告警、没有任何迹象**。

**怎么切帧：按 zstd 的帧结构解析**（帧头描述符 → 块头 → 块大小 → 校验和），
**不是"找 4 字节魔数 `28 B5 2F FD`"**。

- 🔴 为什么不用魔数扫描：如果这 4 个字节**出现在某个帧的压缩数据内部**，扫描就会把一个帧劈成两半。
  **这不是理论风险** —— 实测 320 次里造出 **40 次**。（复现脚本没随包发，因为它是"造对抗样本"的一次性探针；
  做法写在 `selfcheck.py` 的 ⑫ 里，那条会**造出**这种帧并断言结构扫描不受骗。）
- 本文件的结构解析**移植自 DSH 自己的实现**：
  `@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js` 的 `scanZstdFrames()`。
  （DSH 也是"自己切帧、再逐帧解"——它注明 *"Node exposes synchronous decoding only as a one-shot API"*，
  所以它去用 stream 的私有 handle 复用原生上下文。）

**正确性怎么证明**（三重，都不靠我自己写的尺）：

1. **与生产者的扫描器逐字节一致**：本文件的结构扫描 vs DSH 自己的 `scanZstdFrames`，
   在**全部 100 个真实会话日志**上，**帧边界 100% 相同**（0 不一致、0 抛错）。
2. **每一帧都带 zstd 校验和**：DSH 写帧时开了 `ZSTD_c_checksumFlag`。⇒ `zstdDecompressSync`
   是**校验通过**才返回，不是"看着像"就返回。**（帧数是逐次快照，会随会话增长而变；
   要自己数就把它印出来：解码读数里的 `checksum_frames` / `frames`。别引用本文的历史数字。）**
3. **DSH 自己写的 `seq` 连续**：逐帧解出来是 `seq` 无重复无缺口、`time` 单调无回退；
   只解第一帧则远不满足。帧边界也**恰好等于行边界**。

---

## 2 依赖

一个**能解 zstd 多帧**的解码器，按优先级自动查找：

1. `node`（PATH 上，或环境变量 `DSH_NODE`，或 `DSH_HOME`/`~/.dsh` 下 DSH 自带的那份）
2. `zstd` 命令行
3. Python `zstandard` 模块

> ⚠️ **Python 标准库做不到这件事**（3.12 没有 zstd；3.14 的 `compression.zstd` 也未必跨帧）。
> 本项目实测走的是 **DSH 自带的 node**；`zstd` CLI 与 `zstandard` 两条路径**未实测**。

---

## 3 用法

### 3.0 先打两行补丁（为什么必须打）

`memory.py` 的 `register()` 有条硬闸：

```python
if not str(meta.get("cli_version", "")).startswith("0.162."):
    raise ValueError("unverified rollout version; adapt and test before registering")
```

DSH 的真实版本不是 `0.162.*`。于是只有两条路：

| 路线 | 第一环是真是假 | 评价 |
|---|---|---|
| 让转接头自称是 Codex（例如声明 `0.162.0+…`） | **假话** | ❌ 对一个以「可核验」为卖点的包，这条最不能省 |
| 把允许表改成如实接受 DSH | **真话** | ✅ 上游 README §8 指定的扩展点 |

本项目走第二条。**上游仓库不动**，补丁只打在你自己的副本上：

```bash
python -B -X utf8 apply_patch.py --package <你的 memory-pack 副本> --check-only   # 先看要改什么
python -B -X utf8 apply_patch.py --package <你的 memory-pack 副本>                # 再打
```

补丁**只改两行**，而且**只认一个原始版本**（sha256 对不上就拒绝，不猜）：

| 行 | 原 | 新 |
|---|---|---|
| L17 | `ADAPTER = "codex-rollout-0.162-response-item-v1"` | `ADAPTER = "dsh-session-v4-response-item-v1"` |
| L137 | `…startswith("0.162.")` | `…not in {"dsh-session-v4", "0.162.0"}` |

`ADAPTER` 也必须改：它会被写进 `sources.json` 与每个收件批次 —— 只改闸门不改它，
`sources.json` 里就会留一句"这份来源的适配器是 codex-rollout-…"，**那还是假话**。

补丁是**幂等**的，并且会先备份 `memory.py.prepatch-backup`。

### 3.1 三条命令

```bash
# ① DSH 日志 → 派生日志（只追加，可反复跑）
python -B -X utf8 dsh_adapter.py --session <session.v4.jsonl.zstd> --out <derived.jsonl>

# ② 登记（只此一次；--cwd 必须与日志里记的 cwd 指向同一处）
python -B -X utf8 memory.py --root <状态目录> register \
       --path <derived.jsonl> --session <会话id> --cwd <工作目录> --from start

# ③ 收件
python -B -X utf8 memory.py --root <状态目录> sync
```

**转接头的默认声明值是 `dsh-session-v4` —— 真话，而且故意不在默认允许表里。**
所以**没打补丁就直接 `register` 会被拒**，报 `unverified rollout version`。
**这个拒绝是设计要的行为**（fail closed）：包不替你的格式背书，你得显式声明你适配过。
`dsh_adapter.py` 的 stdout 会在 `prerequisite` 里提醒这件事。

`dsh_adapter.py` 的 stdout 会打印 JSON 读数，其中 `next_command` 是第 ② 步命令的**模板** ——
里面的 `<PACKAGE>` 与 `<STATE_DIR>` 是**占位符**，要换成你自己的路径（`--session` / `--cwd` 已经是真值）。

**DSH 的日志在哪**：`%DSH_HOME%\sessions\<项目目录>\session-<uuid>\session.v4.jsonl.zstd`
（`DSH_HOME` 未设时默认 `~/.dsh`。）

---

## 4 自检（**不需要任何真实会话**）

> **临时目录要求**：运行自检前，请确保系统临时目录可写，并位于本包的解压目录之外。
> 已知限制：在部分受限环境中，临时文件可能落入解压目录，导致包目录净度检查报告 `DIRTY`。
> 若遇到这种情况，请先修正临时目录设置再重试；该结果本身不足以判断保存或恢复功能是否失效。自检成功会打印 `SELFCHECK OK`。

```bash
python -B -X utf8 selfcheck.py
```

它自己造一份 DSH 形状的合成会话 → 打包成 zstd 多帧 → 跑转接头 → 断言 **50 项**，包括：

- 解码：帧数全部解出、没丢尾帧
- 映射：条数、丢掉哪些、`reasoning` 丢弃
- **正向控制**：该出现的话必须出现
- **负向控制**：藏在 `reasoning` / `system` 里的私有哨兵**必须不出现**
- **幂等**：同一份源再跑一次，追加 0 字节
- **增量**：源里追加一条后，只追加那一条，且首行不被改写
- **截断尾帧**：源末尾挂一个"半条"帧（模拟日志正在被写）⇒ 必须**真的丢掉**、不许崩、不许混进派生日志
- **档案守卫**：已有派生日志时换个来源写同一路径 ⇒ 必须拒绝且一个字节不改
- **`--dry-run`**：不写任何文件
- **`--stamp`**：覆盖声明值（给已把允许表改成别样的人）
- **`apply_patch.py`**：来源哈希对不上 ⇒ 必须拒绝（不猜、不改）
- **帧内部含魔数**：造一个"压缩数据里含 `28 B5 2F FD`"的帧 ⇒ 结构扫描必须只认 1 个帧、且解出的字节与原样逐字节相同（魔数扫描会在这里劈开并失败）

末尾打印 `SELFCHECK OK`。

---

## 5 独立复核（对真实数据）

```bash
python -B -X utf8 verify_derived.py --state <状态目录>
python -B -X utf8 verify_derived.py --state <状态目录> --find "某句话"
```

**它不 import `memory.py`**，自己重读磁盘、自己重算哈希：

- **判据 A**：每个收件事件的 `sha256(源文件[offset:end]) == raw_sha256`
- **判据 B**：切片解析出的文本 == 事件里存的文本
  （⚠️ **只覆盖 `user` / `assistant`** —— `tool_call` / `tool_result` 的字段名不同（`input` / `output`），
  判据 B 不比对它们；那些由判据 A 的**整行哈希**覆盖）
- **判据 C**（`--find`）：指定的一句话能不能被原样取回并溯源

末尾打印 `VERIFY OK`。

---

## 5.1 发布前净度自检

```bash
python -B -X utf8 publish_check.py
```

**如果这个转接头要被公开**，跑这个。它查两件事：

- **结构类**：目录里不该有 `*.jsonl` / `*.zstd` / `*.state.json` 这类数据文件；必须全是 LF（不该混进 CRLF）
- **内容类**：把本机 DSH 会话全解码出来，收集**真实会话 id** 与**长度 ≥40 字的真实正文片段**，
  再在发布集的每个文件里搜 —— **命中就是泄露**

实测（100 个真实会话 / 12,928 条长正文片段）：**命中 0**，`PUBLISH-CHECK CLEAN`。

⚠️ **它的诚实边界**：找不到会话目录时它**明说"内容类未验"**，不会假装通过。
**"没检查"和"检查过没问题"不是一回事。**

---

## 6 设计约束（三条，都别破）

1. **不改 memory-pack。** 转接头在包外出产一份"派生日志"，再交给 `memory.py register/sync`。
   （原包跑前跑后逐字节一致。）
2. **只追加。** 派生日志一旦写出，前面的字节**永不改写** —— `memory.py` 的游标存的是
   `prefix_sha256`，改写前缀会让 `sync` 报 `source truncated/replaced`。
   水位记在 `<派生日志>.state.json` 里，按 DSH 的 `seq` 去重。
3. **不打印正文。** 读数只报计数／字节／哈希；内容只落盘。

---

## 7 形状映射

| DSH 记录 | 派生日志 | 备注 |
|---|---|---|
| `session`（首条） | 首行 `session_meta` | `id` / `cwd` 原样；`cli_version` 见下 |
| `user/message` | `message` role=`user` | 取 `content[].type=="text"` |
| `assistant/message` | `message` role=`assistant` | 只取 `text`；`reasoning` 丢弃；`tool-call` 项丢弃（避免与下一条重复） |
| `tool/call` | `function_call` | `callId` / `name` / `arguments` |
| `tool/result` | `function_call_output` | `toolCallId` / 文本 |
| 其余（`system/message`、`developer/message`、`todo/write`、`compaction/*` …） | **丢弃** | 与 memory-pack 的"公开输入"定义一致 |

派生日志每行还带 `timestamp` 与 `ordinal`（= DSH 的 `seq`），给 memory-pack 做溯源。

---

## 8 边界与未验（照实写）

| 项 | 状态 |
|---|---|
| `cli_version` 声明值 = `dsh-session-v4` | **真话**，而且故意不在默认允许表里 ⇒ 没打补丁时 `register` 会拒（fail closed）。打 §3.0 那两行补丁后如实接受。**曾经的假前缀做法已废弃**。 |
| 图片附件 | **丢弃**（memory-pack 的 `public()` 按设计只收文本）。**已计数上报**，不是静默丢。 |
| `reasoning` 推理项 | 丢弃 —— 与 memory-pack 的哨兵设计一致（自检里有负向控制）。 |
| `zstd` CLI / python `zstandard` 两条解码路径 | **未实测**（开发机没有）。 |
| `sync` 成功 | 只等于**收到**，**不等于语义接受**。 |
| 末帧不完整（日志正在被写） | 解码器**丢掉该尾帧**并标记 `truncated_tail_frame_dropped: true`。⚠️ **这条曾经是假的**：当时标记为 `true`，但那半条**仍然被留在输出里** —— 结果是"守卫报成功、实际没守"，喂给 adapter 直接 `JSONDecodeError: Unterminated string` 崩掉。已修，并在自检 ⑦ 里加了覆盖（对照实测：改前 1 条坏行 + adapter rc=1；改后 0 条坏行 + adapter rc=0）。 |
| 上游若改了日志版本 | 派生日志的 `session_meta` 由**本转接头**签发，所以不受上游版本变化影响；但**映射规则要跟着改**。 |

---

## 9 独立红队评审（2026-10-17）与处置

发布前，这个包被一个**独立红队**攻过：另一个上下文里的评审者，**看不到任何对话史**，
只拿到代码和一句"去找真问题"。它自己把 `app.asar` 里的解码器挖出来逐行比对过。

**它确认打不过的部分**（＝它没能推翻的）：

- 结构切帧是 DSH `scanZstdFrames` 的**忠实移植**（它抽出 `lib/index.js` 逐行比过）；
- 100 个真实日志 / 15,223 帧 —— **100% 带校验和、帧输出 100% 以 `\n` 收尾、`seq` 无缺口**，
  且与魔数扫描的**边界集合 100/100 一致**；
- 补丁后的 `register → sync → verify` 全链路在**副本**上验通，原包未被改动。

**它打穿的（已修，每一条都有回归测试）**：

| # | 问题 | 处置 | 回归 |
|---|---|---|---|
| P0-1 | 并发/重复运行会把整份会话**静默翻倍**，还往文件中间插一行 `session_meta`（3 进程 × 6 轮全部复现，三个进程 rc 全 0） | 给档案加**独占锁**，锁在**读水位之前**拿 | 自检 ⑮ |
| P0-3 | `verify_derived.py` 在**什么都没验**时也打印 `VERIFY OK`（判据恒真） | `checked==0` ⇒ `VERIFY VACUOUS` + rc=2 | 自检 ⑯ |
| P0-4 | **`python-zstandard` 回退路径默认只解第一帧** ⇒ 没 node 的人被静默喂 0.001%，**正是本工具要修的那个 bug** | 显式 `read_across_frames=True`；两条未实测回退**改为默认拒绝**，要 `--allow-untested-decoder` 才放行 | 自检（回退路径读数） |
| P0-5 | 派生日志被外部截断后 adapter **rc=0 报成功**（state 里记着 `derived_bytes` 却从不回读） | 追加前校验文件长度与 state 记录一致，不一致就拒 | 自检 ⑬ |
| P0-6 | 水位文件丢失／换机 ⇒ **整份会话重复追加**（幂等承诺是无条件的） | 档案存在但水位缺失 ⇒ **拒绝**，提示恢复水位或换新档案 | 自检 ⑭ |
| P1-3 | 往"末行不带换行"的档案追加 ⇒ 产出坏 JSON 行 ⇒ 该来源后续 `sync` 整批 `check_failed` | 末行不是完整行 ⇒ 拒绝追加 | 自检（同上段） |
| A-3/A-5 | `publish_check.py` 跳过内容类检查时仍印 `CLEAN`+rc=0 | 改为 `PUBLISH-CHECK INCOMPLETE` + **rc=2**；解码失败的日志数**上报** | 自检 ⑰ |

**它打穿的、我【没有】修的（照实写，别当成没有）**：

| # | 问题 | 为什么没修 |
|---|---|---|
| P1-1 | **末帧撕裂时整帧丢弃**，而 DSH 自己的读取器能用 `finishFlush: ZSTD_e_flush` 从**同一个撕裂帧**里救回完整记录（红队实测救回 131,072 B / 994 条 vs 本工具 0 条） | 那要改用 stream 的私有 handle 契约（DSH 的做法）。**没做**：宁可少收，不可错收；且这条要真机撕裂样本才好验 |
| P1-2 | 完整末帧只要不以 `\n` 结尾也**整帧丢弃**，与"半条"混用同一个读数 | 读数里现在区分了 `torn_tail_frame_start`，但**丢弃行为没改** |
| P0-2 | 「只解第一帧」这条失效模式，对**已经产出的**派生日志**不可见**：验证链只做"派生日志↔收件事件"的内部一致性，**没有**一条"派生日志 vs 源 `.zstd`"的交叉判据 | 现在靠"解码器结构扫描 + 与 DSH 扫描器 100/100 一致"来防，**但那不是运行时判据**。要做需在 `verify_derived.py` 里回读源 `.zstd` 对齐 `seq` 集合 —— **未做** |
| P1-6 | 运行期**整份会话明文**落到 `%TEMP%\dsh-adapter-*\raw.jsonl`；清理失败被 `ignore_errors` 吞掉 | 没改。**知情要点：跑一次就等于在临时目录留了一份明文**（本机观测 399,867 B） |
| A-4 | `publish_check.py` 的 `--min-fragment 40` ⇒ **短正文泄露看不见** | 阈值是为了压误报，没改 |

**三条它明确指出、我认下的"说法比代码宽"**（已改文档）：`next_command` 不是"照抄即可"；
判据 B 只覆盖 `user`/`assistant`；README 曾引用一个**不在包里**的复现脚本、以及**不可重跑的历史帧数**。

> 一句话：**红队把"我声称的"和"代码实际做的"逐条对了一遍。上面第二张表是我的欠账。**

---

## 10 许可

**待定**（上传前由项目所有者拍板）。上游 memory-pack 本体是 MIT。
