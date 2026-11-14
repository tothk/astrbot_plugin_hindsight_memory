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

这是一个纯 Python 仓库，**没有 npm、没有 Node 依赖**。下面的配置都是可选的，仓库里也刻意没有提交 `.husky/` 或 `commitlint.config.js`——本地钩子属于个人环境，不该进版本库。

### 1. 代码检查

```bash
ruff check .
ruff format .
```

### 2. 跑测试

测试要在 AstrBot 的源码环境里跑（原因见 README「开发与测试」）：

```bash
pip install -r requirements-dev.txt
python -m pytest . -q
```

### 3. 可选：本地提交信息校验

想在自己机器上强制 Conventional Commits，可以装 `commitlint`（需要 Node）。装完**不要把生成的钩子提交上去**：

```bash
npm i -D @commitlint/cli @commitlint/config-conventional husky
npx husky init
echo 'npx --no -- commitlint --edit "$1"' > .husky/commit-msg
```

`commitlint.config.js` 内容：

```js
module.exports = {
  extends: ['@commitlint/config-conventional'],
};
```

装好后把 `node_modules/`、`.husky/`、`commitlint.config.js`、`package.json` 都加进 `.gitignore`（如果你确实不想让它们共享）。

### 4. 可选工具

- [commitizen](https://github.com/commitizen/cz-cli)：交互式生成提交信息（同样需要 Node）
- [semantic-release](https://github.com/semantic-release/semantic-release)：全自动发布流水线

> 注意：上面的类型前缀（`feat`/`fix`/`docs`…）是规范，**冒号后的标题写中文就好**。本仓库的历史提交都是中文标题，这不违反约定；一致性比语言重要。

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