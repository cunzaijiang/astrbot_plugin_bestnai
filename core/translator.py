"""提示词翻译模块。

使用 LLM 将中文描述转换为 NAI 风格的英文提示词。
"""

import re
import aiohttp

SYSTEM_PROMPT = """你是一位专业的 NovelAI 绘画提示词专家，精通 Danbooru 标签体系。
你的任务是将用户的中文描述转换为高质量的英文提示词（Danbooru tag 格式）。

规则：
1. 直接输出英文提示词，不要解释，不要加任何前缀
2. 使用逗号分隔各个 tag
3. 标签顺序：人物数量 > 角色名 > 外观 > 服装 > 动作 > 场景 > 光影
4. 已知角色（如初音未来、雷姆等）直接写角色名和出处，如 hatsune miku (vocaloid)
5. 原创人物需描述外貌特征（发色、发型、瞳色等）
6. 单人场景在最前加 solo, 1girl 或 solo, 1boy
7. 自动补充 masterpiece, best quality 等质量标签
8. 保留用户提供的英文 tag 不变

示例：
输入：画一张初音未来站在樱花树下
输出：solo, 1girl, hatsune miku (vocaloid), long hair, twintails, teal hair, teal eyes, school uniform, standing, cherry blossoms, outdoors, spring, petals, masterpiece, best quality"""


def has_chinese(text: str) -> bool:
    """检测文本是否包含中文字符。"""
    return bool(re.search(r'[\u4e00-\u9fff]', text))


class TranslatorError(Exception):
    """翻译错误。"""
    pass


class PromptTranslator:
    """提示词翻译器。"""

    def __init__(self, config):  # config: TranslatorConfig
        self.config = config
        self.timeout = 30

    async def translate(self, text: str) -> str:
        """将中文描述翻译为英文提示词。

        失败时返回原文（降级处理）。
        """
        if not self.config.enabled:
            return text
        if not has_chinese(text):
            return text
        if not self.config.is_configured():
            return text

        try:
            return await self._call_llm(text)
        except Exception as e:
            from astrbot.api import logger
            logger.warning(f"[BestNAI] 翻译失败，使用原文: {e}")
            return text

    async def _call_llm(self, text: str) -> str:
        base = self.config.base_url.rstrip('/')
        # 避免重复拼接 /v1
        if base.endswith('/v1'):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"
        system_prompt = self.config.system_prompt.strip() if self.config.system_prompt else SYSTEM_PROMPT
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            "temperature": 0.2,
            "max_tokens": 300,
            "stream": False
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json"
        }

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        ) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    text_body = await resp.text()
                    raise TranslatorError(f"LLM API 返回 {resp.status}: {text_body[:200]}")
                data = await resp.json()
                result = data["choices"][0]["message"]["content"].strip()
                return result
