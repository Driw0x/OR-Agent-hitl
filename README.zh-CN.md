[Français](README.md) | [简体中文](README.zh-CN.md)

<a id="or-agent"></a>
# OR-Agent

用于构建、求解和验证容量约束下指派问题的本地应用。

<a id="présentation"></a>
## 简介

OR-Agent 将**广义指派问题（GAP，Generalized Assignment Problem）**的数据转换为结构化的问题表述，验证后使用 CP-SAT 求解。独立验证器重新计算任务指派、资源负载及总成本。

网页界面和命令行接口（CLI）提供确定性的提取、可选的本地生成（使用 Ollama）以及人工审查与批准（HITL）。会话、可视化和导出功能允许查看和保存结果。

**V1 的功能与技术范围已完成。**

<a id="problème-gap-pris-en-charge"></a>
## 支持的 GAP 问题

所采用的 GAP 问题基于以下规则：

- 所有任务都是强制性的且不可分割的。
- 每个任务恰好被分配到一种资源上。
- 任务的消耗可能依赖于所选择的资源。
- 对于每种资源，其消耗之和不得超过其容量。
- 目标是使分配成本之和最小化。

成本是介于 `-10^9` 和 `10^9` 之间的整数。消耗和容量是介于 `0` 和 `10^9` 之间的整数。允许负成本和零消耗。单位必须明确说明；不进行任何隐式转换。模型最多接受 **10,000 个任务-资源配对**。

路径规划、任务先后约束、截止日期、时序调度、可选任务及额外的任务间约束均不在此问题契约范围内。输入不完整时应请求澄清；不得删除超出范围的约束来使问题可解。

<a id="fonctionnement"></a>
## 运行机制

```text
Énoncé original
    |
    v
Extraction déterministe des faits et contrôle du périmètre
    |                                              |
    v                                              v
Format fermé certifié                    Entrée non certifiée
Construction directe du GAP              Brouillon manuel ou Ollama
Sans LLM ni revue humaine                Correction et validation
    |                                    Revue et confirmation de portée
    |                                    Approbation de la révision exacte
    |                                              |
    +----------------------+-----------------------+
                           |
                           v
                    Conversion CP-SAT
                           |
                           v
                    Résolution sous budget
                           |
                           v
                    Vérification arithmétique indépendante
                           |
                           v
                    Résultat, visualisations et exports
```

确定性认证仅适用于已识别的封闭格式，并检查数据及其来源。对于其他输入，人工填写或 Ollama 生成的提案必须经过检查、审查和批准。批准仅授权已经审阅的具体修订版本，并不自动保证其忠实于原始问题陈述。任何修改都会使相关授权失效。

一个经过验证的模型符合数据协议。一个经过验证的候选方案对所提供的 GAP 是可行的。最优解还需额外的 CP-SAT 证明：

- `OPTIMAL`：当候选方案的证明和验证均有效时，确定最优解。
- `FEASIBLE`：候选方案已提供，提交给验证器；最优性未被证明。
- `INFEASIBLE`：所提供的 GAP 被证明不可行。
- `UNKNOWN`：在预算范围内搜索未得出结论。

技术错误将单独报告。

<a id="fonctionnalités"></a>
## 功能

<a id="extraction-et-formulation"></a>
### 提取与建模

- 严格的 Pydantic 模型：类型、维度、唯一标识符、单位，以及对未知字段的拒绝。
- 数据来源及对缺失、矛盾或无法解释元素的诊断。
- 对已知封闭格式的确定性重建。
- 编辑一份可读的提案，包含任务-资源关联和重构矩阵的校验。
- 人工审核，确认GAP范围，并基于修订足迹进行批准。

<a id="résolution-et-vérification"></a>
### 求解与验证

- 通过代码构建的二元分配变量、累积能力与成本目标。
- 使用时间限制的CP-SAT求解，根据状态提供候选解和边界。
- 独立验证任务覆盖、负载和总成本。
- 区分呈现可行解、已证明最优解、不可行性以及无结论的输出。

<a id="interface-et-traçabilité"></a>
### 界面与可追溯性

- 本地Web界面，无需前端构建，通过服务器事件（SSE）跟踪处理步骤。
- 会话持久化，重启后可查阅及生成诊断。
- 可视化分配与使用能力；支持导出SVG、HTML和JSON格式。
- 在启动时明确允许的情况下，仅读取现有会话的导入。

评估工具会审计语料库，并对比生成与纠错方法。

<a id="architecture-et-technologies"></a>
## 架构与技术

