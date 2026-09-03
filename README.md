# Scout Local

Scout 是一个免费的本地联网阅读助手。你告诉它想看什么，它会寻找完整原文、逐篇翻成中文，
并把正文、译文、批注和阅读记忆留在你自己的电脑上。

**你提供自己的 Codex，Scout 提供阅读产品。**Scout 不出售模型额度，也不要求 OpenAI API key；
实际使用受你自己的 ChatGPT/Codex 方案与额度限制。

## 最简单的安装方式

第一阶段正式支持 Windows 10/11。打开已经登录的 Codex，把下面这句话交给它：

> 请把 https://github.com/hesoyamlp1/scout-local 安装到这台 Windows PC。先完整阅读仓库里的 INSTALL.md，严格按它操作；安装前告诉我会改哪些路径，安装后必须运行 Scout doctor 并打开本地页面。

Codex 会处理 Git、Python 和 Scout 的安装步骤。过程中会打开一次浏览器，让你授权 Scout 使用
你自己的 Codex 账户。Scout 使用独立的本地登录目录，不会复制或改写你平常使用的 Codex 配置。

安装完成后，Scout 默认打开：<http://127.0.0.1:8765>

## 它能做什么

- 用自然语言寻找值得读的内容，而不是只返回链接；
- 判断文章页、目录页、残篇和连续章节；
- 把完整正文保存到本地，逐段生成中文译文；
- 在中文、对照和原文视图之间切换；
- 围绕当前文章继续提问和写批注；
- 关注公开来源，之后检查有没有新内容。

## 日常命令

通常直接让 Codex 执行即可：

```powershell
.\scout.ps1 start
.\scout.ps1 status
.\scout.ps1 doctor
.\scout.ps1 restart
.\scout.ps1 stop
```

程序与用户数据彼此分开。更新源码不会删除书架；卸载数据前必须由用户明确确认。

## 隐私

书架、原文、译文、批注和记忆默认只保存在本机。为了完成搜索、判断、翻译和问答，相关任务内容
会发送给用户自己登录的 Codex；抓取公开网页时，Scout 会直接访问对应网站。详见
[PRIVACY.md](docs/PRIVACY.md)。

## 当前状态

这是 Windows 本地版的早期公开版本。Windows 是正式验收平台；macOS 与 Linux 暂未承诺安装体验。

## License

Apache-2.0
