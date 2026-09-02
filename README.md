# astrbot_plugin_napcat_monitor

一个给 AstrBot 用的 **NapCat / OneBot v11 掉线邮件通知** 插件。

它是 [astrbot_plugin_napcat_offline_notice](https://github.com/RabbitZheng4424/astrbot_plugin_napcat_offline_notice) 的邮件版变体：

- 沿用原插件对 `aiocqhttp` 平台连接数的**轮询检测逻辑**（`连接数 > 0` 在线，`= 0` 离线）；
- 但把"向管理员跨平台会话推送"改为**直接发送一封邮件给管理员邮箱**。

这样即使 QQ（NapCat）侧已经静默失联，你也能在收件箱里第一时间收到提醒。

## 适用场景

- 你用 NapCat 把 QQ 接进了 AstrBot；
- 你希望 NapCat 被踢下线 / 断网 / 进程退出时，**通过邮件**通知你自己（admin）；
- 不依赖其他 IM 平台是否还在线，邮件链路独立可用。

## 安装

把插件目录放到 AstrBot 的插件目录下：

```
data/plugins/astrbot_plugin_napcat_monitor
```

然后在 AstrBot WebUI 的插件管理中**启用或重载**插件。

## 配置项（插件配置 / `_conf_schema.json`）

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `target_platform_ids` | 空 | 监控的 `aiocqhttp` 平台 ID，每行或逗号分隔；留空 = 监控全部 |
| `poll_interval_seconds` | `5` | 轮询连接状态的间隔（秒） |
| `offline_cooldown_seconds` | `600` | 同一平台相同状态的重复通知冷却（秒） |
| `notify_recovery` | `true` | 恢复连接后是否也发邮件 |
| `smtp_host` | 空 | SMTP 服务器地址，如 `smtp.qq.com` |
| `smtp_port` | `465` | SMTP 端口（465=SSL，587=STARTTLS） |
| `smtp_user` | 空 | SMTP 登录账号（通常是发件邮箱） |
| `smtp_password` | 空 | SMTP 授权码 / 密码 |
| `smtp_security` | `ssl` | 加密方式：`ssl` / `starttls` / `none` |
| `smtp_timeout` | `15` | SMTP 连接超时（秒） |
| `from_name` | 空 | 发件人显示名称，留空则用 `smtp_user` |
| `admin_emails` | 空 | 收件人邮箱，多个用逗号或换行分隔 |
| `subject_prefix` | `[AstrBot]` | 邮件主题前缀 |
| `offline_subject_template` | 内置 | 掉线邮件主题模板，占位符 `{platform_id} {status_text} {detail}` |
| `recovery_subject_template` | 内置 | 恢复邮件主题模板 |
| `offline_template` | 内置 | 掉线邮件正文模板，占位符含 `{time}` |
| `recovery_template` | 内置 | 恢复邮件正文模板 |
| `email_retry_times` | `2` | 发送失败后的最大重试次数 |
| `email_retry_interval` | `5` | 重试间隔（秒） |

> 模板使用 Python `str.format`，例如正文里写 `时间：{time}`、`平台：{platform_id}`。

## 常见邮箱 SMTP 配置参考

| 服务商 | smtp_host | smtp_port | smtp_security | 密码说明 |
| --- | --- | --- | --- | --- |
| QQ 邮箱 | `smtp.qq.com` | `465` | `ssl` | 用「授权码」而非登录密码 |
| 163 邮箱 | `smtp.163.com` | `465` | `ssl` | 用「授权码」 |
| Gmail | `smtp.gmail.com` | `587` | `starttls` | 用应用专用密码 |
| 企业微信 / 自建 | 你的 SMTP | 按实际 | 按实际 | —— |

> ⚠️ 多数邮箱**不允许直接用登录密码发信**，需要在邮箱设置里开启 SMTP 并生成「授权码」填入 `smtp_password`。

## 命令（默认需管理员权限）

| 命令 | 说明 |
| --- | --- |
| `/napcat_monitor status` | 查看监控范围、SMTP、收件人及当前各平台在线状态 |
| `/napcat_monitor list` | 查看邮件配置（密码已隐藏） |
| `/napcat_monitor test` | 向 `admin_emails` 发一封测试邮件，验证链路 |
| `/napcat_monitor selftest` | 一键自检：SMTP 配置 + 平台状态 + 发送测试邮件 |
| `/napcat_monitor fake_offline [平台ID]` | 假装指定或全部平台离线（测试用，无需真断网） |
| `/napcat_monitor fake_online [平台ID]` | 取消假装，恢复真实状态 |

## 快速开始

1. 启用插件；
2. 在插件配置里填写 `smtp_host` / `smtp_user` / `smtp_password` / `admin_emails`；
3. 以管理员身份发送 `/napcat_monitor selftest`，一键确认配置与邮件链路；
4. 发送 `/napcat_monitor fake_offline`，确认能收到掉线提醒邮件；
5. 用 `/napcat_monitor fake_online` 恢复。

## 工作原理

1. 后台任务按 `poll_interval_seconds` 轮询所有 `aiocqhttp` 平台的连接数；
2. 当某平台连接数从 `>0` 变为 `=0`（在线→离线）时，触发掉线通知；
3. 离线→在线时，若 `notify_recovery` 开启则发送恢复通知；
4. 通知通过 `smtplib`（标准库，无需额外依赖）按配置发送邮件给 `admin_emails`；
5. 同一平台相同状态的通知受 `offline_cooldown_seconds` 冷却，避免刷屏；
6. 发送失败按 `email_retry_times` / `email_retry_interval` 重试。

## 注意事项

- 管理员身份以 AstrBot 的 `event.is_admin()` / `admins_id` 校验命令权限，但**邮件发送只看 `admin_emails` 配置**，无需机器人先记住任何会话；
- 若 `smtp_host` 或 `admin_emails` 为空，插件会跳过发送并记录警告日志；
- 如果未来 AstrBot 内部 `aiocqhttp` 连接实现变化，检测逻辑可能需要适配更新。

## 仓库结构

本仓库根目录即插件本体（AstrBot 直接把本仓库克隆 / 软链到 `data/plugins/astrbot_plugin_napcat_monitor` 即可加载）：

```
├─ __init__.py
├─ _conf_schema.json
├─ main.py
├─ metadata.yaml
├─ requirements.txt
└─ README.md
```
