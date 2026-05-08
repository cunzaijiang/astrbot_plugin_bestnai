# astrbot_plugin_bestnai

通过 BestNAI API（NovelAI 图片生成）在 AstrBot 中生成 AI 图片的插件。

## 功能特性

- 🎨 **基础生成**：`/nai <提示词>` 一键生成图片
- ⚙️ **高级生成**：`/nai_adv` 支持自定义分辨率、步数、CFG Scale、负面提示词
- ⏱️ **冷却机制**：每个用户独立冷却时间，防止滥用
- 💾 **图片保存**：可选持久化保存生成的图片
- 🔒 **安全配置**：API Key 脱敏显示

## 安装

1. 将插件目录放置到 AstrBot 的 `addons/plugins/` 目录下
2. 在 AstrBot 管理面板中启用插件
3. 配置 API URL 和 API Key

## 配置

在 AstrBot 管理面板的插件配置中填写以下参数：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `api_url` | BestNAI API 地址 | （必填） |
| `api_key` | API 密钥（Bearer Token） | （必填） |
| `default_model` | 默认模型 | `nai-diffusion-4-5-full-anlas-0` |
| `default_size` | 默认分辨率（宽x高） | `832x1216` |
| `default_steps` | 默认生成步数 | `23` |
| `default_scale` | 默认 CFG Scale | `5.0` |
| `user_cooldown` | 用户冷却时间（秒） | `30` |
| `negative_prompt` | 默认负面提示词 | `lowres, bad anatomy...` |
| `save_images` | 是否保存生成的图片 | `false` |
| `save_dir` | 图片保存目录 | （空） |

## 使用方法

### 基础生成

```
/nai 1girl, masterpiece, best quality, anime style
```

使用默认参数生成图片，生成过程中会显示"正在生成..."提示。

### 高级生成

```
/nai_adv 1girl, masterpiece --size 1024x1024 --steps 28 --scale 7 --neg "bad quality, blurry"
```

**支持的参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| `--size 宽x高` | 自定义分辨率 | `--size 1024x1024` |
| `--steps N` | 生成步数（1-150） | `--steps 28` |
| `--scale F` | CFG Scale（0-20） | `--scale 7` |
| `--neg "提示词"` | 负面提示词 | `--neg "bad quality"` |

### 查看帮助

```
/nai_help
```

### 查看配置（管理员）

```
/nai_cfg
```

显示当前插件配置，API Key 自动脱敏。

## 错误处理

| 错误 | 说明 | 解决方案 |
|------|------|----------|
| API Key 未配置 | 未填写 API URL 或 API Key | 在管理面板中完成配置 |
| 401 认证失败 | API Key 无效 | 检查 API Key 是否正确 |
| 402 点数不足 | 账户余额不足 | 充值或使用更小的分辨率 |
| 429 频率限制 | 请求过于频繁 | 等待后重试 |
| 503 服务器繁忙 | 服务端负载过高 | 稍后重试 |
| 超时（120秒） | 网络或服务器响应慢 | 检查网络连接 |

## 默认生成参数

```
model:          nai-diffusion-4-5-full-anlas-0
size:           832 x 1216
steps:          23
scale:          5.0
sampler:        k_euler_ancestral
quality:        true
uc_preset:      light
noise_schedule: karras
image_format:   png
```

## 项目结构

```
astrbot_plugin_bestnai/
├── main.py                   # 主入口，插件类和指令处理
├── metadata.yaml             # 插件元数据
├── _conf_schema.json         # 配置模式定义
├── requirements.txt          # Python 依赖
├── README.md                 # 本文档
├── core/
│   ├── __init__.py
│   └── generator.py          # BestNAI API 调用核心逻辑
├── models/
│   ├── __init__.py
│   └── config.py             # 配置数据模型
└── utils/
    ├── __init__.py
    └── helpers.py            # 工具函数（参数解析、文件处理等）
```

## 依赖

- `aiohttp >= 3.8.0`：异步 HTTP 客户端

## 开发说明

- Python 3.9+
- 异步编程（asyncio + aiohttp）
- 完整类型注解
- Google Style 文档字符串

## License

MIT
