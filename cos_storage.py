"""
腾讯云 COS 存储适配器
用于在云端存储 wiki 数据
"""

import os
from qcloud_cos import CosConfig, CosS3Client

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
        """上传内容字符串/字节到 COS"""
        try:
            body = content.encode('utf-8') if isinstance(content, str) else content
            self.client.put_object(
                Bucket=self.bucket_name,
                Body=body,
                Key=remote_path
            )
            return True
        except Exception as e:
            print(f"Upload content error: {e}")
            return False

    def download_content(self, remote_path):
        """从 COS 读取内容（返回 bytes，失败返回 None）"""
        try:
            resp = self.client.get_object(
                Bucket=self.bucket_name,
                Key=remote_path
            )
            return resp['Body'].get_raw_stream().read()
        except Exception as e:
            print(f"Download content error ({remote_path}): {e}")
            return None

    def list_files_paged(self, prefix=''):
        """列出 COS 中所有文件（自动翻页）"""
        keys = []
        marker = ''
        while True:
            try:
                response = self.client.list_objects(
                    Bucket=self.bucket_name,
                    Prefix=prefix,
                    Marker=marker,
                    MaxKeys=1000
                )
            except Exception as e:
                print(f"List error: {e}")
                break
            for obj in response.get('Contents', []):
                keys.append(obj['Key'])
            if response.get('IsTruncated') == 'true':
                marker = response.get('NextMarker', '')
                if not marker:
                    break
            else:
                break
        return keys

    def sync_dir_from_cos(self, cos_prefix, local_dir):
        """把 COS 下某前缀的全部文件下载到本地目录"""
        os.makedirs(local_dir, exist_ok=True)
        count = 0
        for key in self.list_files_paged(cos_prefix):
            if key.endswith('/'):
                continue
            rel = key[len(cos_prefix):].lstrip('/')
            if not rel:
                continue
            local_path = os.path.join(local_dir, rel)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            if self.download_file(key, local_path):
                count += 1
        return count

    def sync_dir_to_cos(self, local_dir, cos_prefix):
        """把本地目录全部上传到 COS 某前缀下"""
        if not os.path.isdir(local_dir):
            return 0
        count = 0
        for root, _, files in os.walk(local_dir):
            for fname in files:
                local_path = os.path.join(root, fname)
                rel = os.path.relpath(local_path, local_dir).replace(os.sep, '/')
                key = f"{cos_prefix.rstrip('/')}/{rel}"
                if self.upload_file(local_path, key):
                    count += 1
        return count


_storage_singleton = None

def get_storage():
    """获取 COS 存储实例（单例）"""
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = COSStorage()
    return _storage_singleton