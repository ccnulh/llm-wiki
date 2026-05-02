"""
Cloudflare R2 存储适配器
用于在云端存储 wiki 数据
"""

import boto3
import os
import json
from botocore.exceptions import ClientError

class R2Storage:
    def __init__(self):
        self.account_id = os.getenv('R2_ACCOUNT_ID')
        self.access_key_id = os.getenv('R2_ACCESS_KEY_ID')
        self.access_key_secret = os.getenv('R2_ACCESS_KEY_SECRET')
        self.bucket_name = os.getenv('R2_BUCKET_NAME', 'llm-wiki')
        self.endpoint = os.getenv('R2_ENDPOINT', f'https://{self.account_id}.r2.cloudflarestorage.com')

        if not all([self.account_id, self.access_key_id, self.access_key_secret]):
            raise ValueError("R2 credentials not configured")

        self.client = boto3.client(
            's3',
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.access_key_secret,
            region_name='auto'
        )

    def upload_file(self, local_path, remote_path):
        """上传本地文件到 R2"""
        try:
            self.client.upload_file(local_path, self.bucket_name, remote_path)
            return True
        except ClientError as e:
            print(f"Upload error: {e}")
            return False

    def download_file(self, remote_path, local_path):
        """从 R2 下载文件到本地"""
        try:
            self.client.download_file(self.bucket_name, remote_path, local_path)
            return True
        except ClientError as e:
            print(f"Download error: {e}")
            return False

    def delete_file(self, remote_path):
        """从 R2 删除文件"""
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=remote_path)
            return True
        except ClientError as e:
            print(f"Delete error: {e}")
            return False

    def list_files(self, prefix=''):
        """列出 R2 中的文件"""
        try:
            response = self.client.list_objects_v2(Bucket=self.bucket_name, Prefix=prefix)
            return [obj['Key'] for obj in response.get('Contents', [])]
        except ClientError as e:
            print(f"List error: {e}")
            return []

    def file_exists(self, remote_path):
        """检查文件是否存在"""
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=remote_path)
            return True
        except ClientError:
            return False


def get_storage():
    """获取 R2 存储实例"""
    return R2Storage()