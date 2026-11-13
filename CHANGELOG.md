# Changelog

所有重要的更改都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
并且遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## 【0.0.3】 - 2026-11-13

### 优化

- perf(recall): 召回改用 low 预算，并把返回大小交还给服务端默认

### 修复

 - fix(recall): 注入记忆前先清掉自己上一轮的片段，避免重复叠加

### 重构

 - refactor：统一各处 bank_id 默认值；插件主类重命名（历史遗留问题）
 - docs：添加 CHANGLOG.md 文件

## [0.0.2] - 2026-11-11

0.0.2是0.0.1 的补丁版本，聚焦修复bug；不引入新的运行时依赖，也没有强制迁移步骤。

### 新增

- **数据库结构：**用 PRAGMA user_version 记录表结构版本

### 修复

- fix(sync): 三路触发的同步改为串行，避免并发投递同一批记录
- fix(sync): 文档级时间戳固定为当天零点，避免被逐次 append 覆盖
- fix(sync): 提交前先落库 operation_id，消除崩溃后的重复追加

### 重构

- refactor(recall): 召回记忆改注入用户消息，避免破坏前缀缓存。
- docs: README.md 如实说明按内容去重的代价；更一步准确书写插件简短描述

### 升级说明

- 无需修改配置，也没有强制迁移步骤。
  
