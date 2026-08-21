# `command_line_tools_test/offline_env`（离线/可迁移环境目录）

这个目录用于把 **Harmony 工具链测试**在运行时可能需要联网下载的内容（尤其是 `ohpm install` 的依赖包缓存）提前准备好，并在迁移到**无网络环境**后继续复用。

核心思路：

- 每个 ArkTS/Harmony 工程依赖落地目录是工程内的 `oh_modules/`（通常不提交 git）。
- `ohpm install` 实际下载的包会进入 `ohpm` 的 **全局 store/cache**。
- 我们把 `ohpm` 的 store/cache **固定到本仓库内**（本目录下），这样只要拷贝本目录，就能在无网环境中从本地缓存生成各工程的 `oh_modules/`。

> 说明：工程里仍然需要执行 `ohpm install` 来生成 `oh_modules/`，但只要 cache/store 指向本目录，就不需要联网下载。

---

## 目录结构

- `ohpm_store/`：建议作为 ohpm store（包仓库/硬链接源）
- `ohpm_cache/`：建议作为 ohpm cache（下载缓存）
- `scripts/`：准备与切换脚本
  - `prepare_ohpm_cache.ps1`：有网环境运行，配置 ohpm 目录并“预热”依赖
  - `use_offline_ohpm_cache.ps1`：无网环境运行，只做配置切换（不下载）

---

## 快速使用

### A. 在有网环境“准备离线依赖”

在 `command_line_tools_test` 目录运行：

```powershell
pwsh -File .\\offline_env\\scripts\\prepare_ohpm_cache.ps1 `
  -RepoPath \"E:\\WorkApp\\MSWE-agent\\MSWE-agent\\MSWE-agent\\repair_repo\\repo_after_fix\\Media-Audio\" `
  -OhpmExe \"E:\\WorkApp\\DevEco Studio\\tools\\ohpm\\bin\\ohpm.bat\"
```

它会：

- 将 ohpm 的 store/cache 指向本目录下的 `ohpm_store/`、`ohpm_cache/`
- 在你指定的工程目录执行一次 `ohpm install`
- 如果本次 `ohpm install` 没有触发下载（例如工程已存在 `oh_modules/`），脚本会把你机器上原本的 ohpm `cache` 目录内容复制一份到 `offline_env/ohpm_cache/`，保证离线包目录里“确实有东西”

准备完成后，把整个 `command_line_tools_test/offline_env/` 目录拷贝到目标机器即可。

### B. 在无网环境“使用离线依赖”

在 `command_line_tools_test` 目录运行：

```powershell
pwsh -File .\\offline_env\\scripts\\use_offline_ohpm_cache.ps1 `
  -OhpmExe \"E:\\WorkApp\\DevEco Studio\\tools\\ohpm\\bin\\ohpm.bat\"
```

然后进入任意工程根执行：

```powershell
& \"E:\\WorkApp\\DevEco Studio\\tools\\ohpm\\bin\\ohpm.bat\" install
```

此时 `ohpm` 会从本目录 store/cache 取包，不需要联网。

---

## 注意事项

- **不要把工程的 `oh_modules/` 当作离线缓存的唯一来源**：它更像是“项目级依赖展开结果”，体积大且跨项目复用差。推荐用全局 store/cache。
- 如果你们的 DevEco/ohpm 版本在配置项名称上有差异，脚本会尽量兼容；如失败，可执行 `ohpm config list` 并按输出调整脚本中的配置 key。

