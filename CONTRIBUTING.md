# Git Commit 规范

> 本项目遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/) 规范。
> 统一的提交信息便于回溯历史、自动生成 CHANGELOG 与版本号。

## 目录

- [提交格式](#提交格式)
- [Header：类型与范围](#header类型与范围)
- [Body 与 Footer](#body-与-footer)
- [示例](#示例)
- [正反例对比](#正反例对比)
- [工具链](#工具链)
- [其他约定](#其他约定)

## 提交格式

```
<type>(<scope>): <subject>
<空行>
<body>
<空行>
<footer>
```

- **Header**（必填）：一行说明本次提交的核心内容
- **Body**（可选）：说明**为什么改**、**改了什么**
- **Footer**（可选）：关联 Issue、标注不兼容变更

## Header：类型与范围

### type（必填）

| 类型 | 说明 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | 修复 bug |
| `docs` | 仅文档变更 |
| `style` | 代码格式（空格、分号、缩进等，不影响逻辑） |
| `refactor` | 重构（既非新增功能也非修复 bug） |
| `perf` | 性能优化 |
| `test` | 增加或修改测试 |
| `build` | 构建系统或依赖变更（webpack、npm 等） |
| `ci` | CI 配置与脚本变更 |
| `chore` | 杂项（不修改 src 或 test） |
| `revert` | 回滚之前的提交 |

### scope（可选）

表示影响范围，通常是模块名、组件名或目录名：

```
feat(user): ...
fix(api): ...
docs(readme): ...
```

无明确范围时可省略。

### subject（必填）

- 使用**祈使句、现在时**：`add` 而非 `added` / `adds`
- **首字母小写**，**结尾不加句号**
- 简明扼要，建议不超过 50 个字符
- 中文项目可用中文描述，但需团队统一

## Body 与 Footer

### Body（可选）

- 与 Header 之间空一行
- 说明动机与影响，而非逐行解释代码
- 可分点、可多段

### Footer（可选）

- **不兼容变更**：以 `BREAKING CHANGE:` 开头，说明影响与迁移方式
- **关联 Issue**：`Closes #123`、`Fixes #456`、`Refs #789`

## 示例

### 新功能

```
feat(auth): add JWT token refresh mechanism

Refresh token 有效期为 7 天，access token 过期后自动续期，
避免用户频繁重新登录。

Closes #128
```

### 修复 bug

```
fix(cart): correct total price calculation with discount

折扣叠加时原价被重复计算，导致总价偏高。

Fixes #231
```

### 不兼容变更

```
refactor(api): extract request interceptor into module

BREAKING CHANGE: 移除 request.js 的默认导出，改为具名导出
createRequest，调用方需更新引入方式。
```

### 文档更新

```
docs: update README installation steps
```

## 正反例对比

| ❌ 不推荐 | ✅ 推荐 |
| --- | --- |
| `update code` | `fix(order): handle null address` |
| `fix bug` | `fix(login): prevent submit on empty password` |
| `修改` | `feat(search): support fuzzy matching` |
| `feat: Added new button.` | `feat(ui): add export button to toolbar` |
| `WIP` | `feat(profile): add avatar upload (WIP)` |

## 工具链

### 1. 安装依赖

```bash
npm i -D @commitlint/cli @commitlint/config-conventional husky
npx husky init
```

### 2. 配置 commitlint

`commitlint.config.js`：

```js
module.exports = {
  extends: ['@commitlint/config-conventional'],
};
```

### 3. 配置 husky 钩子

`.husky/commit-msg`：

```bash
npx --no -- commitlint --edit "$1"
```

### 4. 可选工具

- [commitizen](https://github.com/commitizen/cz-cli)：交互式生成提交信息
- [standard-version](https://github.com/conventional-changelog/standard-version)：根据提交自动生成 CHANGELOG 与版本号
- [semantic-release](https://github.com/semantic-release/semantic-release)：全自动发布流水线

## 其他约定

- 一次提交只做一件事，避免巨型提交
- 不要提交被注释掉的代码、调试语句与临时文件
- 合并分支建议使用 `--no-ff` 保留历史（团队可约定）
- 禁止直接向 `main` / `master` 强推，统一通过 PR 合并
- 提交前先 `pull --rebase`，保持线性历史

---

> 核心原则：**让每次提交都能被读懂、被回溯、被工具解析。**

## 参考

- [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)
- [Angular Commit Message Guidelines](https://github.com/angular/angular/blob/main/CONTRIBUTING.md#commit)