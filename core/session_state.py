"""会话级状态管理模块。

每个群/用户维护独立的会话状态，支持动态切换模型、尺寸、NSFW 等设置。
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class SessionState:
    """单个会话的运行时状态。

    Attributes:
        model_version: 使用的模型版本，可选 "3" / "4" / "4.5"。
        size_preset: 尺寸预设名称或 "宽x高" 形式的自定义尺寸。
        nsfw_enabled: 是否开启 NSFW 内容（关闭时自动追加 NSFW 负面词）。
        pt_show: 生图成功后是否额外发送提示词。
        plugin_enabled: 该会话是否允许使用本插件。
        last_image_message_id: 最近一次发送图片的消息 ID（用于手动撤回）。
    """

    model_version: str = "4.5"
    size_preset: str = "竖图"
    nsfw_enabled: bool = False
    pt_show: bool = False
    plugin_enabled: bool = True
    last_image_message_id: Optional[int] = None
    artist_preset: str = ""  # 空字符串表示使用全局默认，"none" 表示不使用任何预设


# 全局会话状态字典，key = session_key（group_id 或 user_id）
_session_states: Dict[str, SessionState] = {}
_default_version: str = "4.5"


def set_default_version(version: str) -> None:
    """设置新会话的默认模型版本。

    Args:
        version: 模型版本，可选 "3" / "4" / "4.5"。
    """
    global _default_version
    _default_version = version


def get_session_state(session_key: str) -> SessionState:
    """获取指定会话的状态，不存在则创建默认状态。

    Args:
        session_key: 会话唯一标识（群 ID 或用户 ID）。

    Returns:
        对应的 SessionState 实例。
    """
    if session_key not in _session_states:
        _session_states[session_key] = SessionState(model_version=_default_version)
    return _session_states[session_key]


def reset_session_state(session_key: str) -> SessionState:
    """重置指定会话的状态为默认值。

    Args:
        session_key: 会话唯一标识。

    Returns:
        新建的默认 SessionState 实例。
    """
    _session_states[session_key] = SessionState(model_version=_default_version)
    return _session_states[session_key]
