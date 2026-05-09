"""工具函数模块。

提供参数解析、文件处理等通用工具函数。
"""

import argparse
import os
import re
import shlex
import tempfile
import time
from typing import Dict, Optional, Tuple

from ..models.config import GenerationConfig, _parse_size


def parse_advanced_args(raw_args: str) -> Tuple[str, Dict]:
    """解析高级生成指令的参数。

    支持格式：<提示词> [--size WxH] [--steps N] [--scale F] [--neg "负面提示词"]

    Args:
        raw_args: 原始参数字符串。

    Returns:
        (prompt, overrides) 元组，overrides 包含覆盖的参数。

    Raises:
        ValueError: 如果参数格式不正确。
    """
    # 使用 shlex 分割，支持引号包裹的字符串
    try:
        tokens = shlex.split(raw_args)
    except ValueError as e:
        raise ValueError(f"参数解析失败，请检查引号是否匹配: {e}") from e

    # 提取 prompt（非 -- 开头的前缀部分）
    prompt_parts = []
    i = 0
    while i < len(tokens) and not tokens[i].startswith("--"):
        prompt_parts.append(tokens[i])
        i += 1

    prompt = " ".join(prompt_parts).strip()
    if not prompt:
        raise ValueError("提示词不能为空")

    # 解析剩余参数
    overrides: Dict = {}
    while i < len(tokens):
        token = tokens[i]
        if token == "--size":
            i += 1
            if i >= len(tokens):
                raise ValueError("--size 参数缺少值")
            try:
                width, height = _parse_size(tokens[i])
                overrides["width"] = width
                overrides["height"] = height
            except ValueError as e:
                raise ValueError(f"--size 参数无效: {e}") from e
        elif token == "--steps":
            i += 1
            if i >= len(tokens):
                raise ValueError("--steps 参数缺少值")
            try:
                steps = int(tokens[i])
                if steps <= 0 or steps > 150:
                    raise ValueError("步数必须在 1-150 之间")
                overrides["steps"] = steps
            except ValueError as e:
                raise ValueError(f"--steps 参数无效: {e}") from e
        elif token == "--scale":
            i += 1
            if i >= len(tokens):
                raise ValueError("--scale 参数缺少值")
            try:
                scale = float(tokens[i])
                if scale <= 0 or scale > 20:
                    raise ValueError("CFG Scale 必须在 0-20 之间")
                overrides["scale"] = scale
            except ValueError as e:
                raise ValueError(f"--scale 参数无效: {e}") from e
        elif token == "--neg":
            i += 1
            if i >= len(tokens):
                raise ValueError("--neg 参数缺少值")
            overrides["negative_prompt"] = tokens[i]
        else:
            raise ValueError(f"未知参数: {token}")
        i += 1

    return prompt, overrides


def apply_overrides(base_config: GenerationConfig, overrides: Dict) -> GenerationConfig:
    """将覆盖参数应用到生成配置。

    Args:
        base_config: 基础生成配置。
        overrides: 覆盖参数字典。

    Returns:
        新的 GenerationConfig 实例（不修改原始配置）。
    """
    from dataclasses import replace
    return replace(base_config, **overrides)


def save_image_to_temp(image_data: bytes, image_format: str = "png") -> str:
    """将图片数据保存到临时文件。

    Args:
        image_data: 图片二进制数据。
        image_format: 图片格式（png/jpg 等）。

    Returns:
        临时文件路径。
    """
    suffix = f".{image_format.lower()}"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="bestnai_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(image_data)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def save_image_to_dir(image_data: bytes, save_dir: str, image_format: str = "png") -> str:
    """将图片数据保存到指定目录。

    Args:
        image_data: 图片二进制数据。
        save_dir: 保存目录路径。
        image_format: 图片格式（png/jpg 等）。

    Returns:
        保存的文件路径。
    """
    os.makedirs(save_dir, exist_ok=True)
    timestamp = int(time.time() * 1000)
    filename = f"bestnai_{timestamp}.{image_format.lower()}"
    path = os.path.join(save_dir, filename)
    with open(path, "wb") as f:
        f.write(image_data)
    return path


def cleanup_file(path: str) -> None:
    """安全删除文件。

    Args:
        path: 要删除的文件路径。
    """
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def extract_images_from_content(content: str) -> list:
    """从 API 返回内容中提取 base64 图片数据。

    Args:
        content: API 返回的内容字符串，包含 markdown data-URI 格式的图片。

    Returns:
        [(image_format, base64_data), ...] 列表。
    """
    pattern = r'data:image/(\w+);base64,([A-Za-z0-9+/=]+)'
    return re.findall(pattern, content)


