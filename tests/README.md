# tests

```
python tests/run_all.py            # 全量（默认跳过 live 测试）
python tests/run_all.py --live     # 连 live 测试一起跑
python tests/cases/t_xxx.py        # 单个测试
```

运行测试时不要开着 Nano：运行器会检查 `data/` 有没有被改动，Nano 运行时写入的文件会被当成测试写了真实数据。

## 目录

| 位置 | 内容 |
|---|---|
| `run_all.py` | 运行器：逐个在独立进程里跑 `cases/t_*.py`，汇总结果，检查真实数据目录未被改动 |
| `cases/` | 测试用例，一个文件一个领域，文件头 docstring 写明它验证什么 |
| `_console.py` | 每个用例第一个导入它：控制台编码保护，并启用数据沙盒 |
| `_sandbox.py` | 数据沙盒：每个测试进程使用自己的临时数据目录 |
| `_src.py` | 读产品源码的唯一入口（按模块名，兼容拆成包的模块） |
| `_win_window.py` | 需要真实窗口的测试用的辅助进程 |

## 新增一个测试

1. 在 `cases/` 下新建 `t_<领域>.py`，文件头 docstring 写清验证什么。
2. 在导入任何产品模块之前 `import tests._console`。
3. 读产品源码用 `tests._src`，不按路径读文件；测试需要的素材自己造，不读 docs 或真实数据。
4. 需要真实桌面、真实模型等外部环境的测试，在文件前 30 行里加一行 `# nano-test: live`，默认跳过。
