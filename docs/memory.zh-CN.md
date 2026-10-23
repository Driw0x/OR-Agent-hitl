[Français](memory.md) | [简体中文](memory.zh-CN.md)

<a id="or-agent--mémoire-technique-du-projet"></a>
# OR-Agent — 项目技术文档

本文档记录了OR-Agent的技术成果与决策。[README](../README.zh-CN.md)介绍了产品及其命令；[roadmap](roadmap.zh-CN.md)保留了已完成的关键节点。

<a id="m0--cadrage-gap"></a>
## M0 — 间隙对齐

项目规定每项任务必须明确且不可分割地分配至某一资源。对于资源`i`和任务`j`，`x[i,j]`是二元变量，`c[i,j]`是成本，`a[i,j]`是消耗量，`b[i]`是容量：

```text
Minimiser : somme(i,j) c[i,j] * x[i,j]
Sous contraintes :
    pour chaque tâche j : somme(i) x[i,j] = 1
    pour chaque ressource i : somme(j) a[i,j] * x[i,j] <= b[i]
    x[i,j] appartient à {0, 1}
```

该完整合约允许在求解器中构建并控制模型，无需额外解释。[GAP范围](../README.zh-CN.md#problème-gap-pris-en-charge)定义了边界、单位和可接受的约束。模型的验证、与源数据的一致性、可行性及最优性是四个独立的控制环节。

<a id="m1--validation-et-représentation"></a>
## M1 — 验证与表示

[models.py](../or_agent/models.py) 提供了CLI、代理和求解器之间的通用合约：`Gap`用于数据，`Extraction`用于配置`ready`、`clarify`、`unsupported`和`invalid`，以及`Candidate`用于分配和声明的成本。

Pydantic要求模型严格且封闭。资源占据矩阵的行，任务占据矩阵的列。控制项涵盖唯一标识、维度、数值域和单位；JSON读取器也拒绝重复键和无穷大数值。

一个提取`ready`要求完整问题、无阻塞诊断，并为GAP中每个七个字段提供源文本的精确引用。其他配置项要求解释说明，不包含需要求解的问题。有效案例与拒绝案例由[test_models.py](../tests/test_models.py)覆盖。

<a id="m2--résolution-cp-sat"></a>
## M2 — CP-SAT求解

[solver.py](../or_agent/solver.py) 构建二元变量、唯一分配、容量和目标。大语言模型提供数据，绝不提供可执行代码给求解器。

CP-SAT使用一个工作进程、固定种子和零最优容差。默认预算为五秒。结果保留候选解、目标值、最优下界、版本、参数和求解器运行时间。候选解的成本以整数算术重新计算；最优性证明要求`OPTIMAL`成立，且目标值等于下界值。

[test_solver.py](../tests/test_solver.py) 将小规模案例与穷举法对比，并验证状态、数值边界和预算。结果将CP-SAT的证明与候选解的独立验证关联起来。

<a id="m3--vérification-indépendante"></a>
## M3 — 独立验证

[verify.py](../or_agent/verify.py) 使用独立的算术计算，不导入求解器或转换器。它验证标识符、任务覆盖的精确性、负载、容量和总成本。违规情况以结构化形式返回。

[test_verify.py](../tests/test_verify.py) 注入遗漏、重复、未知标识符、超限和错误成本。在[提供的示例](../examples/small.json)中，验证器发现负载`R1=4`、`R2=2`和成本`6`；CP-SAT独立地建立了最优性。

<a id="m4--boucle-agent"></a>
## M4 — 代理循环

[agent.py](../or_agent/agent.py) 集成 Ollama，实现解码、验证、求解与输出。生成过程已从指令要求的 JSON 格式演进为结构化模式和有界修正。评估工具保留了 `json`、`structured` 和 `structured-repair` 策略。

CLI 工具 `agent` 采用结构化生成，最多一次修正，即最多两次调用。修正过程保留已解析的数据。预算限制调用次数、总时长和求解深度；观察记录原始响应、错误、诊断和资源检查结果。

分支由 [test_agent.py](../tests/test_agent.py) 以受控响应覆盖，并在 CLI 中执行了真实的 Ollama 驱动。该流程直接解决一个已验证的提取任务；人工介入由 `route` 和网页实现。

<a id="m5--évaluation"></a>
## M5 — 评估

初始语料库 [corpus initial](../data/benchmark.json) 包含 **16 个案例：7 个开发案例和 9 个测试案例**。扩展语料库 [corpus étendu](../data/extended-benchmark.json) 包含 **130 个案例：30 个开发案例和 100 个测试案例**。它们覆盖了可执行与不可执行实例、负成本、零消耗、歧义以及超出范围的请求。

评估工具将测量划分为准备、执行和复核阶段。审计检查足迹、引用和家族分离。对于扩展语料库中的 **105 个完整实例**，它们验证了引用、CP-SAT 求解结果以及从源数据的重建。缺失结果仍计入分母。

历史评估发现存在遗漏、错误关联和转置问题，且未通过自动修复恢复出最终正确响应。网页驱动在七条完整的 GAP 来源中产生了六条不完整表述和一条错误完整表述，即 **0/7 条完整且忠实的表述**。这些观察结果促使下文明确描述的审查流程，但不构成整体可靠性度量。

<a id="pourquoi-le-hitl-a-été-introduit"></a>
## 为何引入 HITL

仅生成结构化表述不足以确保其与原问题的忠实性。因此，OR-Agent 在允许 HITL 流程求解前，必须进行明确的人工审查。审查者对比源材料、数据和约束条件；其批准才允许进入审查流程。

<a id="implémentation-du-hitl"></a>
## HITL 的实现

[hitl.py](../or_agent/hitl.py) 定义了格式 `HITL-GAP: 1`：目标、库存、单位、能力，以及任务-资源配对的记录条目。未知值以 `?` 表示，并阻止完整性；诊断信息记录在 `Issue:` 行中。

流程保留原始请求并依次执行：

1. 手动提案或 Ollama 提案，随后进行草稿修正。
2. 语法、覆盖范围和数据的验证。
3. 对源材料、提案及重建矩阵的复核。
4. 当流程要求时，确认 GAP 范围。
5. 对复核后修订内容的明确批准。
6. 求解已批准的 GAP 并验证候选方案。

会话保留 `original.txt`、`proposal.txt`、`session.json`、诊断和事件。批准将源与提案的指纹关联；被批准的元素将保存在 `approved/` 下。任何修改将使审核、确认和批准失效。

CLI 使用确认 `SCOPE:<review-token>` 和 `APPROVE:<review-token>`。LLM 无法授予这些权限。[test_hitl.py](../tests/test_hitl.py) 检查在缺少或过期授权情况下的修正、拒绝、未知和无法解决的情况。

<a id="extraction-et-routage-déterministes"></a>
## 确定性提取与路由

[deterministic.py](../or_agent/deterministic.py) 提取与源中字段、任务、资源、值、单位、引用及位置相关的事实。位置遵循 Python 字符索引，区间为 `[start, end)`。

[routing.py](../or_agent/routing.py) 重建 GAP 并检查库存、单位、数字和文本覆盖、标准约束、矛盾缺失以及规范一致性。证书 `VERIFIED_COMPLETE` 允许分支 `AUTONOMOUS`，无需 LLM 也无需审核，仅适用于已确认的封闭格式。其他证书则导向人工审核或报告阻塞问题。

手动草稿单独重新确认已验证的事实，并将缺失数据保留至 `?`。未被识别的文本需要明确的范围确认。源中检测到的矛盾和排除项在草稿重写后仍然存在；修正源需要新的会话。[test_routing.py](../tests/test_routing.py) 覆盖证书、重建和接纳流程。

<a id="interface-web"></a>
## 网页界面

[web.py](../or_agent/web.py) 重用路由、HITL、解析和验证服务。HTML/CSS/JavaScript界面直接展示源码、事实、诊断、表述和结果，无需前端构建。SSE事件跟踪处理流程的各个步骤。

会话持久保存在`.or-agent-web/`中，并在重启后仍可访问，无需重新运行计算。可视化展示分配与能力信息；SVG、HTML和JSON导出与当前版本关联。导入的会话在启动时被允许，并以只读方式查看。

单用户本地服务器控制变更来源和会话文件的访问权限。可选的生成过程会验证Ollama模型的摘要和可用资源；手动草稿在无需Ollama的情况下仍可访问。

[test_web.py](../tests/test_web.py) 覆盖了HTTP服务、权限、文件删除和文件保护功能。[web_browser.mjs](../tests/web_browser.mjs) 在Chrome中执行确定性路径和HITL操作，结合CP-SAT求解器和真实验证器，实现受控的生成与审批。

<a id="bilan-des-fonctionnalités-et-validations-v1"></a>
## 功能与验证情况汇总 V1

V1版本整合了严格GAP合约、确定性提取与路由、可选的本地生成、HITL审查、CP-SAT求解、独立验证、命令行接口、网页会话、可视化和导出功能。

已完成的技术验证包括：

- **864个测试通过，零失败，零被跳过**，在Windows环境下最终验证完成。
- Chrome 决定性流程和 HITL：授权、编辑后失效、持久性、JSON/SVG/HTML 导出的可视化与一致性，屏幕宽度为 **1440、1280 和 390 px**，支持移动端操作。
- Windows 系统下使用 Python 3.13，Linux Ubuntu WSL2 下使用 Python 3.12，依赖项固定，可编辑安装，示例与离线审计。Chrome 流程已在 Windows 上验证。
- 导入、源代码编译、依赖关系一致性、命令行接口及两个数据集的审计。

在调查员重建建议并经人工审批后，历史控制测试获得了六个最优解 **15、2、−7、5、55 和 56**，包含已验证的候选解，且不可行性符合枚举结果。相关的 21 个 JSON/SVG/HTML 导出控制测试全部通过。三个独立的决定性控制测试分别得到最优解 **6 和 −5**，已验证，且不可行性符合要求。

**V1 在功能和技术范围内已结束。**
