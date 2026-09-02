from __future__ import annotations

import asyncio
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_napcat_monitor",
    "Tyrkb",
    "监控 OneBot v11/NapCat 连接状态，在掉线或恢复后通过邮件通知管理员。",
    "0.1.0",
)
class NapcatMonitorPlugin(Star):
    """NapCat 掉线邮件通知插件（邮件版）。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.monitor_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_platform_status: dict[str, dict[str, Any]] = {}
        self._forced_offline_platforms: set[str] = set()  # 本地测试：假装这些平台离线

    async def initialize(self):
        """启动后台监控任务。"""
        self._stop_event = asyncio.Event()
        await self._seed_platform_status()
        self.monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name="napcat_monitor",
        )
        logger.info(
            "[NapcatMonitor] 插件已启动，监控范围: %s",
            self._format_monitored_platform_hint(),
        )

    async def terminate(self):
        """停止后台监控任务。"""
        self._stop_event.set()
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None

    # ---------------- 命令 ----------------
    @filter.command_group("napcat_monitor")
    def napcat_monitor(self):
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @napcat_monitor.command("status")
    async def show_status(self, event: AstrMessageEvent):
        """查看监控状态。"""
        monitored = self._get_monitored_platforms()
        rows = self._collect_platform_status_rows(monitored)
        configured_ids = sorted(self._configured_platform_ids())
        missing = [
            pid
            for pid in configured_ids
            if pid not in {p.meta().id for p in monitored}
        ]
        forced = sorted(self._forced_offline_platforms)

        lines = [
            "NapCat 掉线邮件通知状态：",
            f"- 监控范围: {self._format_monitored_platform_hint()}",
            f"- 轮询间隔: {self._poll_interval_seconds()} 秒",
            f"- 通知冷却: {self._cooldown_seconds()} 秒",
            f"- 恢复通知: {'开启' if self._notify_recovery() else '关闭'}",
            f"- SMTP: {self._smtp_host() or '(未配置)'}",
            f"- 收件人: {', '.join(self._admin_emails()) or '(未配置)'}",
        ]
        if rows:
            lines.append("- 当前平台状态：")
            lines.extend(rows)
        else:
            lines.append("- 当前没有匹配到可监控的 aiocqhttp 平台实例。")
        if forced:
            lines.append("- 以下平台正在被'假装离线'（测试用）：")
            lines.extend(f"  - {pid}" for pid in forced)
        if missing:
            lines.append("- 以下配置的平台 ID 当前不存在：")
            lines.extend(f"  - {pid}" for pid in missing)

        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @napcat_monitor.command("list")
    async def list_config(self, event: AstrMessageEvent):
        """查看当前邮件配置（密码已隐藏）。"""
        lines = [
            "NapCat 掉线邮件通知配置：",
            f"- SMTP 主机: {self._smtp_host() or '(空)'}",
            f"- SMTP 端口: {self._smtp_port()}",
            f"- SMTP 安全: {self._smtp_security()}",
            f"- SMTP 账号: {self._smtp_user() or '(空)'}",
            f"- SMTP 密码: {'*' * 8 if self._smtp_password() else '(空)'}",
            f"- 发件人名称: {self._from_name() or '(默认)'}",
            f"- 收件人: {', '.join(self._admin_emails()) or '(空)'}",
            f"- 主题前缀: {self._subject_prefix()}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @napcat_monitor.command("test")
    async def send_test(self, event: AstrMessageEvent):
        """向 admin_emails 发送一封测试邮件。"""
        if not self._smtp_host():
            yield event.plain_result("未配置 smtp_host，无法发送测试邮件。")
            return
        if not self._admin_emails():
            yield event.plain_result(
                "未配置 admin_emails，无法发送测试邮件。请先在插件配置中填写收件人邮箱。"
            )
            return

        yield event.plain_result("正在发送测试邮件...")
        subject = f"{self._subject_prefix()} 邮件通知测试"
        ok = await self._send_email_with_retry(
            subject,
            "这是一封来自 AstrBot「NapCat 掉线邮件通知」插件的测试邮件。\n\n"
            "如果您收到了这封邮件，说明 SMTP 配置与邮件链路已打通。",
        )
        yield event.plain_result(
            "测试邮件已成功发送，请检查收件箱。" if ok
            else "测试邮件发送失败，请查看 AstrBot 日志排查 SMTP 配置。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @napcat_monitor.command("selftest")
    async def self_test(self, event: AstrMessageEvent):
        """一键自检：检查 SMTP 配置、监控平台状态并发送测试邮件。"""
        yield event.plain_result("正在执行自检...")

        lines = ["NapCat 掉线邮件通知 · 自检报告："]
        lines.append("[1] SMTP 配置检查")
        all_config_ok = True
        for label, ok, detail in self._collect_config_checks():
            all_config_ok = all_config_ok and ok
            lines.append(f"  - [{'OK' if ok else '缺失'}] {label}: {detail}")

        monitored = self._get_monitored_platforms()
        configured_ids = sorted(self._configured_platform_ids())
        missing = [
            pid
            for pid in configured_ids
            if pid not in {p.meta().id for p in monitored}
        ]
        lines.append("[2] 平台监控检查")
        lines.append(f"  - 监控范围: {self._format_monitored_platform_hint()}")
        rows = self._collect_platform_status_rows(monitored)
        lines.extend(rows if rows else ["  - 未匹配到可监控的 aiocqhttp 平台实例"])
        if missing:
            lines.append("  - 配置但未匹配到的平台: " + ", ".join(missing))

        lines.append("[3] 邮件链路检查")
        if not all_config_ok:
            lines.append("  - 跳过：SMTP 或收件人配置不完整")
        else:
            email_ok = await self._send_email_with_retry(
                f"{self._subject_prefix()} 自检测试邮件",
                "这是一封来自 AstrBot「NapCat 掉线邮件通知」插件的自检测试邮件。\n\n"
                "如果您收到了这封邮件，说明 SMTP 配置与邮件链路已打通。",
            )
            lines.append("  - 发送测试邮件: " + ("成功" if email_ok else "失败"))

        lines.append("自检完成。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @napcat_monitor.command("fake_offline")
    async def fake_platform_offline(self, event: AstrMessageEvent):
        """假装某个（或所有）NapCat 平台离线，用于本地测试。"""
        payload = self._extract_subcommand_payload(
            event.message_str, "napcat_monitor fake_offline"
        )
        if payload:
            target_ids = {
                p.strip() for p in payload.replace(",", " ").split() if p.strip()
            }
        else:
            target_ids = {p.meta().id for p in self._get_monitored_platforms()}

        if not target_ids:
            yield event.plain_result("没有找到可监控的 aiocqhttp 平台实例。")
            return

        self._forced_offline_platforms.update(target_ids)
        lines = ["已将以下平台标记为'假装离线'（下次轮询会触发邮件通知）："]
        lines.extend(f"  - {pid}" for pid in sorted(target_ids))
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @napcat_monitor.command("fake_online")
    async def fake_platform_online(self, event: AstrMessageEvent):
        """取消假装离线，恢复真实连接状态。"""
        payload = self._extract_subcommand_payload(
            event.message_str, "napcat_monitor fake_online"
        )
        if payload:
            target_ids = {
                p.strip() for p in payload.replace(",", " ").split() if p.strip()
            }
        else:
            target_ids = set(self._forced_offline_platforms)

        if not target_ids:
            yield event.plain_result("当前没有平台被标记为'假装离线'。")
            return

        removed = target_ids & self._forced_offline_platforms
        for pid in removed:
            self._forced_offline_platforms.discard(pid)

        lines = ["已取消以下平台的'假装离线'标记："]
        lines.extend(f"  - {pid}" for pid in sorted(removed))
        yield event.plain_result("\n".join(lines))

    # ---------------- 监控循环 ----------------
    async def _monitor_loop(self):
        while not self._stop_event.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[NapcatMonitor] 监控循环出错: %s", exc)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_seconds(),
                )
            except asyncio.TimeoutError:
                continue

    async def _seed_platform_status(self):
        self._last_platform_status = {}
        for platform in self._get_monitored_platforms():
            platform_id = platform.meta().id
            connection_count = self._get_connection_count(platform)
            self._last_platform_status[platform_id] = {
                "online": self._is_platform_online(platform_id, connection_count),
                "connection_count": connection_count,
                "updated_at": int(time.time()),
            }

    async def _poll_once(self):
        for platform in self._get_monitored_platforms():
            platform_id = platform.meta().id
            connection_count = self._get_connection_count(platform)
            is_online = self._is_platform_online(platform_id, connection_count)
            previous = self._last_platform_status.get(platform_id)
            self._last_platform_status[platform_id] = {
                "online": is_online,
                "connection_count": connection_count,
                "updated_at": int(time.time()),
            }

            if previous is None:
                continue
            if bool(previous.get("online")) == is_online:
                continue

            previous_count = int(previous.get("connection_count", 0))
            if is_online:
                if not self._notify_recovery():
                    logger.info(
                        "[NapcatMonitor] %s 已恢复连接，但恢复通知已关闭。",
                        platform_id,
                    )
                    continue
                detail = (
                    f"连接数从 {previous_count} 恢复到 {connection_count}，"
                    "QQ 侧消息现在应该已经恢复。"
                )
                await self._handle_status_change("recovery", platform_id, detail)
            else:
                detail = (
                    f"连接数从 {previous_count} 变为 {connection_count}，"
                    "这通常表示 NapCat 已断开，可能是被踢下线、断网或进程退出。"
                )
                await self._handle_status_change("offline", platform_id, detail)

    async def _handle_status_change(self, status: str, platform_id: str, detail: str):
        if not await self._should_send_notification(status, platform_id):
            logger.info(
                "[NapcatMonitor] %s %s 通知处于冷却时间内，跳过发送。",
                platform_id,
                status,
            )
            return

        logger.info(
            "[NapcatMonitor] 检测到 %s 状态变化: %s",
            platform_id,
            status,
        )
        ok = await self._notify_email(status, platform_id, detail)
        if ok:
            await self._mark_notification_sent(status, platform_id)

    async def _notify_email(self, status: str, platform_id: str, detail: str) -> bool:
        recipients = self._admin_emails()
        if not recipients:
            logger.warning(
                "[NapcatMonitor] 未配置收件人邮箱（admin_emails），跳过邮件发送。"
            )
            return False
        if not self._smtp_host():
            logger.warning(
                "[NapcatMonitor] 未配置 SMTP 主机，跳过邮件发送。"
            )
            return False

        subject = self._build_subject(status, platform_id, detail)
        body = self._build_body(status, platform_id, detail)
        return await self._send_email_with_retry(subject, body)

    # ---------------- 邮件发送 ----------------
    async def _send_email_with_retry(self, subject: str, body: str) -> bool:
        attempts = max(1, self._email_retry_times())
        interval = self._email_retry_interval()
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.to_thread(self._sync_send_email, subject, body)
                logger.info(
                    "[NapcatMonitor] 邮件发送成功（第 %d 次尝试）。", attempt
                )
                return True
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "[NapcatMonitor] 第 %d 次发送失败: %s", attempt, exc
                )
                if attempt < attempts:
                    await asyncio.sleep(interval)
        logger.error("[NapcatMonitor] 邮件发送最终失败: %s", last_error)
        return False

    def _sync_send_email(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._from_header()
        msg["To"] = ", ".join(self._admin_emails())
        msg["Subject"] = subject
        msg.set_content(body)

        host = self._smtp_host()
        port = self._smtp_port()
        user = self._smtp_user()
        password = self._smtp_password()
        security = self._smtp_security().lower()
        timeout = self._smtp_timeout()

        if security == "ssl":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as server:
                if user:
                    server.login(user, password)
                server.send_message(msg)
        elif security == "starttls":
            context = ssl.create_default_context()
            with smtplib.SMTP(host, port, timeout=timeout) as server:
                server.starttls(context=context)
                if user:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as server:
                if user:
                    server.login(user, password)
                server.send_message(msg)

    # ---------------- 文案 / 模板 ----------------
    def _build_subject(self, status: str, platform_id: str, detail: str) -> str:
        template = self._subject_template(status)
        rendered = self._render_template(
            template,
            platform_id=platform_id,
            status=status,
            status_text=self._status_text(status),
            detail=detail,
        )
        return f"{self._subject_prefix()} {rendered}".strip()

    def _build_body(self, status: str, platform_id: str, detail: str) -> str:
        template = self._body_template(status)
        return self._render_template(
            template,
            platform_id=platform_id,
            status=status,
            status_text=self._status_text(status),
            detail=detail,
            time=self._now_str(),
        )

    def _status_text(self, status: str) -> str:
        if status == "recovery":
            return "NapCat 已恢复连接"
        return "NapCat 已断开连接，可能是被踢下线"

    def _now_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def _render_template(self, template: str, **kwargs: Any) -> str:
        try:
            return str(template).format(**kwargs).strip()
        except Exception:
            return str(template).strip()

    # ---------------- 配置读取 ----------------
    def _config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def _smtp_host(self) -> str:
        return str(self._config("smtp_host", "") or "").strip()

    def _smtp_port(self) -> int:
        try:
            return int(self._config("smtp_port", 465))
        except (TypeError, ValueError):
            return 465

    def _smtp_user(self) -> str:
        return str(self._config("smtp_user", "") or "").strip()

    def _smtp_password(self) -> str:
        return str(self._config("smtp_password", "") or "").strip()

    def _smtp_security(self) -> str:
        v = str(self._config("smtp_security", "ssl") or "ssl").strip().lower()
        if v not in ("ssl", "starttls", "none", "plain"):
            return "ssl"
        return v

    def _smtp_timeout(self) -> int:
        try:
            return max(5, int(self._config("smtp_timeout", 15)))
        except (TypeError, ValueError):
            return 15

    def _from_name(self) -> str:
        return str(self._config("from_name", "") or "").strip()

    def _from_header(self) -> str:
        user = self._smtp_user()
        name = self._from_name()
        if name and user:
            return formataddr((name, user))
        return user or ""

    def _admin_emails(self) -> list[str]:
        raw = str(self._config("admin_emails", "") or "").strip()
        result: list[str] = []
        for part in raw.replace("\r", "\n").replace(",", "\n").split("\n"):
            e = part.strip()
            if e and "@" in e:
                result.append(e)
        return result

    def _collect_config_checks(self) -> list[tuple[str, bool, str]]:
        checks: list[tuple[str, bool, str]] = []
        checks.append(
            ("SMTP 主机 (smtp_host)", bool(self._smtp_host()), self._smtp_host() or "(空)")
        )
        checks.append(
            ("SMTP 端口 (smtp_port)", self._smtp_port() > 0, str(self._smtp_port()))
        )
        checks.append(
            ("SMTP 加密 (smtp_security)", True, self._smtp_security())
        )
        checks.append(
            ("SMTP 账号 (smtp_user)", bool(self._smtp_user()), self._smtp_user() or "(空)")
        )
        checks.append(
            ("SMTP 密码 (smtp_password)", bool(self._smtp_password()),
             "已设置" if self._smtp_password() else "(空)")
        )
        emails = self._admin_emails()
        checks.append(
            ("收件人 (admin_emails)", bool(emails), ", ".join(emails) or "(空)")
        )
        return checks

    def _subject_prefix(self) -> str:
        return str(self._config("subject_prefix", "[AstrBot]") or "").strip()

    def _subject_template(self, status: str) -> str:
        if status == "recovery":
            default = "NapCat 已恢复连接（{platform_id}）"
            return str(self._config("recovery_subject_template", "") or default)
        default = "NapCat 已断开连接（{platform_id}）"
        return str(self._config("offline_subject_template", "") or default)

    def _body_template(self, status: str) -> str:
        if status == "recovery":
            default = (
                "管理员您好，\n\n"
                "AstrBot 监控到 NapCat / OneBot v11 平台「{platform_id}」已恢复连接，"
                "QQ 侧消息恢复正常。\n\n"
                "{detail}\n\n"
                "时间：{time}"
            )
            return str(self._config("recovery_template", "") or default)
        default = (
            "管理员您好，\n\n"
            "AstrBot 监控到 NapCat / OneBot v11 平台「{platform_id}」已断开连接，"
            "QQ 侧消息暂时可能收不到。\n\n"
            "{detail}\n\n"
            "时间：{time}"
        )
        return str(self._config("offline_template", "") or default)

    def _email_retry_times(self) -> int:
        try:
            return max(1, int(self._config("email_retry_times", 2)))
        except (TypeError, ValueError):
            return 2

    def _email_retry_interval(self) -> int:
        try:
            return max(1, int(self._config("email_retry_interval", 5)))
        except (TypeError, ValueError):
            return 5

    # ---------------- 平台监控 ----------------
    def _is_platform_online(self, platform_id: str, real_connection_count: int) -> bool:
        if platform_id in self._forced_offline_platforms:
            return False
        return real_connection_count > 0

    def _get_monitored_platforms(self) -> list[Any]:
        target_ids = self._configured_platform_ids()
        platforms: list[Any] = []
        for platform in self.context.platform_manager.platform_insts:
            meta = platform.meta()
            if meta.name != "aiocqhttp":
                continue
            if target_ids and meta.id not in target_ids:
                continue
            platforms.append(platform)
        return platforms

    def _collect_platform_status_rows(self, platforms: list[Any]) -> list[str]:
        rows: list[str] = []
        for platform in platforms:
            platform_id = platform.meta().id
            real_count = self._get_connection_count(platform)
            is_online = self._is_platform_online(platform_id, real_count)
            if platform_id in self._forced_offline_platforms:
                rows.append(
                    f"  - {platform_id}: {'在线' if is_online else '离线'} "
                    f"(假装离线，真实连接数: {real_count})"
                )
            else:
                rows.append(
                    f"  - {platform_id}: {'在线' if is_online else '离线'} "
                    f"(连接数: {real_count})"
                )
        return rows

    def _configured_platform_ids(self) -> set[str]:
        raw = str(self._config("target_platform_ids", "") or "").strip()
        if not raw or raw == "*":
            return set()
        result: set[str] = set()
        normalized = raw.replace("\r", "\n").replace(",", "\n")
        for item in normalized.split("\n"):
            value = item.strip()
            if value:
                result.add(value)
        return result

    def _get_connection_count(self, platform: Any) -> int:
        bot = getattr(platform, "bot", None)
        if bot is None:
            get_client = getattr(platform, "get_client", None)
            if callable(get_client):
                bot = get_client()

        api_clients = getattr(bot, "_wsr_api_clients", None)
        event_clients = getattr(bot, "_wsr_event_clients", None)

        connection_count = 0
        if isinstance(api_clients, dict):
            connection_count += len(api_clients)
        if isinstance(event_clients, set):
            connection_count += len(event_clients)
        return connection_count

    def _format_monitored_platform_hint(self) -> str:
        configured_ids = sorted(self._configured_platform_ids())
        if not configured_ids:
            return "全部 aiocqhttp / OneBot v11 实例"
        return ", ".join(configured_ids)

    def _poll_interval_seconds(self) -> int:
        try:
            return max(2, int(self._config("poll_interval_seconds", 5)))
        except (TypeError, ValueError):
            return 5

    def _cooldown_seconds(self) -> int:
        try:
            return max(0, int(self._config("offline_cooldown_seconds", 600)))
        except (TypeError, ValueError):
            return 600

    def _notify_recovery(self) -> bool:
        return bool(self._config("notify_recovery", True))

    def _extract_subcommand_payload(self, message: str, command_text: str) -> str:
        normalized = (message or "").strip()
        for prefix in (f"/{command_text}", command_text):
            if normalized.startswith(prefix):
                return normalized[len(prefix):].strip()
        return ""

    # ---------------- 冷却 ----------------
    async def _should_send_notification(self, status: str, platform_id: str) -> bool:
        cooldown = self._cooldown_seconds()
        if cooldown <= 0:
            return True
        key = f"delivered_ts:v1:{status}:{platform_id}"
        last_sent_at = await self.get_kv_data(key, 0)
        try:
            last_sent_at_int = int(last_sent_at or 0)
        except (TypeError, ValueError):
            last_sent_at_int = 0
        return not last_sent_at_int or int(time.time()) - last_sent_at_int >= cooldown

    async def _mark_notification_sent(self, status: str, platform_id: str) -> None:
        key = f"delivered_ts:v1:{status}:{platform_id}"
        await self.put_kv_data(key, int(time.time()))
