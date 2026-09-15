# tooldelta-plugin-hygiene

ToolDelta 插件的代码卫生治理工作区。本仓库只用于 **DeepSource 静态分析 + 问题修复**，
不是插件市场的一部分。

## 包含内容

| 目录 | 说明 |
|---|---|
| `前置_循环获取玩家坐标/` | 待审查插件（118 行） |
| `『Libra-天秤座』玩家权限管理系统/` | 待审查插件（10 个模块，2145 行） |

## 配置

`.deepsource.toml` 启用 Python 分析器：

- `max_line_length = 100`
- `skip_doc_coverage = ["magic", "init"]`

## 为什么单独建仓库

官方插件市场仓库在推送 `main` 时会触发 `CD.yml`，自动重建目录 JSON 并同步到线上，
因此治理工作在本仓库隔离进行，避免影响线上插件市场。

## 回归测试

`『Libra-天秤座』玩家权限管理系统` 有配套的 `unittest` 测试（不依赖 ToolDelta 运行时），
测试文件保留在插件市场仓库的 `tests/` 目录下，本地运行：

```
python -m unittest tests.test_libra_runtime
python -m unittest tests.test_libra_realtime
```
