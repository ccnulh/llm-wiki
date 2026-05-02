"""
阿里云Token获取工具
用于获取NLS语音识别服务所需的Token
"""

import hashlib
import hmac
import base64
import urllib.parse
import requests
import json
from datetime import datetime, timezone
import uuid


class AliyunTokenGetter:
    """阿里云Token获取器"""

    def __init__(self, access_key_id: str, access_key_secret: str):
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret

    def get_token(self) -> dict:
        """获取Token"""
        # 方法1: 尝试使用POP API
        result = self._try_pop_api()
        if result.get('success'):
            return result

        # 方法2: 尝试使用NLS Meta API
        result = self._try_nls_meta_api()
        if result.get('success'):
            return result

        return {
            'success': False,
            'error': '无法自动获取Token，请手动从控制台获取',
            'manual_guide': self._get_manual_guide()
        }

    def _try_pop_api(self) -> dict:
        """尝试使用POP API"""
        try:
            from aliyunsdkcore.client import AcsClient
            from aliyunsdkcore.request import CommonRequest

            client = AcsClient(self.access_key_id, self.access_key_secret, 'cn-shanghai')
            request = CommonRequest()
            request.set_domain('nls-meta.cn-shanghai.aliyuncs.com')
            request.set_version('2018-05-25')
            request.set_action_name('CreateToken')
            request.set_method('GET')

            response = client.do_action_with_exception(request)
            result = json.loads(response)

            if 'Token' in result:
                return {
                    'success': True,
                    'token': result['Token']['Id'],
                    'expire_time': result['Token'].get('ExpireTime'),
                    'user_id': result.get('UserId')
                }
        except Exception as e:
            pass

        return {'success': False}

    def _try_nls_meta_api(self) -> dict:
        """尝试使用NLS Meta API"""
        try:
            # 签名参数
            params = {
                'Action': 'CreateToken',
                'Format': 'JSON',
                'Version': '2018-05-25',
                'AccessKeyId': self.access_key_id,
                'SignatureMethod': 'HMAC-SHA1',
                'Timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'SignatureVersion': '1.0',
                'SignatureNonce': str(uuid.uuid4()),
                'RegionId': 'cn-shanghai'
            }

            # 计算签名
            sorted_params = sorted(params.items())
            canonicalized = '&'.join([
                f'{urllib.parse.quote(k, safe="")}={urllib.parse.quote(str(v), safe="")}'
                for k, v in sorted_params
            ])
            string_to_sign = 'GET&%2F&' + urllib.parse.quote(canonicalized, safe='')
            key = (self.access_key_secret + '&').encode('utf-8')
            signature = base64.b64encode(
                hmac.new(key, string_to_sign.encode('utf-8'), hashlib.sha1).digest()
            ).decode('utf-8')
            params['Signature'] = signature

            # 发送请求
            url = 'https://nls-meta.cn-shanghai.aliyuncs.com/'
            response = requests.get(url, params=params, timeout=10)
            result = response.json()

            if 'Token' in result:
                return {
                    'success': True,
                    'token': result['Token']['Id'],
                    'expire_time': result['Token'].get('ExpireTime'),
                    'user_id': result.get('UserId')
                }
        except Exception as e:
            pass

        return {'success': False}

    def _get_manual_guide(self) -> str:
        """获取手动获取Token的指南"""
        return """
手动获取Token步骤：
1. 登录阿里云控制台: https://nls-portal.console.aliyun.com/
2. 点击左侧「项目管理」
3. 找到你的项目，点击「获取Token」
4. 复制生成的Token，粘贴到系统设置中
"""


def get_token_with_access_key(access_key_id: str, access_key_secret: str) -> dict:
    """使用AccessKey获取Token"""
    getter = AliyunTokenGetter(access_key_id, access_key_secret)
    return getter.get_token()


if __name__ == '__main__':
    import sys

    if len(sys.argv) >= 3:
        access_key_id = sys.argv[1]
        access_key_secret = sys.argv[2]
    else:
        # 使用默认配置
        import os
        import json

        config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'settings.json')
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            access_key_id = config.get('asr', {}).get('access_key_id', '')
            access_key_secret = config.get('asr', {}).get('access_key_secret', '')

    if access_key_id and access_key_secret:
        result = get_token_with_access_key(access_key_id, access_key_secret)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("请提供AccessKey ID和Secret")