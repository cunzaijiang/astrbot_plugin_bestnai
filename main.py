"""AstrBot 插件：BestNAI 图片生成 v2.0.0。

通过 BestNAI API（NovelAI 图片生成）在 AstrBot 中生成 AI 图片。

支持指令：
    /nai <提示词>            - 基础图片生成
    /nai0 <英文tag>          - 直接用英文 tag 生图（跳过翻译）
    /nai_adv <提示词> [参数]  - 高级图片生成（支持自定义参数）
    /nai_set 3|4|4.5         - 切换模型版本
    /nai_size <预设|尺寸>    - 切换尺寸
    /nai_nsfw on/off         - NSFW 开关
    /nai_pt on/off           - 提示词显示开关
    /nai_on / /nai_off       - 会话插件开关
    /nai_recall              - 撤回最后一张图
    /nai_status              - 查看当前会话配置
    /nai_help                - 查看帮助信息
    /nai_cfg                 - 查看当前全局配置（管理员）
"""

import asyncio
import os
import time
import weakref
from dataclasses import replace
from typing import AsyncGenerator, Dict, List, Optional, Set, Tuple

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star

from .constants import SIZE_PRESETS, VERSION_MODELS, NAI_SUBCOMMANDS
from .core.generator import (
    APIKeyError,
    GenerationError,
    ImageGenerator,
    QuotaExceededError,
    RateLimitError,
    ServerBusyError,
)
from .core.session_state import SessionState, get_session_state, set_default_version
from .core.translator import PromptTranslator, has_chinese
from .models.config import GenerationConfig, PluginConfig
from .utils.helpers import (
    apply_nsfw_filter,
    apply_overrides,
    build_final_prompt,
    cleanup_file,
    extract_images_from_content,
    format_cooldown_message,
    format_size_display,
    get_session_key,
    parse_advanced_args,
    resolve_size_preset,
    save_image_to_dir,
    save_image_to_temp,
)


