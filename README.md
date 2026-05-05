# MVP Agent

`MVP Agent` 是一个面向 `Windows 10 / Windows 11` 的多 Agent 代码指挥框架。

它的目标不是替代所有模型，而是像一个稳定的 AI 技术负责人一样，把任务拆开、派工、复核、回收结果，再把成本、速度和质量压到一个更平衡的状态。

如果说很多 agent 更像“单兵作战”，那 `MVP Agent` 更像一个小型 AI 工程团队的总控台。

## 它能做什么

- 接收一个复杂任务并自动拆成规划、架构、编码、测试、验收等子任务
- 在 `Ollama`、`OpenClaw`、`Hermes Agent`、`Claude Code`、`Codex` 之间做角色化分工
- 优先把低风险、重复型任务交给本地模型，减少 token 消耗
- 在远端高质量模型、本地模型、外部 agent 之间做成本感知路由
- 记录任务历史、框架历史、技能网格、工作区状态和硬件负荷
- 在桌面 GUI 中显示当前协作链路、任务进度、Pet 状态和控制平面信息

## 为什么它不只是另一个 agent

`MVP Agent` 的核心不是“我也能聊天”，而是：

- `Leader-style Orchestration`
  先判断，再拆分，再分工，不是一股脑全交给单个模型。
- `Skill Mesh`
  能看到任务真正依赖哪些 skills，缺了哪些能力，再决定补哪条链路。
- `Budget-aware Routing`
  省 API、均衡、高质量三种倾向下，路由行为会不同。
- `Multi-Transport Compatibility`
  不只支持 CLI agent，也给 IDE bridge 和 service mesh 打好了基础。

## 当前兼容方向

内建或一等支持：

- `Ollama`
- `OpenClaw`
- `Hermes Agent`
- `Claude Code`
- `Codex`

已准备好接入的扩展面：

- `Aider`
- `Goose`
- `Gemini CLI`
- 其他 `external_cli_agent`
- IDE 扩展桥接类 agent：`extension_bridge_agent`
- HTTP / daemon / mesh 类 agent：`service_mesh_agent`

## 主要能力

### 1. 多 Agent 协调

把任务拆分成：

- planner
- builder
- reviewer
- fallback

并根据能力、权限、成本和速度做动态分配。

### 2. 代码工作流优先

它本质上是一个 `code agent framework`，重点优化了：

- 仓库感知文件级路由
- 并行编码分包
- 代码回包展示
- 审核与改派工

### 3. 快慢双通道探测

- `quick ping`
  适合日常快速探测
- `deep ping`
  适合验证真实 roundtrip

### 4. GUI 控制平面

桌面端已经具备：

- 左侧状态栏
- 右侧问答主屏
- 任务时间线
- 协作流
- 技能网格
- 工作区快照
- MVP Pet 状态联动

## 仓库结构

```text
.
├─ .github/workflows/
├─ docs/
├─ examples/
├─ mvp/
├─ scripts/
├─ skills/
├─ tests/
├─ tools/mvp/
├─ .gitignore
├─ LICENSE
├─ README.md
└─ requirements.txt
```

## 快速开始

### 1. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

### 2. 运行桌面端

```powershell
python -m mvp app
```

### 3. 跑一次自检

```powershell
python -m mvp app --self-test
```

### 4. 查看框架状态

```powershell
python -m mvp health
python -m mvp ping --mode quick
python -m mvp ping --mode deep
```

## 示例配置

看这里：

- [examples/mvp.config.example.json](./examples/mvp.config.example.json)
- [examples/external-agents.example.json](./examples/external-agents.example.json)
- [examples/routing-policy.example.json](./examples/routing-policy.example.json)

`external-agents.example.json` 里已经包含了 3 类扩展面示例：

- CLI agent
- IDE bridge agent
- service mesh agent

## 内置技能

仓库里已经附带一个可独立复用的 leader skill：

- [skills/windows-multi-agent-orchestrator](./skills/windows-multi-agent-orchestrator)

它可以单独上传成 GitHub skill，也可以继续作为 `MVP Agent` 的协调核心。

## 验证与发布

### 仓库校验

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\validate-repo.ps1
```

### 端到端冒烟

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-e2e-smoke.ps1
```

### 打包发布

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package-release.ps1 -Version v0.1.2
```

## 项目定位

`MVP Agent` 不是一个已经完结的终点产品，它更像一个正在成长的多 Agent 操作系统雏形。

但它已经具备一个成熟开源框架最重要的几样东西：

- 清晰的角色分工
- 稳定的控制平面
- 可验证的测试链路
- 可扩展的 agent transport 基础
- 可以直接继续迭代和发布的仓库结构

## License

MIT