```text
or_agent/                      Cœur du projet : extraction, validation, revue humaine, résolution et interface web
data/                          Jeux de données et références d'évaluation
examples/                      Exemples de problèmes GAP
tests/                         Tests automatisés
docs/                          Roadmap et mémoire technique
```

基础层使用 **Python**、**Pydantic**、**OR-Tools CP-SAT** 和 **pytest**。Ollama提供可选的本地生成功能，标识为`qwen2.5-coder:7b`。HTTP服务器及Ollama调用使用标准Python库；前端使用HTML、CSS和JavaScript。

<a id="installation"></a>
## 安装

前置要求：**Python 3.12及以上版本；已在Python 3.12和3.13上验证安装**，Git；Ollama为可选。可复现环境的版本在 [requirements.txt](requirements.txt) 中固定。包依赖及相同最低版本Python在 [pyproject.toml](pyproject.toml) 中声明。

在Windows PowerShell下，克隆仓库。对于已有副本，从根目录开始创建环境。

```powershell
git clone https://github.com/Driw0x/OR-Agent-hitl.git
cd OR-Agent-hitl
python -m venv .venv
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install -r requirements.txt
& $py -m pip install --no-deps -e .
& $py -m pip check
```

在Linux下：

```sh
git clone https://github.com/Driw0x/OR-Agent-hitl.git
cd OR-Agent-hitl
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pip check
```

`python3` 必须对应 Python 3.12 及以上版本。若其指向更早版本，则需显式使用已安装的兼容解释器，例如 `python3.12 -m venv .venv`。固定依赖同样要求 Python 3.12 及以上版本。

以可编辑模式安装也提供命令 `or-agent`。以下示例使用 `python -m or_agent` 显式标识Python环境。

<a id="utilisation-de-linterface-web"></a>
## 使用Web界面

从根目录在PowerShell下启动界面：

```powershell
$py = ".\.venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"
& $py -B -m or_agent web
```

在Linux下：`.venv/bin/python -B -m or_agent web`。

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，使用此确切地址而非`localhost`。服务器为本地且单用户。会话持久存储在`.or-agent-web/`中。**Ctrl+C** 会等待当前处理完成后再终止，不会中断共享Ollama服务器。

1. 输入一个GAP问题或选择内置示例，然后点击 **启动分析**。
2. 查看提取的事实和诊断信息。一个完全认证的输入将直接求解，无需LLM或人工审核。
3. 否则，请输入一个提案或点击 **请求Ollama提案**。与源内容对比，进行修正，**保存修改**，然后 **验证提案**。
4. **执行审核**，然后 **确认范围**。勾选批准并点击 **解决已批准的差距** 以批准并解决该精确修订。
5. 查看状态、成本、费用及独立验证情况。
6. 检查可视化结果并导出结果。一旦发生无效修改，将导致所有批准和当前导出失效。

检测到的缺失数据、矛盾和超出范围的约束会阻止批准。源内容中的矛盾需在新会话中通过修正声明解决：清除草稿中的诊断不会解决此问题。手动路径和认证示例可在不使用Ollama的情况下正常运行。

`--port`、`--sessions`、`--base-url` 和 `--import-session` 选项可用于调整启动配置。请查阅 `& $py -m or_agent web --help`；在Linux系统中，将 `& $py` 替换为 `.venv/bin/python`。

<a id="exemple-sans-llm"></a>
### 无LLM示例

该陈述遵循带标签行的封闭格式。可直接粘贴至界面中：

```text
Assign each task exactly once; minimize total cost within capacities.
Tasks: T1, T2, T3.
Resources: R1, R2.
Cost unit: credit.
Resource unit: slot.
R1 capacity: 4.
R1 costs: T1=2, T2=8, T3=3.
R1 consumption: T1=2, T2=2, T3=2.
R2 capacity: 2.
R2 costs: T1=5, T2=1, T3=4.
R2 consumption: T1=2, T2=2, T3=2.
```

最优分配为 `T1 → R1`、`T2 → R2`、`T3 → R1`。成本为 **6个信用点**，其中 R1 上占用 **4个槽位**，R2 上占用 **2个槽位**。相同的实例以 JSON 格式提供在 [examples/small.json](examples/small.json) 中。

关键词、标点符号和显式关联属于认可格式。自由翻译或改写可能触发人工审核。[examples/small.txt](examples/small.txt) 是面向生成路径的文本版本；它并非上述认证格式。

<a id="utilisation-du-cli"></a>
## CLI 使用方法

