"""配置模型模块。

定义插件配置的数据类和验证逻辑。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class GenerationConfig:
    """图片生成配置。

    Attributes:
        model: 使用的模型名称。
        width: 图片宽度（像素）。
        height: 图片高度（像素）。
        steps: 生成步数。
        scale: CFG Scale 值。
        sampler: 采样器名称。
        negative_prompt: 负面提示词。
        quality: 是否启用质量增强。
        uc_preset: UC 预设级别。
        noise_schedule: 噪声调度方式。
        image_format: 输出图片格式。
    """

    model: str = "nai-diffusion-4-5-full-anlas-0"
    width: int = 832
    height: int = 1216
    steps: int = 23
    scale: float = 5.0
    sampler: str = "k_euler_ancestral"
    negative_prompt: str = "lowres, bad anatomy, bad hands, text, error, missing fingers"
    quality: bool = True
    uc_preset: str = "light"
    noise_schedule: str = "karras"
    image_format: str = "png"

    @classmethod
    def from_plugin_config(cls, config: dict) -> "GenerationConfig":
        """从插件配置字典创建生成配置。

        Args:
            config: 插件配置字典。

        Returns:
            GenerationConfig 实例。
        """
        from ..constants import SIZE_PRESETS
        size_str = config.get("default_size", "竖图")
        width, height = resolve_size_preset(size_str, SIZE_PRESETS)

        return cls(
            model=config.get("default_model", "nai-diffusion-4-5-full-anlas-0"),
            width=width,
            height=height,
            steps=int(config.get("default_steps", 23)),
            scale=float(config.get("default_scale", 5.0)),
            negative_prompt=config.get(
                "negative_prompt",
                "lowres, bad anatomy, bad hands, text, error, missing fingers",
            ),
        )

    @classmethod
    def for_version(cls, version: str, config: dict, base: "GenerationConfig") -> "GenerationConfig":
        """根据模型版本返回对应配置的 GenerationConfig。

        会读取 nai3_* / nai4_* / nai45_* 等版本独立配置项覆盖 base。

        Args:
            version: 模型版本，如 "3" / "4" / "4.5"。
            config: 插件原始配置字典。
            base: 基础 GenerationConfig（用于其他参数的默认值）。

        Returns:
            对应版本的 GenerationConfig 实例。
        """
        from dataclasses import replace
        from ..constants import SIZE_PRESETS, VERSION_MODELS

        prefix_map = {"3": "nai3", "4": "nai4", "4.5": "nai45"}
        prefix = prefix_map.get(version, "nai45")
        model = VERSION_MODELS.get(version, base.model)

        steps_key = f"{prefix}_steps"
        size_key = f"{prefix}_size"
        sampler_key = f"{prefix}_sampler"

        steps = int(config.get(steps_key, base.steps))
        size_str = config.get(size_key, "竖图")
        width, height = resolve_size_preset(size_str, SIZE_PRESETS)
        sampler = config.get(sampler_key, base.sampler)

        return replace(
            base,
            model=model,
            steps=steps,
            width=width,
            height=height,
            sampler=sampler,
        )

    def to_api_params(self, prompt: str) -> dict:
        """转换为 API 请求参数。

        Args:
            prompt: 正面提示词。

        Returns:
            API 请求参数字典。
        """
        return {
            "prompt": prompt,
            "size": [self.width, self.height],
            "steps": self.steps,
            "scale": self.scale,
            "sampler": self.sampler,
            "negative_prompt": self.negative_prompt,
            "quality_toggle": self.quality,
            "ucPreset": self.uc_preset,
            "noise_schedule": self.noise_schedule,
            "image_format": self.image_format,
        }


@dataclass
class TranslatorConfig:
    """提示词翻译器配置。"""

    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-4o-mini"
    show_progress: bool = True
    show_result: bool = True
    system_prompt: str = ""

    def is_configured(self) -> bool:
        """检查翻译器是否已配置。

        Returns:
            True 如果 base_url 和 api_key 均不为空。
        """
        return bool(self.base_url and self.api_key)

    def masked_api_key(self) -> str:
        """返回脱敏后的 API Key。

        Returns:
            脱敏字符串。
        """
        if not self.api_key:
            return "(未配置)"
        if len(self.api_key) <= 8:
            return "****"
        return f"{self.api_key[:4]}****{self.api_key[-4:]}"


@dataclass
class PluginConfig:
    """插件全局配置。

    Attributes:
        api_url: BestNAI API 地址。
        api_key: API 密钥（Bearer Token）。
        user_cooldown: 用户冷却时间（秒）。
        save_images: 是否保存生成的图片。
        save_dir: 图片保存目录。
        auto_recall: 是否自动撤回。
        auto_recall_delay: 自动撤回延迟（秒）。
        generation: 默认生成配置。
        translator: 翻译器配置。
        artist_presets: 画师风格预设列表。
        default_artist_preset: 默认画师预设名。
        prompt_suffix: 提示词后缀。
        raw_config: 原始配置字典（用于版本独立配置读取）。
    """

    api_url: str = ""
    api_key: str = ""
    user_cooldown: int = 30
    save_images: bool = False
    save_dir: str = ""
    auto_recall: bool = False
    auto_recall_delay: int = 30
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    translator: TranslatorConfig = field(default_factory=TranslatorConfig)
    artist_presets: List[Dict] = field(default_factory=list)
    default_artist_preset: str = ""
    prompt_suffix: str = ""
    raw_config: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, config: dict) -> "PluginConfig":
        """从字典创建插件配置。

        Args:
            config: 配置字典。

        Returns:
            PluginConfig 实例。
        """
        return cls(
            api_url=config.get("api_url", "").rstrip("/"),
            api_key=config.get("api_key", ""),
            user_cooldown=int(config.get("user_cooldown", 30)),
            save_images=bool(config.get("save_images", False)),
            save_dir=config.get("save_dir", ""),
            auto_recall=bool(config.get("auto_recall", False)),
            auto_recall_delay=int(config.get("auto_recall_delay", 30)),
            generation=GenerationConfig.from_plugin_config(config),
            translator=TranslatorConfig(
                enabled=bool(config.get("translator_enabled", False)),
                base_url=config.get("translator_base_url", "").rstrip("/"),
                api_key=config.get("translator_api_key", ""),
                model=config.get("translator_model", "gpt-4o-mini"),
                show_progress=bool(config.get("translator_show_progress", True)),
                show_result=bool(config.get("translator_show_result", True)),
                system_prompt=config.get("translator_system_prompt", ""),
            ),
            artist_presets=list(config.get("artist_presets", [])),
            default_artist_preset=config.get("default_artist_preset", ""),
            prompt_suffix=config.get("prompt_suffix", ""),
            raw_config=config,
        )

    def get_generation_config_for_version(self, version: str) -> GenerationConfig:
        """获取指定模型版本的生成配置。

        Args:
            version: 模型版本，如 "3" / "4" / "4.5"。

        Returns:
            对应版本的 GenerationConfig。
        """
        return GenerationConfig.for_version(version, self.raw_config, self.generation)

    def get_artist_prompt(self, preset_name: str) -> str:
        """根据预设名称查找画师提示词。

        Args:
            preset_name: 画师预设名称。

        Returns:
            对应的 prompt 字符串，未找到则返回空字符串。
        """
        for preset in self.artist_presets:
            if isinstance(preset, dict) and preset.get("name") == preset_name:
                return preset.get("prompt", "")
        return ""

    def is_configured(self) -> bool:
        """检查是否已完成基本配置。

        Returns:
            True 如果 API URL 和 API Key 均已配置。
        """
        return bool(self.api_url and self.api_key)

    def masked_api_key(self) -> str:
        """返回脱敏后的 API Key。

        Returns:
            脱敏后的 API Key 字符串，仅显示前4位和后4位。
        """
        if not self.api_key:
            return "(未配置)"
        if len(self.api_key) <= 8:
            return "****"
        return f"{self.api_key[:4]}****{self.api_key[-4:]}"


def _parse_size(size_str: str) -> Tuple[int, int]:
    """解析分辨率字符串。

    Args:
        size_str: 格式为 "宽x高" 的字符串，例如 "832x1216"。

    Returns:
        (宽, 高) 元组。

    Raises:
        ValueError: 如果格式不正确。
    """
    try:
        parts = size_str.lower().replace("×", "x").split("x")
        if len(parts) != 2:
            raise ValueError(f"无效的分辨率格式: {size_str}")
        width = int(parts[0].strip())
        height = int(parts[1].strip())
        if width <= 0 or height <= 0:
            raise ValueError(f"分辨率必须为正整数: {size_str}")
        return width, height
    except (ValueError, AttributeError) as e:
        raise ValueError(f"解析分辨率失败: {size_str}") from e


def resolve_size_preset(size_input: str, presets: dict) -> Tuple[int, int]:
    """将预设名称或 "宽x高" 字符串解析为 (宽, 高)。

    优先查找预设表，找不到则尝试直接解析为数字。

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
