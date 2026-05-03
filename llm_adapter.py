"""
LLM适配器 - 多模型统一接口
"""

import json
import os
from abc import ABC, abstractmethod


class LLMAdapter(ABC):
    """LLM适配器基类"""

    @abstractmethod
    def chat(self, messages: list, **kwargs) -> str:
        """聊天接口"""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """检查是否已配置"""
        pass


class AliyunAdapter(LLMAdapter):
    """阿里云DashScope适配器"""

    def __init__(self, config: dict):
        self.api_key = config.get('api_key', '')
        self.model = config.get('name', 'qwen-plus')
        self.base_url = config.get('base_url', 'https://dashscope.aliyuncs.com/api/v1')

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list, **kwargs) -> str:
        """调用阿里云Qwen模型"""
        if not self.api_key:
            raise ValueError("API Key未配置")

        try:
            import dashscope
            from dashscope import Generation

            dashscope.api_key = self.api_key

            response = Generation.call(
                model=self.model,
                messages=messages,
                result_format='message',
                **kwargs
            )

            if response.status_code == 200:
                return response.output.choices[0].message.content
            else:
                raise Exception(f"API调用失败: {response.code} - {response.message}")

        except ImportError:
            # 如果dashscope不可用，使用HTTP请求
            return self._chat_http(messages, **kwargs)

    def _chat_http(self, messages: list, **kwargs) -> str:
        """使用HTTP请求调用API"""
        import requests

        url = f"{self.base_url}/services/aigc/text-generation/generation"

        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }

        data = {
            'model': self.model,
            'input': {
                'messages': messages
            },
            'parameters': {
                'result_format': 'message'
            }
        }

        response = requests.post(url, headers=headers, json=data)

        if response.status_code == 200:
            result = response.json()
            return result['output']['choices'][0]['message']['content']
        else:
            raise Exception(f"API调用失败: {response.status_code} - {response.text}")


class OpenAIAdapter(LLMAdapter):
    """OpenAI适配器"""

    def __init__(self, config: dict):
        self.api_key = config.get('api_key', '')
        self.model = config.get('name', 'gpt-4')
        self.base_url = config.get('base_url', 'https://api.openai.com/v1')

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list, **kwargs) -> str:
        """调用OpenAI模型"""
        import requests

        url = f"{self.base_url}/chat/completions"

        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }

        data = {
            'model': self.model,
            'messages': messages,
            **kwargs
        }

        response = requests.post(url, headers=headers, json=data)

        if response.status_code == 200:
            result = response.json()
            return result['choices'][0]['message']['content']
        else:
            raise Exception(f"API调用失败: {response.status_code} - {response.text}")


class LocalAdapter(LLMAdapter):
    """本地模型适配器（Ollama）"""

    def __init__(self, config: dict):
        self.model = config.get('name', 'llama2')
        self.base_url = config.get('base_url', 'http://localhost:11434')

    def is_configured(self) -> bool:
        # 本地模型无需API Key，检查服务是否可用
        import requests
        try:
            response = requests.get(f"{self.base_url}/api/tags")
            return response.status_code == 200
        except:
            return False

    def chat(self, messages: list, **kwargs) -> str:
        """调用本地Ollama模型"""
        import requests

        url = f"{self.base_url}/api/chat"

        data = {
            'model': self.model,
            'messages': messages,
            'stream': False
        }

        response = requests.post(url, json=data)

        if response.status_code == 200:
            result = response.json()
            return result['message']['content']
        else:
            raise Exception(f"本地模型调用失败: {response.status_code}")


def get_adapter(config: dict) -> LLMAdapter:
    """根据配置获取适配器"""
    provider = config.get('provider', 'aliyun')

    if provider == 'aliyun':
        return AliyunAdapter(config)
    elif provider == 'openai':
        return OpenAIAdapter(config)
    elif provider == 'local':
        return LocalAdapter(config)
    else:
        raise ValueError(f"未知的提供商: {provider}")


def load_config(config_path: str = None) -> dict:
    """加载配置 - 首先尝试环境变量，然后回退到文件"""
    if config_path is None:
        # 首先尝试从环境变量加载
        env_api_key = os.getenv('DASHSCOPE_API_KEY')
        if env_api_key:
            config = {'model': {}}
            config['model']['provider'] = 'aliyun'
            config['model']['name'] = os.getenv('DASHSCOPE_MODEL', 'qwen-plus')
            config['model']['api_key'] = env_api_key
            config['model']['base_url'] = os.getenv('DASHSCOPE_BASE_URL', 'https://dashscope.aliyuncs.com/api/v1')

            # ASR配置
            if os.getenv('ASR_APP_KEY'):
                config['asr'] = {
                    'provider': 'aliyun',
                    'appkey': os.getenv('ASR_APP_KEY'),
                    'access_key_id': os.getenv('ASR_ACCESS_KEY_ID', ''),
                    'access_key_secret': os.getenv('ASR_ACCESS_KEY_SECRET', ''),
                    'token': os.getenv('ASR_TOKEN', ''),
                    'use_local_whisper': os.getenv('ASR_USE_LOCAL_WHISPER', 'false').lower() == 'true'
                }

            # 飞书配置
            if os.getenv('FEISHU_APP_ID'):
                config['feishu'] = {
                    'app_id': os.getenv('FEISHU_APP_ID'),
                    'app_secret': os.getenv('FEISHU_APP_SECRET', '')
                }

            return config

        # 回退到文件
        base_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(base_dir, 'config', 'settings.json')

    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_llm() -> LLMAdapter:
    """获取配置好的LLM实例"""
    config = load_config()
    return get_adapter(config.get('model', {}))