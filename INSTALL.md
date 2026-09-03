# Scout Windows 安装契约

这份文件主要给 Codex 阅读。目标是让用户只提供仓库地址，Codex 就能把 Scout 安全、可验证地
安装到用户自己的 Windows PC。

## 边界

- 第一阶段只正式支持 Windows 10/11。
- 默认程序目录：`%LOCALAPPDATA%\Scout\app`。
- 默认数据目录：`%LOCALAPPDATA%\Scout`；其中 `data`、`codex-home` 和 `logs` 在更新时必须保留。
- 不复制用户现有的 Codex 凭据，不修改用户现有的 `%USERPROFILE%\.codex\config.toml`。
- 不要求 API key。用户在安装过程中为 Scout 的独立 Codex 目录完成一次浏览器登录。
- 不安装 Docker，不开放局域网或公网端口，只监听 `127.0.0.1:8765`。
- 不删除已有文件。发现同名目录有本地改动时停止并向用户说明。

## Codex 应执行的步骤

1. 确认系统是 Windows，说明将使用的程序目录和数据目录。
2. 检查 Git 和 Python 3.10+。缺少 Git 时使用官方 Git for Windows；缺少 Python 时优先使用
   `winget install --id Python.Python.3.12 -e`。安装系统依赖前先告诉用户将要安装什么。
3. 仓库不存在时：

   ```powershell
   git clone https://github.com/hesoyamlp1/scout-local "$env:LOCALAPPDATA\Scout\app"
   ```

   仓库已存在时先运行 `git status --short`。只有工作区干净时才执行 `git pull --ff-only`；
   不覆盖、不清理用户改动。
4. 在仓库根目录执行：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
   ```

5. 安装脚本会创建仓库内的 `.venv`、安装固定依赖与 Playwright Chromium、初始化本地目录，
   然后打开 Codex 登录。让用户亲自完成浏览器授权。
6. 安装脚本最后会启动 Scout 并运行 doctor。只有 doctor 全部为 `[OK]`，且
   `http://127.0.0.1:8765` 能打开，才能报告安装成功。
7. 最终告诉用户：本地页面、数据目录，以及 `start`、`doctor`、`stop` 三条命令。

## 更新

先停止 Scout，确认 Git 工作区干净，再快进更新并重新运行安装脚本：

```powershell
.\scout.ps1 stop
git pull --ff-only
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

更新不得删除 `%LOCALAPPDATA%\Scout\data` 或 `codex-home`。

## 故障处理

先运行：

```powershell
.\scout.ps1 doctor
```

只读取 doctor 输出以及 `%LOCALAPPDATA%\Scout\logs` 中对应日志。不要读取书架数据库正文或
Codex 登录凭据。没有用户明确授权时，不删除数据目录、不重装全局 Codex。
