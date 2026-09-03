# MineWorker

一个上手简单、结构清晰的 Python 爬虫框架，对标 [feapder](https://github.com/Boris-code/feapder)：
用户只写 `start_requests` + `parse`，框架负责调度、下载、重试、去重、批量落库。

> **状态：阶段 00（Pre-Alpha）**。当前仅有配置系统、日志、工具集与包骨架。
> 路线图见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

## 开发环境（conda）

```bash
conda env create -f environment.yml
conda activate mineworker
pre-commit install
```

`environment.yml` 会用 pip 以可编辑方式安装 `.[dev]`（含全部可选依赖 + 测试 / lint 工具）。

## 常用命令

```bash
pytest                    # 单元测试
ruff check . && ruff format --check .
mypy                      # 严格类型检查
pre-commit run --all-files
```

CI（GitHub Actions）用 `actions/setup-python` + pip 跑同一套 `pyproject.toml`，
矩阵覆盖 Python 3.10–3.13；conda 只是本地开发环境的选择。

## 目录结构

```
mineworker/
├── setting.py          分层配置（默认 ← 项目 setting.py ← 环境变量 MINEWORKER_*）
├── exceptions.py       异常层级
├── utils/              日志（loguru）、工具函数、指标
├── network/            Request / Response / Downloader        （阶段 01）
├── core/               Scheduler / Collector / ParserWorker    （阶段 02）
├── buffer/             RequestBuffer / ItemBuffer              （阶段 02–03）
├── dedup/              Dedup 门面 + 布隆 / 精确过滤器           （阶段 03）
├── pipelines/          Console / CSV / Mongo                   （阶段 03）
├── db/                 MongoDB（阶段 03）/ Redis（v2）
├── commands/           CLI                                     （阶段 05）
└── templates/          脚手架模板                              （阶段 05）
```

## License

[MIT](LICENSE)