<a id="valider-résoudre-et-vérifier-un-gap-json"></a>
### 验证、解决并检查一个 JSON 差距

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py -B -m or_agent validate examples/small.json
& $py -B -m or_agent solve examples/small.json --seconds 5
& $py -B -m or_agent verify examples/small.json examples/candidate.json
```

JSON 矩阵使用 **行资源和列任务**。`validate` 控制结构契约。`solve` 解决该模型并验证其候选方案。`verify` 重新计算所提供候选方案的可行性与成本，但不搜索最优解。这些命令直接作用于 GAP JSON。

[examples/infeasible.json](examples/infeasible.json) 展示了一个不可行实例；[examples/multiple.json](examples/multiple.json) 包含一个具有多个最优解的实例。`schema` 展示了提取所需的输出结构。

<a id="analyser-un-énoncé-avant-admission"></a>
### 在接纳前分析陈述

```powershell
& $py -B -m or_agent route analyze examples/small.txt
```

该命令显示确定性事实和证书，不调用 Ollama 也不进行求解。`route submit` 之后将创建一个带有认证分支或 HITL 的会话。子命令 `review`、`validate`、`scope`、`approve`、`reject`、`status` 和 `solve` 展示了审核步骤。请查阅 `& $py -m or_agent route --help` 和 [技术文档](docs/memory.zh-CN.md) 以了解审核契约。

<a id="génération-et-correction-bornée"></a>
### 有界生成与修正

```powershell
& $py -B -m or_agent agent examples/small.txt --total-seconds 180 --solver-seconds 5
```

结构化生成允许最多一次修正，即最多两次调用。`--max-calls 1` 限制处理仅限一次调用。命令 `agent` 可直接解决一个已验证的提取，无需人工批准。为在解决前控制提案，可使用网页或 `route`。

<a id="configuration-ollama-facultative"></a>
## 可选的 Ollama 配置

Ollama 是可选的。安装完成后，准备模型：

```powershell
ollama pull qwen2.5-coder:7b
ollama list
```

Web 默认使用 `http://127.0.0.1:11434`；`--base-url` 可用于选择其他地址。它在 [web.py](or_agent/web.py) 中控制模型的预期哈希值：相同名称但哈希值不同的模型可能会被拒绝。CLI 工具 `agent` 也接受 `--model`；这不会改变 Web 端允许的模型。

资源限制控制可能会拒绝生成或中断客户端等待。客户端超时并不能保证 Ollama 中计算的取消。命令 `resources` 和 `ollama` 提供了可用的诊断信息；如果生成不可用，手动输入仍然可用。

<a id="tests"></a>
## 测试

从根目录开始，不启动 Ollama 运行流程：

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py -B -m pytest -q -ra
& $py -B -m or_agent.evaluate audit
& $py -B -m or_agent.extended_corpus audit
```

在 Linux 上，将 `& $py` 替换为 `.venv/bin/python`。

测试覆盖了模式、CP-SAT 与穷举比较、损坏的候选、预算、来源、评估和 HITL/web 会话。生成测试使用受控的响应。

完整测试套件要求 **Node 22 及以上版本和 Chrome/Chromium**，以通过其调试协议控制真正的浏览器，无需额外的 JavaScript 依赖。`OR_AGENT_CHROME` 可用于提供浏览器路径，如果浏览器未被检测到。缺少工具将导致明确的失败。文件保护测试需要链接创建权限。

仅执行浏览器路径、确定性流程和 HITL 的测试，同时控制导出和宽度为 1440、1280 和 390 px 的设置：

```powershell
& $py -B -m pytest -q -s tests/test_web.py::test_browser_session_deletion_and_combined_approval
```

浏览器使用本地 HTTP 服务器和真实的 CP-SAT 求解器。Ollama 的不可用性以及生成响应情况被控制；无需实际的 LLM 生成。

语料库及其参考文件允许在没有 Ollama 的情况下进行离线审计。[V1 总结](docs/memory.zh-CN.md#bilan-des-fonctionnalités-et-validations-v1) 概括了已完成的技术验证。

<a id="documentation"></a>
## 文档

- [路线图](docs/roadmap.zh-CN.md)：里程碑、已完成的检查点和 V1 状态。
- [技术文档](docs/memory.zh-CN.md)：M0–M5 实现、HITL、确定性路由、Web 界面和 V1 验证。
- [示例](examples/small.json)：用于在无需 LLM 的情况下验证求解的起点。

<a id="licence"></a>
### 许可

该项目以 MIT 许可证分发。详见 [LICENSE](LICENSE)。

版权所有 © 2026 Victor YE。