class BestNAIPlugin(Star):
    """BestNAI 图片生成插件 v2.0.0。

    通过 BestNAI API 提供 NovelAI 风格的 AI 图片生成功能。
    支持会话级状态管理、多模型版本切换、画师预设、NSFW 过滤等。

    Attributes:
        plugin_config: 插件配置实例。
        generator: 图片生成器实例。
        cooldown_map: 用户冷却时间记录，格式为 {user_id: last_request_timestamp}。
        _recall_tasks: 弱引用的自动撤回任务集合。
    """

    def __init__(self, context: Context, config: dict) -> None:
        """初始化插件。

        Args:
            context: AstrBot 上下文对象。
            config: 插件配置字典（来自 _conf_schema.json）。
        """
        super().__init__(context)
        self.plugin_config = PluginConfig.from_dict(config)
        self.generator = ImageGenerator(self.plugin_config)
        self.cooldown_map: Dict[str, float] = {}
        self._recall_tasks: weakref.WeakSet = weakref.WeakSet()
        # 将配置里的默认版本注入会话管理器
        default_ver = config.get("default_version", "4.5")
        if default_ver not in ("3", "4", "4.5"):
            default_ver = "4.5"
        set_default_version(default_ver)
        logger.info(
            f"[BestNAI] 插件 v2.0.0 已加载，API URL: {self.plugin_config.api_url or '(未配置)'}，默认版本: NAI {default_ver}"
        )

    async def terminate(self) -> None:
        """插件终止时的清理操作。"""
        self.cooldown_map.clear()
        for task in list(self._recall_tasks):
            task.cancel()
        logger.info("[BestNAI] 插件已卸载")

    # ──── 冷却管理 ─────────────────────────────────────────────────────────────

    def _check_cooldown(self, user_id: str) -> Optional[float]:
        """检查用户是否在冷却中。

        Args:
            user_id: 用户唯一标识。

        Returns:
            剩余冷却时间（秒），如果不在冷却中则返回 None。
        """
        cooldown = self.plugin_config.user_cooldown
        if cooldown <= 0:
            return None
        last_time = self.cooldown_map.get(user_id)
        if last_time is None:
            return None
        elapsed = time.time() - last_time
        remaining = cooldown - elapsed
        return remaining if remaining > 0 else None

    def _update_cooldown(self, user_id: str) -> None:
        """更新用户冷却时间戳。

        Args:
            user_id: 用户唯一标识。
        """
        self.cooldown_map[user_id] = time.time()

    # ──── 撤回管理 ─────────────────────────────────────────────────────────────

    async def _do_recall(self, client, message_id: int, delay: int) -> None:
        """延迟撤回消息。

        Args:
            client: aiocqhttp CQHttp 客户端。
            message_id: 要撤回的消息 ID。
            delay: 延迟秒数。
        """
        await asyncio.sleep(delay)
        try:
            await client.delete_msg(message_id=message_id)
            logger.info(f"[BestNAI] 已撤回消息 {message_id}")
        except Exception as e:
            logger.warning(f"[BestNAI] 撤回消息 {message_id} 失败: {e}")

    def _get_cqhttp_client(self, event: AstrMessageEvent):
        """尝试获取 aiocqhttp 客户端，非 QQ 平台返回 None。

        Args:
            event: AstrBot 消息事件。

        Returns:
            CQHttp 客户端实例，非 QQ 平台返回 None。
        """
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
            if isinstance(event, AiocqhttpMessageEvent):
                return event.bot
        except Exception:
            pass
        return None

    # ──── 图片发送 ─────────────────────────────────────────────────────────────

    async def _send_images(
        self,
        event: AstrMessageEvent,
        images: List[Tuple[str, bytes]],
        gen_config: GenerationConfig,
        session_state: Optional[SessionState] = None,
        prompt: str = "",
    ) -> AsyncGenerator:
        """发送生成的图片，并在配置开启时安排自动撤回。

        同时记录最后一张图的 message_id 到 session_state，用于手动撤回。
        若 session_state.pt_show 为 True，额外发送一条提示词消息。

        Args:
            event: AstrBot 消息事件。
            images: [(image_format, image_bytes), ...] 列表。
            gen_config: 使用的生成配置。
            session_state: 当前会话状态（可选）。
            prompt: 最终使用的正面提示词（用于 pt_show）。

        Yields:
            消息结果。
        """
        save_images = self.plugin_config.save_images
        save_dir = self.plugin_config.save_dir
        auto_recall = self.plugin_config.auto_recall
        recall_delay = self.plugin_config.auto_recall_delay

        cqhttp_client = self._get_cqhttp_client(event)
        is_cqhttp = cqhttp_client is not None

        for idx, (img_format, img_bytes) in enumerate(images):
            temp_path = None
            saved_path = None

            try:
                temp_path = save_image_to_temp(img_bytes, img_format)

                if save_images and save_dir:
                    try:
                        saved_path = save_image_to_dir(img_bytes, save_dir, img_format)
                        logger.info(f"[BestNAI] 图片已保存到: {saved_path}")
                    except Exception as e:
                        logger.warning(f"[BestNAI] 保存图片失败: {e}")

                if auto_recall and is_cqhttp and cqhttp_client:
                    # 底层发送以获取 message_id 用于自动撤回
                    try:
                        import astrbot.api.message_components as Comp
                        from astrbot.core.message.message_event_result import MessageChain

                        cq_img = Comp.Image.fromFileSystem(temp_path)
                        obmsg = await event._parse_onebot_json(MessageChain(chain=[cq_img]))

                        result = None
                        group_id = event.get_group_id()
                        sender_id = event.get_sender_id()
                        if group_id:
                            result = await cqhttp_client.send_group_msg(
                                group_id=int(group_id), message=obmsg
                            )
                        elif sender_id:
                            result = await cqhttp_client.send_private_msg(
                                user_id=int(sender_id), message=obmsg
                            )

                        if result and (msg_id := result.get("message_id")):
                            # 记录到会话状态
                            if session_state is not None:
                                session_state.last_image_message_id = int(msg_id)

                            task = asyncio.create_task(
                                self._do_recall(cqhttp_client, int(msg_id), recall_delay)
                            )
                            self._recall_tasks.add(task)
                            task.add_done_callback(lambda t: self._recall_tasks.discard(t))
                            logger.info(
                                f"[BestNAI] 图片已发送（message_id={msg_id}），"
                                f"将在 {recall_delay} 秒后撤回"
                            )
                        else:
                            yield event.chain_result([Image.fromFileSystem(temp_path)])
                    except Exception as e:
                        logger.warning(f"[BestNAI] 底层发送失败，降级为普通发送: {e}")
                        yield event.chain_result([Image.fromFileSystem(temp_path)])
                else:
                    # 普通发送（如果是 QQ 平台且有 cqhttp_client，仍尝试记录 message_id）
                    if is_cqhttp and cqhttp_client and session_state is not None:
                        try:
                            import astrbot.api.message_components as Comp
                            from astrbot.core.message.message_event_result import MessageChain

                            cq_img = Comp.Image.fromFileSystem(temp_path)
                            obmsg = await event._parse_onebot_json(MessageChain(chain=[cq_img]))

                            result = None
                            group_id = event.get_group_id()
                            sender_id = event.get_sender_id()
                            if group_id:
                                result = await cqhttp_client.send_group_msg(
                                    group_id=int(group_id), message=obmsg
                                )
                            elif sender_id:
                                result = await cqhttp_client.send_private_msg(
                                    user_id=int(sender_id), message=obmsg
                                )

                            if result and (msg_id := result.get("message_id")):
                                session_state.last_image_message_id = int(msg_id)
                        except Exception:
                            # 记录失败不影响正常发送
                            yield event.chain_result([Image.fromFileSystem(temp_path)])
                    else:
                        yield event.chain_result([Image.fromFileSystem(temp_path)])

            except Exception as e:
                logger.error(f"[BestNAI] 发送图片 {idx + 1} 失败: {e}")
                yield event.plain_result(f"❌ 发送图片失败: {e}")
            finally:
                if temp_path and not (save_images and save_dir and saved_path == temp_path):
                    cleanup_file(temp_path)

        # 提示词显示
        if session_state is not None and session_state.pt_show and prompt:
            yield event.plain_result(f"📝 提示词：{prompt}")

    # ──── 核心生图逻辑 ─────────────────────────────────────────────────────────

    async def _do_generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        gen_config: GenerationConfig,
        session_state: Optional[SessionState] = None,
        skip_translation: bool = False,
    ) -> AsyncGenerator:
        """执行核心生图流程（翻译 + 生成 + 发送）。

        Args:
            event: AstrBot 消息事件。
            prompt: 原始提示词（可为中文）。
            gen_config: 生成配置。
            session_state: 当前会话状态（可选）。
            skip_translation: 是否跳过翻译（nai0 使用）。

        Yields:
            状态消息和图片结果。
        """
        # 检查配置
        if not self.plugin_config.is_configured():
            yield event.plain_result(
                "❌ 插件未配置，请在 AstrBot 管理面板中配置 API URL 和 API Key"
            )
            return

        # 检查冷却
        user_id = event.get_sender_id()
        remaining = self._check_cooldown(user_id)
        if remaining is not None:
            yield event.plain_result(format_cooldown_message(remaining))
            return

        final_prompt = prompt

        # 中文翻译
        if not skip_translation:
            translator = PromptTranslator(self.plugin_config.translator)
            tr_cfg = self.plugin_config.translator
            if tr_cfg.enabled and has_chinese(prompt):
                if tr_cfg.show_progress:
                    yield event.plain_result("🔤 正在将中文描述转换为提示词...")
                translated = await translator.translate(prompt)
                if translated != prompt:
                    if tr_cfg.show_result:
                        yield event.plain_result(f"📝 提示词：{translated}")
                    final_prompt = translated
                elif has_chinese(prompt):
                    yield event.plain_result(
                        "⚠️ 翻译失败，请检查翻译器配置，或使用 /nai0 直接输入英文 tag"
                    )
                    return
            elif has_chinese(prompt) and not tr_cfg.enabled:
                yield event.plain_result(
                    "⚠️ 检测到中文描述，但翻译功能未开启。\n"
                    "请在配置中启用 translator_enabled，或使用英文 tag。"
                )
                return

        # 构建最终提示词（画师预设 + 后缀）
        # session_state.artist_preset: "" = 用全局默认, "none" = 不用任何
        if session_state and session_state.artist_preset == "none":
            session_artist = ""
            use_default = False
        elif session_state and session_state.artist_preset:
            session_artist = session_state.artist_preset
            use_default = False
        else:
            session_artist = ""
            use_default = True
        final_prompt = build_final_prompt(
            final_prompt, self.plugin_config,
            artist_preset_name=session_artist if not use_default else self.plugin_config.default_artist_preset
        )

        # NSFW 过滤
        nsfw_enabled = session_state.nsfw_enabled if session_state else False
        neg = apply_nsfw_filter(gen_config.negative_prompt, nsfw_enabled)
        if neg != gen_config.negative_prompt:
            from dataclasses import replace as dc_replace
            gen_config = dc_replace(gen_config, negative_prompt=neg)

        # 发送生成中提示
        size_info = format_size_display(gen_config.width, gen_config.height)
        yield event.plain_result(
            f"🎨 生成中（{size_info} | {gen_config.steps}步）..."
        )

        # 更新冷却
        self._update_cooldown(user_id)

        try:
            images = await self.generator.generate(final_prompt, gen_config)
            # 若 session_state.pt_show 为 True 且翻译器已显示了提示词，避免重复
            # 通过在 _send_images 传入 final_prompt 来控制
            show_prompt = final_prompt
            if (
                not skip_translation
                and self.plugin_config.translator.enabled
                and self.plugin_config.translator.show_result
                and has_chinese(prompt)
            ):
                # 翻译结果已经显示过了，不再重复
                show_prompt = "" if (session_state and session_state.pt_show) else ""
            async for result in self._send_images(
                event, images, gen_config, session_state=session_state, prompt=show_prompt
            ):
                yield result

        except APIKeyError as e:
            yield event.plain_result(f"❌ API Key 错误：{e.message}")
        except QuotaExceededError as e:
            yield event.plain_result(f"❌ {e.message}")
        except RateLimitError as e:
            yield event.plain_result(f"⏳ {e.message}")
        except ServerBusyError as e:
            yield event.plain_result(f"🔄 {e.message}")
        except GenerationError as e:
            logger.error(f"[BestNAI] 生成失败: {e}")
            yield event.plain_result(f"❌ 生成失败：{e.message}")
        except Exception as e:
            logger.exception(f"[BestNAI] 未知错误: {e}")
            yield event.plain_result("❌ 发生未知错误，请稍后重试")

    # ──── /nai 子命令处理器 ────────────────────────────────────────────────────

    async def _handle_set(
        self, event: AstrMessageEvent, args: List[str], state: SessionState
    ) -> AsyncGenerator:
        """处理 /nai set <版本> 子命令。

        Args:
            event: AstrBot 消息事件。
            args: 子命令参数列表（不含 "set"）。
            state: 当前会话状态。

        Yields:
            提示消息。
        """
        if not args:
            yield event.plain_result(
                "❌ 请指定版本，可选：3 / 4 / 4.5\n示例：/nai set 4.5"
            )
            return
        version = args[0].strip()
        if version not in VERSION_MODELS:
            yield event.plain_result(
                f"❌ 不支持的版本：{version}\n可选版本：{' / '.join(VERSION_MODELS.keys())}"
            )
            return
        state.model_version = version
        model_name = VERSION_MODELS[version]
        yield event.plain_result(
            f"✅ 已切换到 NAI V{version}（{model_name}）"
        )

    async def _handle_size(
        self, event: AstrMessageEvent, args: List[str], state: SessionState
    ) -> AsyncGenerator:
        """处理 /nai size <预设|尺寸> 子命令。

        Args:
            event: AstrBot 消息事件。
            args: 子命令参数列表（不含 "size"）。
            state: 当前会话状态。

        Yields:
            提示消息。
        """
        if not args:
            presets_str = "、".join(SIZE_PRESETS.keys())
            yield event.plain_result(
                f"❌ 请指定尺寸，可选预设：{presets_str}\n"
                "或自定义尺寸如：1024x1024\n"
                "示例：/nai size 竖图"
            )
            return
        size_input = args[0].strip()
        try:
            w, h = resolve_size_preset(size_input, SIZE_PRESETS)
        except ValueError:
            presets_str = "、".join(SIZE_PRESETS.keys())
            yield event.plain_result(
                f"❌ 无效的尺寸：{size_input}\n"
                f"可选预设：{presets_str}\n"
                "或使用 宽x高 格式，如 1024x1024"
            )
            return
        state.size_preset = size_input
        yield event.plain_result(
            f"✅ 已切换尺寸为 {size_input}（{w}x{h}）"
        )

    async def _handle_nsfw(
        self, event: AstrMessageEvent, args: List[str], state: SessionState
    ) -> AsyncGenerator:
        """处理 /nai nsfw on/off 子命令。

        Args:
            event: AstrBot 消息事件。
            args: 子命令参数列表（不含 "nsfw"）。
            state: 当前会话状态。

        Yields:
            提示消息。
        """
        if not args or args[0].lower() not in ("on", "off"):
            current = "on" if state.nsfw_enabled else "off"
            current_desc = "已开启（不过滤）" if state.nsfw_enabled else "已关闭（过滤已启用）"
            yield event.plain_result(
                f"❌ 请指定 on 或 off\n当前状态：{current_desc}\n示例：/nai_nsfw on"
            )
            return
        state.nsfw_enabled = args[0].lower() == "on"
        if state.nsfw_enabled:
            status_text = "NSFW 模式已开启 🔞（不过滤内容）"
        else:
            status_text = "NSFW 模式已关闭 🔒（内容过滤已启用）"
        yield event.plain_result(f"✅ {status_text}")

    async def _handle_pt(
        self, event: AstrMessageEvent, args: List[str], state: SessionState
    ) -> AsyncGenerator:
        """处理 /nai pt on/off 子命令。

        Args:
            event: AstrBot 消息事件。
            args: 子命令参数列表（不含 "pt"）。
            state: 当前会话状态。

        Yields:
            提示消息。
        """
        if not args or args[0].lower() not in ("on", "off"):
            current = "on" if state.pt_show else "off"
            yield event.plain_result(
                f"❌ 请指定 on 或 off\n当前状态：{current}\n示例：/nai pt on"
            )
            return
        state.pt_show = args[0].lower() == "on"
        status = "开启" if state.pt_show else "关闭"
        yield event.plain_result(f"✅ 提示词显示已{status}")

    async def _handle_recall(
        self, event: AstrMessageEvent, state: SessionState
    ) -> AsyncGenerator:
        """处理 /nai 撤回 子命令，撤回最后一张图。

        Args:
            event: AstrBot 消息事件。
            state: 当前会话状态。

        Yields:
            提示消息。
        """
        cqhttp_client = self._get_cqhttp_client(event)
        if cqhttp_client is None:
            yield event.plain_result("❌ 当前平台不支持撤回（仅 QQ/aiocqhttp 平台支持）")
            return

        msg_id = state.last_image_message_id
        if msg_id is None:
            yield event.plain_result("❌ 没有可撤回的图片记录")
            return

        try:
            await cqhttp_client.delete_msg(message_id=msg_id)
            state.last_image_message_id = None
            yield event.plain_result("✅ 已撤回最后一张图片")
        except Exception as e:
            yield event.plain_result(f"❌ 撤回失败：{e}")

    async def _handle_status(
        self, event: AstrMessageEvent, state: SessionState
    ) -> AsyncGenerator:
        """处理 /nai status / /nai cfg 子命令，显示当前会话配置。

        Args:
            event: AstrBot 消息事件。
            state: 当前会话状态。

        Yields:
            配置信息文本。
        """
        cfg = self.plugin_config
        version = state.model_version
        model_name = VERSION_MODELS.get(version, "未知")
        gen_cfg = cfg.get_generation_config_for_version(version)

        # 尺寸
        try:
            w, h = resolve_size_preset(state.size_preset, SIZE_PRESETS)
            size_str = f"{state.size_preset}（{w}x{h}）"
        except ValueError:
            size_str = state.size_preset

        presets_str = "、".join(p["name"] for p in cfg.artist_presets if isinstance(p, dict) and "name" in p)

        text = (
            "📊 当前会话配置\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 模型版本：NAI V{version}（{model_name}）\n"
            f"📐 尺寸：{size_str}\n"
            f"🔞 NSFW：{'开启' if state.nsfw_enabled else '关闭'}\n"
            f"📝 提示词显示：{'开启' if state.pt_show else '关闭'}\n"
            f"🔌 插件状态：{'开启' if state.plugin_enabled else '关闭'}\n"
            f"⚙️ 步数（此版本）：{gen_cfg.steps}\n"
            f"🎨 采样器：{gen_cfg.sampler}\n"
        )
        if cfg.artist_presets:
            default_preset = cfg.default_artist_preset or "（无）"
            text += f"🖌️ 画师预设：{presets_str}\n"
            text += f"   默认预设：{default_preset}\n"
        if cfg.prompt_suffix:
            text += f"📎 提示词后缀：{cfg.prompt_suffix}\n"
        yield event.plain_result(text)

    async def _handle_plugin_toggle(
        self, event: AstrMessageEvent, enabled: bool, state: SessionState
    ) -> AsyncGenerator:
        """处理 /nai on / /nai off 子命令。

        Args:
            event: AstrBot 消息事件。
            enabled: True 为开启，False 为关闭。
            state: 当前会话状态。

        Yields:
            提示消息。
        """
        state.plugin_enabled = enabled
        status = "开启" if enabled else "关闭"
        yield event.plain_result(f"✅ 本会话 BestNAI 插件已{status}")

    # ──── 指令入口 ─────────────────────────────────────────────────────────────

    @filter.command("nai")
    async def cmd_nai(self, event: AstrMessageEvent) -> AsyncGenerator:
        """基础图片生成指令。

        直接将消息内容作为提示词进行生图（支持中文自动翻译）。
        空指令显示帮助。

        Args:
            event: AstrBot 消息事件。

        Yields:
            各种响应结果。

        Example:
            /nai 画一张初音未来
            /nai 1girl, masterpiece
        """
        prompt = event.message_str.strip()
        session_key = get_session_key(event)
        state = get_session_state(session_key)

        # 空指令 -> 显示帮助
        if not prompt:
            async for r in self._show_nai_help(event, state):
                yield r
            return

        # ── 会话开关检查 ──
        if not state.plugin_enabled:
            yield event.plain_result("🔌 BestNAI 在本会话已关闭，使用 /nai_on 开启")
            return

        # 根据会话版本获取生成配置，并叠加会话尺寸
        cfg = self.plugin_config
        version = state.model_version
        gen_config = cfg.get_generation_config_for_version(version)

        # 叠加会话级尺寸
        try:
            w, h = resolve_size_preset(state.size_preset, SIZE_PRESETS)
            from dataclasses import replace as dc_replace
            gen_config = dc_replace(gen_config, width=w, height=h)
        except ValueError:
            pass  # 尺寸无效则使用版本默认值

        async for result in self._do_generate(
            event, prompt, gen_config, session_state=state
        ):
            yield result

    @filter.command("nai_set")
    async def cmd_nai_set(self, event: AstrMessageEvent) -> AsyncGenerator:
        """切换会话模型版本。

        Args:
            event: AstrBot 消息事件。

        Yields:
            提示消息。

        Example:
            /nai_set 4.5
        """
        session_key = get_session_key(event)
        state = get_session_state(session_key)
        args = event.message_str.strip().split()[1:]
        async for r in self._handle_set(event, args, state):
            yield r

    @filter.command("nai_size")
    async def cmd_nai_size(self, event: AstrMessageEvent) -> AsyncGenerator:
        """切换会话尺寸。

        Args:
            event: AstrBot 消息事件。

        Yields:
            提示消息。

        Example:
            /nai_size 竖图
            /nai_size 1024x1024
        """
        session_key = get_session_key(event)
        state = get_session_state(session_key)
        args = event.message_str.strip().split()[1:]
        async for r in self._handle_size(event, args, state):
            yield r

    @filter.command("nai_nsfw")
    async def cmd_nai_nsfw(self, event: AstrMessageEvent) -> AsyncGenerator:
        """切换 NSFW 开关。

        Args:
            event: AstrBot 消息事件。

        Yields:
            提示消息。

        Example:
            /nai_nsfw on
            /nai_nsfw off
        """
        session_key = get_session_key(event)
        state = get_session_state(session_key)
        args = event.message_str.strip().split()[1:]
        async for r in self._handle_nsfw(event, args, state):
            yield r

    @filter.command("nai_pt")
    async def cmd_nai_pt(self, event: AstrMessageEvent) -> AsyncGenerator:
        """切换提示词显示开关。

        Args:
            event: AstrBot 消息事件。

        Yields:
            提示消息。

        Example:
            /nai_pt on
            /nai_pt off
        """
        session_key = get_session_key(event)
        state = get_session_state(session_key)
        args = event.message_str.strip().split()[1:]
        async for r in self._handle_pt(event, args, state):
            yield r

    @filter.command("nai_on")
    async def cmd_nai_on(self, event: AstrMessageEvent) -> AsyncGenerator:
        """开启本会话插件。

        Args:
            event: AstrBot 消息事件。

        Yields:
            提示消息。
        """
        session_key = get_session_key(event)
        state = get_session_state(session_key)
        async for r in self._handle_plugin_toggle(event, True, state):
            yield r

    @filter.command("nai_off")
    async def cmd_nai_off(self, event: AstrMessageEvent) -> AsyncGenerator:
        """关闭本会话插件。

        Args:
            event: AstrBot 消息事件。

        Yields:
            提示消息。
        """
        session_key = get_session_key(event)
        state = get_session_state(session_key)
        async for r in self._handle_plugin_toggle(event, False, state):
            yield r

    @filter.command("nai_recall")
    async def cmd_nai_recall(self, event: AstrMessageEvent) -> AsyncGenerator:
        """撤回最后一张图（仅 QQ/aiocqhttp 平台支持）。

        Args:
            event: AstrBot 消息事件。

        Yields:
            提示消息。
        """
        session_key = get_session_key(event)
        state = get_session_state(session_key)
        async for r in self._handle_recall(event, state):
            yield r

    @filter.command("nai_status")
    async def cmd_nai_status(self, event: AstrMessageEvent) -> AsyncGenerator:
        """查看当前会话配置状态。

        Args:
            event: AstrBot 消息事件。

        Yields:
            配置信息文本。
        """
        session_key = get_session_key(event)
        state = get_session_state(session_key)
        async for r in self._handle_status(event, state):
            yield r

    async def _show_nai_help(
        self, event: AstrMessageEvent, state: SessionState
    ) -> AsyncGenerator:
        """显示 /nai 子命令帮助。

        Args:
            event: AstrBot 消息事件。
            state: 当前会话状态。

        Yields:
            帮助文本消息。
        """
        presets_str = "、".join(SIZE_PRESETS.keys())
        versions_str = " / ".join(VERSION_MODELS.keys())
        help_text = (
            "🎨 BestNAI v2.0.0 指令帮助\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📌 生图指令\n"
            "  /nai <描述>          生成图片（支持中文/英文）\n"
            "  /nai0 <英文tag>      直接用英文 tag 生图\n"
            "  /nai_adv <提示词> [参数]  高级生图\n\n"
            "📌 会话设置\n"
            f"  /nai set <版本>     切换模型（{versions_str}）\n"
            f"  /nai size <预设|尺寸>  切换尺寸\n"
            f"    预设：{presets_str}\n"
            "    自定义：1024x1024\n"
            "  /nai nsfw on/off    NSFW 开关\n"
            "  /nai pt on/off      提示词显示开关\n"
            "  /nai on/off         插件会话开关\n\n"
            "📌 其他\n"
            "  /nai 撤回           撤回最后一张图（仅 QQ）\n"
            "  /nai status         查看当前会话配置\n"
            "  /nai help           查看此帮助\n"
            "  /nai_help           查看详细帮助\n"
            "  /nai_cfg            查看全局配置（管理员）\n"
        )
        yield event.plain_result(help_text)

    @filter.command("nai0")
    async def cmd_nai0(self, event: AstrMessageEvent) -> AsyncGenerator:
        """直接用英文 tag 生图（跳过翻译）。

        Args:
            event: AstrBot 消息事件，消息内容为英文提示词。

        Yields:
            生成状态消息和图片结果。

        Example:
            /nai0 1girl, hatsune miku, masterpiece
        """
        prompt = event.message_str.strip()
        if not prompt:
            yield event.plain_result(
                "❌ 请提供英文提示词\n用法：/nai0 <英文tag>\n示例：/nai0 1girl, masterpiece"
            )
            return

        session_key = get_session_key(event)
        state = get_session_state(session_key)

        if not state.plugin_enabled:
            yield event.plain_result("🔌 BestNAI 在本会话已关闭，使用 /nai on 开启")
            return

        cfg = self.plugin_config
        version = state.model_version
        gen_config = cfg.get_generation_config_for_version(version)

        try:
            w, h = resolve_size_preset(state.size_preset, SIZE_PRESETS)
            from dataclasses import replace as dc_replace
            gen_config = dc_replace(gen_config, width=w, height=h)
        except ValueError:
            pass

        async for result in self._do_generate(
            event, prompt, gen_config, session_state=state, skip_translation=True
        ):
            yield result

    @filter.command("nai_adv")
    async def cmd_nai_adv(self, event: AstrMessageEvent) -> AsyncGenerator:
        """高级图片生成指令。

        支持自定义生成参数，完全覆盖会话级设置。

        Args:
            event: AstrBot 消息事件。

        Yields:
            生成状态消息和图片结果。

        Example:
            /nai_adv 1girl, masterpiece --size 1024x1024 --steps 28 --scale 7 --neg "bad quality"
        """
        raw_args = event.message_str.strip()
        if not raw_args:
            yield event.plain_result(
                "❌ 请提供提示词\n"
                "用法：/nai_adv <提示词> [参数]\n"
                "参数：--size 宽x高  --steps 步数  --scale CFG值  --neg \"负面提示词\"\n"
                "示例：/nai_adv 1girl, masterpiece --size 1024x1024 --steps 28"
            )
            return

        # 检查配置
        if not self.plugin_config.is_configured():
            yield event.plain_result(
                "❌ 插件未配置，请在 AstrBot 管理面板中配置 API URL 和 API Key"
            )
            return

        # 检查冷却
        user_id = event.get_sender_id()
        remaining = self._check_cooldown(user_id)
        if remaining is not None:
            yield event.plain_result(format_cooldown_message(remaining))
            return

        # 解析参数
        try:
            prompt, overrides = parse_advanced_args(raw_args)
        except ValueError as e:
            yield event.plain_result(f"❌ 参数解析失败：{e}")
            return

        # 应用覆盖参数（以全局默认 generation 为基础）
        gen_config = apply_overrides(self.plugin_config.generation, overrides)

        # 发送生成中提示
        size_info = format_size_display(gen_config.width, gen_config.height)
        yield event.plain_result(
            f"🎨 正在生成图片（{size_info}，{gen_config.steps} 步），请稍候..."
        )

        # 更新冷却
        self._update_cooldown(user_id)

        try:
            images = await self.generator.generate(prompt, gen_config)
            async for result in self._send_images(event, images, gen_config):
                yield result

        except APIKeyError as e:
            yield event.plain_result(f"❌ API Key 错误：{e.message}")
        except QuotaExceededError as e:
            yield event.plain_result(f"❌ {e.message}")
        except RateLimitError as e:
            yield event.plain_result(f"⏳ {e.message}")
        except ServerBusyError as e:
            yield event.plain_result(f"🔄 {e.message}")
        except GenerationError as e:
            logger.error(f"[BestNAI] 高级生成失败: {e}")
            yield event.plain_result(f"❌ 生成失败：{e.message}")
        except Exception as e:
            logger.exception(f"[BestNAI] 未知错误: {e}")
            yield event.plain_result("❌ 发生未知错误，请稍后重试")

    @filter.command("nai_help")
    async def cmd_nai_help(self, event: AstrMessageEvent) -> AsyncGenerator:
        """查看详细帮助信息。

        Args:
            event: AstrBot 消息事件。

        Yields:
            帮助信息文本。
        """
        cfg = self.plugin_config
        gen = cfg.generation
        size_info = format_size_display(gen.width, gen.height)
        translator_status = "开启" if cfg.translator.enabled else "关闭"
        presets_str = "、".join(SIZE_PRESETS.keys())
        versions_str = " / ".join(VERSION_MODELS.keys())

        help_text = (
            "🎨 BestNAI 图片生成插件帮助\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📌 生图指令\n"
            "  /nai <描述>          支持中文（自动翻译）或英文 tag\n"
            "  /nai0 <英文tag>      直接英文生图（跳过翻译）\n"
            "  /nai_adv <提示词> [参数]  高级参数生图\n\n"
            "📌 会话设置\n"
            f"  /nai_set 3|4|4.5    切换模型版本\n"
            f"  /nai_size <尺寸>     切换尺寸（{presets_str} 或 宽x高）\n"
            "  /nai_nsfw on|off    NSFW 开关\n"
            "  /nai_pt on|off      提示词显示开关\n"
            "  /nai_on / /nai_off  开启/关闭本会话插件\n"
            "  /nai_recall         撤回最后一张图\n\n"
            "📌 其他\n"
            "  /nai_status         查看当前会话状态\n"
            "  /nai_help           查看帮助\n"
            "  /nai_cfg            查看插件配置（管理员）\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ 全局默认参数\n"
            f"  默认分辨率：{size_info}\n"
            f"  默认步数：{gen.steps}\n"
            f"  CFG Scale：{gen.scale}\n"
            f"  冷却时间：{cfg.user_cooldown} 秒\n"
            f"  中文翻译：{translator_status}\n"
        )
        yield event.plain_result(help_text)

    @filter.command("nai_artist")
    async def cmd_nai_artist(self, event: AstrMessageEvent) -> AsyncGenerator:
        """切换会话画师串预设。

        用法：
            /nai_artist        查看预设列表
            /nai_artist 1      切换到第 1 个预设
            /nai_artist 名称    按名称切换
            /nai_artist none   关闭画师串
            /nai_artist reset  恢复全局默认
        """
        session_key = get_session_key(event)
        state = get_session_state(session_key)
        args = event.message_str.strip().split()[1:]
        presets = self.plugin_config.artist_presets

        # 无参数 -> 显示列表
        if not args:
            if not presets:
                yield event.plain_result("当前未配置任何画师预设。\n请在 WebUI 配置 artist_presets 字段")
                return
            lines = []
            current = state.artist_preset
            for i, p in enumerate(presets, 1):
                name = p.get("name", f"预设{i}") if isinstance(p, dict) else str(p)
                active = " ✅" if (current == name or (not current and name == self.plugin_config.default_artist_preset)) else ""
                lines.append(f"  {i}. {name}{active}")
            status = f"当前：{state.artist_preset or ('全局默认 ' + (self.plugin_config.default_artist_preset or '未设置'))}"
            yield event.plain_result(f"🎨 画师预设列表：\n" + "\n".join(lines) + f"\n\n{status}")
            return

        arg = args[0]

        # none -> 关闭画师串
        if arg.lower() == "none":
            state.artist_preset = "none"
            yield event.plain_result("✅ 已关闭画师串，本次生图不使用任何预设")
            return

        # reset -> 恢复全局默认
        if arg.lower() == "reset":
            state.artist_preset = ""
            default = self.plugin_config.default_artist_preset or "未设置"
            yield event.plain_result(f"✅ 已恢复使用全局默认画师串：{default}")
            return

        # 按序号
        if arg.isdigit():
            idx = int(arg) - 1
            if idx < 0 or idx >= len(presets):
                yield event.plain_result(f"❌ 序号超出范围，当前共 {len(presets)} 个预设")
                return
            p = presets[idx]
            name = p.get("name", f"预设{idx+1}") if isinstance(p, dict) else str(p)
            state.artist_preset = name
            yield event.plain_result(f"✅ 已切换画师串：{name}")
            return

        # 按名称匹配
        matched = None
        for p in presets:
            name = p.get("name", "") if isinstance(p, dict) else str(p)
            if name.lower() == arg.lower():
                matched = name
                break
        if matched:
            state.artist_preset = matched
            yield event.plain_result(f"✅ 已切换画师串：{matched}")
        else:
            yield event.plain_result(f"❌ 未找到预设「{arg}」，发送 /nai_artist 查看列表")

    @filter.command("nai_cfg")
    async def cmd_nai_cfg(self, event: AstrMessageEvent) -> AsyncGenerator:
        """查看当前全局插件配置（管理员指令）。

        显示当前插件配置信息，API Key 脱敏显示。

        Args:
            event: AstrBot 消息事件。

        Yields:
            配置信息文本。
        """
        cfg = self.plugin_config
        gen = cfg.generation
        size_info = format_size_display(gen.width, gen.height)
        tr = cfg.translator

        # 版本独立配置
        ver_cfgs = []
        for ver in ("3", "4", "4.5"):
            vc = cfg.get_generation_config_for_version(ver)
            ver_cfgs.append(
                f"  NAI V{ver}: {vc.model} | {vc.width}x{vc.height} | {vc.steps}步 | {vc.sampler}"
            )
        ver_str = "\n".join(ver_cfgs)

        # 画师预设
        if cfg.artist_presets:
            presets_info = "\n".join(
                f"  - {p.get('name', '?')}: {p.get('prompt', '')[:40]}..."
                if isinstance(p, dict)
                else f"  - {p}"
                for p in cfg.artist_presets
            )
            default_preset = cfg.default_artist_preset or "（无）"
        else:
            presets_info = "  （未配置）"
            default_preset = "（无）"

        config_text = (
            "⚙️ BestNAI 插件全局配置 v2.0.0\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 API URL：{cfg.api_url or '(未配置)'}\n"
            f"🔑 API Key：{cfg.masked_api_key()}\n"
            f"✅ 配置状态：{'已配置' if cfg.is_configured() else '❌ 未完成配置'}\n\n"
            "📐 版本独立配置\n"
            f"{ver_str}\n\n"
            "📐 全局默认生成参数\n"
            f"  分辨率：{size_info}\n"
            f"  步数：{gen.steps}\n"
            f"  CFG Scale：{gen.scale}\n"
            f"  采样器：{gen.sampler}\n"
            f"  噪声调度：{gen.noise_schedule}\n"
            f"  质量增强：{'开启' if gen.quality else '关闭'}\n\n"
            "⏱️ 冷却设置\n"
            f"  用户冷却时间：{cfg.user_cooldown} 秒\n\n"
            "💾 图片保存\n"
            f"  保存图片：{'开启' if cfg.save_images else '关闭'}\n"
            f"  保存目录：{cfg.save_dir or '(未设置)'}\n\n"
            "🔁 自动撤回\n"
            f"  状态：{'开启' if cfg.auto_recall else '关闭'}\n"
            f"  延迟：{cfg.auto_recall_delay} 秒（仅 QQ 平台有效）\n\n"
            "🔤 中文翻译器\n"
            f"  状态：{'开启' if tr.enabled else '关闭'}\n"
            f"  API 地址：{tr.base_url or '(未配置)'}\n"
            f"  API Key：{tr.masked_api_key()}\n"
            f"  模型：{tr.model}\n\n"
            "🖌️ 画师预设\n"
            f"  默认预设：{default_preset}\n"
            f"{presets_info}\n\n"
            "📎 提示词后缀\n"
            f"  {cfg.prompt_suffix or '（未设置）'}\n"
        )
        yield event.plain_result(config_text)
