# MVP Agent

`MVP` 是一个本地优先的领导型 AI 编排层。

它自己不抢着做所有事情，而是先理解任务，再拆分子任务，然后把不同工作派给最合适的框架或模型，最后再做验收。

## 当前能力

- 任务拆解
- 动态派工
- 成本控制
- 结果验收
- 本地优先 / 省 API 路由
- 中文桌面 GUI
- 框架状态检查
- 联调自检
- 硬件负荷监控
- 框架历史聚合

## 默认成员

- `Hermes 设计师`
  - 后端：`OpenClaw Agent`
  - 目标：`hermes-wingman @ openai-codex/gpt-5.4`
- `OpenClaw 工程师`
  - 后端：`OpenClaw Agent`
  - 目标：`main @ openai-codex/gpt-5.4`
- `Ollama 规划器`
  - 模型：`qwen3.5:9b`
- `Ollama 审核器`
  - 模型：`qwen3.5:9b`
- `Ollama 重型本地模型`
  - 模型：`qwen3-coder:30b`
- `Codex 架构师`
  - 模型：`gpt-5.4`
- `Codex 终审官`
  - 模型：`gpt-5.4`

## 桌面入口

- `MVP Agent.lnk`
  - 推荐入口
  - 带桌面图标
- `MVP Agent.vbs`
  - 静默启动 GUI
- `MVP Agent.cmd`
  - 通过 `pythonw` 启动 GUI
- `MVP Agent CLI.cmd`
  - 打开命令行版 shell

## 常用命令

启动桌面端：

```powershell
python -m mvp app
```

查看成员：

```powershell
python -m mvp workers
```

检查连通状态：

```powershell
python -m mvp health
```

仅做规划：

```powershell
python -m mvp plan --task "先拆一个本地 AI 总控平台的迭代计划" --mode balanced
```

规划并执行：

```powershell
python -m mvp run --task "省 API，先拆分任务，再决定哪些交给本地模型，哪些交给更强框架"
```

打开交互 shell：

```powershell
python -m mvp shell
```

## 历史与监控

- `MVP` 会把本地调用审计写到 `mvp_runs/worker_calls.jsonl`
- `MVP` 会聚合显示：
  - `MVP` 自己的运行报告
  - `OpenClaw / Hermes` 会话历史
  - `Codex` 会话历史
  - 本地 worker 调用审计
- GUI 里可以直接看到：
  - 当前派工进度
  - 成员状态
  - 内存、磁盘、CPU、GPU 负荷
  - 推荐运行档位

## 说明

当前这台机器上的 `Hermes` 角色，实际上是通过 `OpenClaw` 里的 `hermes-wingman` agent 实现的，不是独立 Hermes runtime。

如果后面你安装了真正独立的 Hermes runtime，可以再改 [mvp.config.json](/C:/Users/CaesarWang/Documents/New%20project/mvp.config.json) 里的 worker 配置。
