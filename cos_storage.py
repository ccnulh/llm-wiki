"""
腾讯云 COS 存储适配器
用于在云端存储 wiki 数据
"""

import os
from qcloud_cos import CosConfig, CosS3Client
from botocore.exceptions import ClientError

class COSStorage:
    def __init__(self):
        self.secret_id = os.getenv('COS_SECRET_ID')
        self.secret_key = os.getenv('COS_SECRET_KEY')
        self.bucket_name = os.getenv('COS_BUCKET_NAME')
        self.region = os.getenv('COS_REGION', 'ap-beijing')

        if not all([self.secret_id, self.secret_key, self.bucket_name]):
            raise ValueError("COS credentials not configured")

        config = CosConfig(
            Region=self.region,
            SecretId=self.secret_id,
            SecretKey=self.secret_key
        )
        self.client = CosS3Client(config)

    def upload_file(self, local_path, remote_path):
        """上传本地文件到 COS"""
        try:
            self.client.upload_file(
                Bucket=self.bucket_name,
                Key=remote_path,
                LocalFilePath=local_path
            )
            return True
        except Exception as e:
            print(f"Upload error: {e}")
            return False

    def download_file(self, remote_path, local_path):
        """从 COS 下载文件到本地"""
        try:
            self.client.download_file(
                Bucket=self.bucket_name,
                Key=remote_path,
                DestFilePath=local_path
            )
            return True
        except Exception as e:
            print(f"Download error: {e}")
            return False

    def delete_file(self, remote_path):
        """从 COS 删除文件"""
        try:
            self.client.delete_object(
                Bucket=self.bucket_name,
                Key=remote_path
            )
            return True
        except Exception as e:
            print(f"Delete error: {e}")
            return False

    def list_files(self, prefix=''):
        """列出 COS 中的文件"""
        try:
            response = self.client.list_objects(
                Bucket=self.bucket_name,
                Prefix=prefix
            )
            return [obj['Key'] for obj in response.get('Contents', [])]
        except Exception as e:
            print(f"List error: {e}")
            return []

    def file_exists(self, remote_path):
        """检查文件是否存在"""
        try:
            self.client.head_object(
                Bucket=self.bucket_name,
                Key=remote_path
            )
            return True
        except Exception:
            return False

    def upload_content(self, content, remote_path):
        """上传内容字符串到 COS"""
        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Body=content.encode('utf-8'),
                Key=remote_path
            )
            return True
        except Exception as e:
            print(f"Upload content error: {e}")
            return False


def get_storage():
    """获取 COS 存储实例"""
    return COSStorage()