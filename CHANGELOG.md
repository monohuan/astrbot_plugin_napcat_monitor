# Changelog

本项目的所有重要变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.0] - 2026-09-03
### Added
- 初始版本：监控 OneBot v11 / NapCat 连接状态，掉线或恢复后通过邮件通知管理员。
- 命令：`status` / `list` / `test` / `selftest` / `fake_offline` / `fake_online`。
- SMTP 发送（SSL / STARTTLS / none），发送失败重试与同状态冷却机制。
- 掉线 / 恢复邮件的主题、正文模板自定义。
- 纯标准库实现（`smtplib` / `ssl` / `email`），无第三方依赖。
