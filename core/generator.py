"""图片生成核心模块。

封装 BestNAI API 调用逻辑，处理请求、响应解析和错误处理。
"""

import base64
import json
from typing import List, Optional, Tuple

import aiohttp

from ..models.config import GenerationConfig, PluginConfig


class GenerationError(Exception):
    """图片生成错误基类。

    Attributes:
        message: 错误消息。
        status_code: HTTP 状态码（如果适用）。
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        """初始化生成错误。

        Args:
            message: 错误消息。
            status_code: HTTP 状态码。
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class APIKeyError(GenerationError):
    """API Key 未配置或无效错误。"""
    pass


class QuotaExceededError(GenerationError):
    """点数不足错误（HTTP 402）。"""
    pass


class RateLimitError(GenerationError):
    """频率限制错误（HTTP 429）。"""
    pass


class ServerBusyError(GenerationError):
    """服务器繁忙错误（HTTP 503）。"""
    pass


class ImageGenerator:
    """BestNAI 图片生成器。

    封装与 BestNAI API 的交互，提供图片生成功能。

    Attributes:
        config: 插件配置。
        timeout: 请求超时时间（秒）。
    """

    DEFAULT_TIMEOUT = 120

    def __init__(self, config: PluginConfig, timeout: int = DEFAULT_TIMEOUT) -> None:
        """初始化图片生成器。

        Args:
            config: 插件配置实例。
            timeout: HTTP 请求超时时间（秒），默认 120 秒。
        """
        self.config = config
        self.timeout = timeout

    async def generate(
        self,
        prompt: str,
        gen_config: Optional[GenerationConfig] = None,
    ) -> List[Tuple[str, bytes]]:
        """生成图片。

        Args:
            prompt: 正面提示词。
            gen_config: 生成配置，为 None 时使用插件默认配置。

        Returns:
            [(image_format, image_bytes), ...] 列表。

        Raises:
            APIKeyError: API Key 未配置或无效。
            QuotaExceededError: 点数不足。
            RateLimitError: 请求频率超限。
            ServerBusyError: 服务器繁忙。
            GenerationError: 其他生成错误。
        """
        if not self.config.is_configured():
            raise APIKeyError("API Key 或 API URL 未配置，请在插件配置中填写")

        if gen_config is None:
            gen_config = self.config.generation

        params = gen_config.to_api_params(prompt)
        payload = self._build_payload(params, gen_config.model)

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        ) as session:
            return await self._do_request(session, payload)

    def _build_payload(self, params: dict, model: str) -> dict:
        """构建 API 请求体。

        Args:
            params: 生成参数字典。
            model: 模型名称。

        Returns:
            完整的 API 请求体字典。
        """
        return {
            "model": model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(params, ensure_ascii=False),
                }
            ],
        }

    async def _do_request(
        self,
        session: aiohttp.ClientSession,
        payload: dict,
    ) -> List[Tuple[str, bytes]]:
        """执行 API 请求并解析响应。

        Args:
            session: aiohttp 客户端会话。
            payload: 请求体字典。

        Returns:
            [(image_format, image_bytes), ...] 列表。

        Raises:
            APIKeyError: 认证失败（401）。
            QuotaExceededError: 点数不足（402）。
            RateLimitError: 频率限制（429）。
            ServerBusyError: 服务器繁忙（503）。
            GenerationError: 其他 HTTP 错误或解析错误。
        """
        url = f"{self.config.api_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return self._parse_response(data)
                elif resp.status == 400:
                    text = await resp.text()
                    raise GenerationError(f"请求参数错误: {text}", status_code=400)
                elif resp.status == 401:
                    raise APIKeyError("API Key 无效，请检查配置", status_code=401)
                elif resp.status == 402:
                    raise QuotaExceededError(
                        "点数不足！请尝试使用更小的分辨率（如 512x768）",
                        status_code=402,
                    )
                elif resp.status == 429:
                    raise RateLimitError(
                        "请求过于频繁，请稍后再试", status_code=429
                    )
                elif resp.status == 503:
                    raise ServerBusyError(
                        "服务器繁忙，请稍后重试", status_code=503
                    )
                else:
                    text = await resp.text()
                    raise GenerationError(
                        f"API 请求失败 (HTTP {resp.status}): {text}",
                        status_code=resp.status,
                    )
        except aiohttp.ClientConnectorError as e:
            raise GenerationError(f"无法连接到 API 服务器: {e}") from e
        except aiohttp.ServerTimeoutError as e:
            raise GenerationError(f"请求超时（{self.timeout}秒），请稍后重试") from e
        except aiohttp.ClientError as e:
            raise GenerationError(f"网络请求错误: {e}") from e

    def _parse_response(self, data: dict) -> List[Tuple[str, bytes]]:
        """解析 API 响应，提取图片数据。

        Args:
            data: API 响应 JSON 数据。

        Returns:
            [(image_format, image_bytes), ...] 列表。

        Raises:
            GenerationError: 响应格式不正确或无图片数据。
        """
        import re

        try:
            choices = data.get("choices", [])
            if not choices:
                raise GenerationError("API 返回数据中没有 choices 字段")

            content = choices[0].get("message", {}).get("content", "")
            if not content:
                raise GenerationError("API 返回内容为空")

            pattern = r'data:image/(\w+);base64,([A-Za-z0-9+/=]+)'
            matches = re.findall(pattern, content)

            if not matches:
                raise GenerationError("API 返回内容中未找到图片数据")

            results = []
            for img_format, b64_data in matches:
                try:
                    image_bytes = base64.b64decode(b64_data)
                    results.append((img_format, image_bytes))
                except Exception as e:
                    raise GenerationError(f"图片 base64 解码失败: {e}") from e

            return results

        except (KeyError, IndexError, TypeError) as e:
            raise GenerationError(f"解析 API 响应失败: {e}") from e