def format_cooldown_message(remaining: float) -> str:
    """格式化冷却时间提示消息。

    Args:
        remaining: 剩余冷却时间（秒）。

    Returns:
        格式化的提示消息。
    """
    return f"⏳ 冷却中，请等待 {remaining:.0f} 秒后再试"


def format_size_display(width: int, height: int) -> str:
    """格式化分辨率显示。

    Args:
        width: 宽度。
        height: 高度。

    Returns:
        格式化的分辨率字符串。
    """
    return f"{width}x{height}"


def get_session_key(event) -> str:
    """从消息事件中提取会话唯一标识。

    群聊优先使用群 ID，私聊使用用户 ID。

    Args:
        event: AstrBot 消息事件对象。

    Returns:
        会话 key 字符串，格式为 "group_{group_id}" 或 "user_{user_id}"。
    """
    group_id = event.get_group_id()
    if group_id:
        return f"group_{group_id}"
    user_id = event.get_sender_id()
    return f"user_{user_id}"


def resolve_size_preset(size_input: str, presets: dict) -> Tuple[int, int]:
    """将预设名称或 "宽x高" 字符串解析为 (宽, 高)。

    优先查找预设表，找不到则尝试直接解析为 "宽x高" 格式。

    Args:
        size_input: 预设名称（如 "竖图"）或 "宽x高" 字符串（如 "1024x1024"）。
        presets: 预设名称到 (宽, 高) 的映射字典。

    Returns:
        (宽, 高) 元组。

    Raises:
        ValueError: 如果既不是已知预设也无法解析为尺寸。
    """
    size_input = size_input.strip()
    if size_input in presets:
        return presets[size_input]
    return _parse_size(size_input)


def build_final_prompt(
    prompt: str,
    plugin_config,
    artist_preset_name: str = "",
) -> str:
    """构建最终提示词，追加画师预设和后缀。

    Args:
        prompt: 用户输入的基础提示词。
        plugin_config: PluginConfig 实例。
        artist_preset_name: 要使用的画师预设名称，空字符串表示使用默认预设。

    Returns:
        最终提示词字符串。
    """
    parts = [prompt.strip()]

    # 画师预设
    preset_name = artist_preset_name or plugin_config.default_artist_preset
    if preset_name:
        artist_prompt = plugin_config.get_artist_prompt(preset_name)
        if artist_prompt:
            parts.append(artist_prompt.strip())

    # 提示词后缀
    if plugin_config.prompt_suffix:
        parts.append(plugin_config.prompt_suffix.strip())

    return ", ".join(p for p in parts if p)


# 当 NSFW 关闭时，需要从正向 prompt 中移除的词（quality_toggle 可能自动注入 nsfw 等词）
_NSFW_POSITIVE_BLOCKLIST = [
    "nsfw", "explicit", "nude", "naked", "sex", "porn", "hentai",
    "genitals", "penis", "vagina", "breast", "nipple",
]


def strip_nsfw_from_prompt(prompt: str) -> str:
    """从正向提示词中移除 NSFW 相关词汇（不区分大小写，整词匹配）。

    Args:
        prompt: 原始正向提示词。

    Returns:
        清理后的正向提示词。
    """
    import re
    parts = [p.strip() for p in prompt.split(",")]
    cleaned = []
    for part in parts:
        lower = part.lower().strip()
        blocked = any(
            re.fullmatch(r"[\s]*" + re.escape(w) + r"[\s]*", lower)
            for w in _NSFW_POSITIVE_BLOCKLIST
        )
        if not blocked:
            cleaned.append(part)
    return ", ".join(p for p in cleaned if p)


def apply_nsfw_filter(negative_prompt: str, nsfw_enabled: bool) -> str:
    """根据 NSFW 开关决定是否追加 NSFW 负面提示词。

    Args:
        negative_prompt: 当前负面提示词。
        nsfw_enabled: 是否开启 NSFW（True 时不追加，False 时追加过滤词）。

    Returns:
        处理后的负面提示词字符串。
    """
    from ..constants import NSFW_NEGATIVE_TAGS
    if nsfw_enabled:
        return negative_prompt
    tags = NSFW_NEGATIVE_TAGS
    if tags and tags not in negative_prompt:
        return f"{negative_prompt}, {tags}" if negative_prompt else tags
    return negative_prompt
