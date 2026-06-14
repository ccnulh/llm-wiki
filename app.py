"""
LLM Wiki - Flask 主应用
"""

import os
import json
import re
import uuid
import time
import threading
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_cors import CORS

# 导入模块
from importer import get_importer, get_web_fetcher, get_wechat_fetcher, get_video_fetcher, get_smart_fetcher, PodcastFetcher
from compiler import get_compiler
from llm_adapter import get_llm

app = Flask(__name__,
            template_folder='templates',
            static_folder='static')
CORS(app)

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 云端运行时（Render 等无持久磁盘）必须把数据放到 /tmp，并以 COS 为权威备份
IS_CLOUD = bool(os.getenv('RENDER') or os.getenv('CLOUD_RUN') or os.getenv('USE_COS_STORAGE'))
if IS_CLOUD:
    DATA_ROOT = os.getenv('DATA_ROOT', '/tmp/llm-wiki-data')
    RAW_DIR = os.path.join(DATA_ROOT, 'raw')
    WIKI_DIR = os.path.join(DATA_ROOT, 'wiki')
    CONFIG_DIR = os.path.join(DATA_ROOT, 'config')
    META_DIR = os.path.join(DATA_ROOT, 'data')
else:
    RAW_DIR = os.path.join(BASE_DIR, 'raw')
    WIKI_DIR = os.path.join(BASE_DIR, 'wiki')
    CONFIG_DIR = os.path.join(BASE_DIR, 'config')
    META_DIR = os.path.join(BASE_DIR, 'data')

for _d in (RAW_DIR, WIKI_DIR, CONFIG_DIR, META_DIR):
    os.makedirs(_d, exist_ok=True)

# 处理任务存储 (key: task_id, value: status dict)
processing_tasks = {}
processing_lock = threading.Lock()

def get_cos_storage():
    """获取COS存储实例"""
    try:
        from cos_storage import get_storage
        return get_storage()
    except Exception as e:
        print(f"COS not configured: {e}")
        return None


# ============ COS <-> 本地 同步层（云上必须，本地无副作用） ============

_COS_PREFIX_MAP = {
    'raw': RAW_DIR,
    'wiki': WIKI_DIR,
    'config': CONFIG_DIR,
    'data': META_DIR,
}

def _local_to_cos_key(local_path):
    """把本地绝对路径转换成 COS Key；不属于受管目录则返回 None"""
    abs_path = os.path.abspath(local_path)
    for prefix, base in _COS_PREFIX_MAP.items():
        base_abs = os.path.abspath(base)
        if abs_path == base_abs or abs_path.startswith(base_abs + os.sep):
            rel = os.path.relpath(abs_path, base_abs).replace(os.sep, '/')
            return f"{prefix}/{rel}"
    return None

def cos_pull_all():
    """启动时从 COS 拉取所有持久数据到本地；若 COS 为空则把代码自带的种子文件推上去"""
    if not IS_CLOUD:
        return
    storage = get_cos_storage()
    if not storage:
        print("[COS] 未配置，跳过启动同步")
        return
    # 代码内自带的种子目录（随 git 仓库发布）
    seed_root = BASE_DIR
    seed_map = {
        'config': os.path.join(seed_root, 'config'),
    }
    for prefix, local_dir in _COS_PREFIX_MAP.items():
        try:
            n = storage.sync_dir_from_cos(prefix + '/', local_dir)
            print(f"[COS] pull {prefix}/ -> {local_dir}: {n} files")
            # 拉不到任何文件 → 用代码内种子兜底
            if n == 0 and prefix in seed_map and os.path.isdir(seed_map[prefix]):
                import shutil
                for fname in os.listdir(seed_map[prefix]):
                    src = os.path.join(seed_map[prefix], fname)
                    dst = os.path.join(local_dir, fname)
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)
                        try:
                            storage.upload_file(dst, f"{prefix}/{fname}")
                        except Exception as e:
                            print(f"[COS] seed push {fname} failed: {e}")
                print(f"[COS] seeded {prefix}/ from code")
        except Exception as e:
            print(f"[COS] pull {prefix}/ failed: {e}")

def cos_push_file(local_path):
    """把刚写好的本地文件回传 COS（云上才生效，失败不抛错）"""
    if not IS_CLOUD:
        return
    storage = get_cos_storage()
    if not storage:
        return
    key = _local_to_cos_key(local_path)
    if not key:
        return
    try:
        storage.upload_file(local_path, key)
    except Exception as e:
        print(f"[COS] push {local_path} failed: {e}")

def cos_delete_file(local_path):
    """删除本地文件时同步删除 COS"""
    if not IS_CLOUD:
        return
    storage = get_cos_storage()
    if not storage:
        return
    key = _local_to_cos_key(local_path)
    if not key:
        return
    try:
        storage.delete_file(key)
    except Exception as e:
        print(f"[COS] delete {key} failed: {e}")


def _install_cos_autosync():
    """全局拦截 builtins.open / os.remove，使受管目录的写入/删除自动同步到 COS"""
    if not IS_CLOUD:
        return
    import builtins
    _orig_open = builtins.open
    _orig_remove = os.remove

    def _patched_open(file, mode='r', *args, **kwargs):
        f = _orig_open(file, mode, *args, **kwargs)
        # 只在写模式下挂 close 钩子
        if isinstance(file, (str, bytes, os.PathLike)) and any(m in str(mode) for m in ('w', 'a', 'x', '+')):
            try:
                path_str = os.fspath(file)
                if _local_to_cos_key(path_str):
                    _orig_close = f.close
                    def _close_and_push():
                        try:
                            _orig_close()
                        finally:
                            try:
                                cos_push_file(path_str)
                            except Exception as e:
                                print(f"[COS] auto push failed: {e}")
                    f.close = _close_and_push
            except Exception:
                pass
        return f

    def _patched_remove(path, *args, **kwargs):
        try:
            key_path = os.fspath(path)
        except Exception:
            key_path = None
        result = _orig_remove(path, *args, **kwargs)
        if key_path and _local_to_cos_key(key_path):
            try:
                cos_delete_file(key_path)
            except Exception as e:
                print(f"[COS] auto delete failed: {e}")
        return result

    builtins.open = _patched_open
    os.remove = _patched_remove
    print("[COS] auto-sync hooks installed")


# 启动时拉数据 + 安装同步钩子（必须在所有 import 之后、第一个请求之前）
# 注意：cos_pull_all() 要在后台线程做，否则 Render 冷启动时被 COS 拉取阻塞，
# gunicorn 来不及绑端口就被健康检查判 failure。
if IS_CLOUD and get_cos_storage():
    import threading as _bt
    _bt.Thread(target=cos_pull_all, daemon=True).start()
_install_cos_autosync()


def get_config():
    """获取配置（优先从环境变量）"""
    config = {}

    # 阿里云 DashScope
    # LLM 配置：优先火山方舟（ARK），再 OpenAI，再阿里云 DashScope
    provider_env = (os.getenv('LLM_PROVIDER') or '').lower()
    if provider_env in ('volcengine', 'ark') or os.getenv('ARK_API_KEY'):
        config['model'] = {
            'provider': 'volcengine',
            'name': os.getenv('LLM_MODEL', 'glm-5.1'),
            'api_key': os.getenv('ARK_API_KEY', ''),
            'base_url': os.getenv('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3'),
        }
    elif provider_env == 'openai' or os.getenv('OPENAI_API_KEY'):
        config['model'] = {
            'provider': 'openai',
            'name': os.getenv('LLM_MODEL', 'gpt-4'),
            'api_key': os.getenv('OPENAI_API_KEY', ''),
            'base_url': os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1'),
        }
    elif os.getenv('DASHSCOPE_API_KEY'):
        config['model'] = {
            'provider': 'aliyun',
            'name': os.getenv('DASHSCOPE_MODEL', 'qwen-plus'),
            'api_key': os.getenv('DASHSCOPE_API_KEY'),
            'base_url': os.getenv('DASHSCOPE_BASE_URL', 'https://dashscope.aliyuncs.com/api/v1')
        }

    # 阿里云 ASR
    if os.getenv('ASR_APP_KEY'):
        config['asr'] = {
            'provider': 'aliyun',
            'appkey': os.getenv('ASR_APP_KEY'),
            'access_key_id': os.getenv('ASR_ACCESS_KEY_ID', ''),
            'access_key_secret': os.getenv('ASR_ACCESS_KEY_SECRET', ''),
            'token': os.getenv('ASR_TOKEN', ''),
            'use_local_whisper': os.getenv('ASR_USE_LOCAL_WHISPER', 'false').lower() == 'true',
            'whisper_model': os.getenv('WHISPER_MODEL', 'medium'),
            'whisper_device': os.getenv('WHISPER_DEVICE', 'cpu')
        }

    # 飞书
    if os.getenv('FEISHU_APP_ID'):
        config['feishu'] = {
            'app_id': os.getenv('FEISHU_APP_ID'),
            'app_secret': os.getenv('FEISHU_APP_SECRET', '')
        }

    # 编译配置
    config['compile'] = {
        'auto_compile': os.getenv('AUTO_COMPILE', 'true').lower() == 'true',
        'batch_size': int(os.getenv('COMPILE_BATCH_SIZE', '1'))
    }

    return config if config else None

# ============ 页面路由 ============

@app.route('/')
def index():
    """首页 - Wiki浏览"""
    return render_template('index.html')

@app.route('/import')
def import_page():
    """导入页面"""
    return render_template('import.html')

@app.route('/query')
def query_page():
    """问答页面"""
    return render_template('query.html')

@app.route('/settings')
def settings_page():
    """设置页面"""
    return render_template('settings.html')

@app.route('/api/status')
def health_check():
    """健康检查（含版本和环境诊断）"""
    raw_files = []
    if os.path.exists(RAW_DIR):
        try:
            raw_files = os.listdir(RAW_DIR)
        except Exception as e:
            raw_files = [f'<list err: {e}>']
    wiki_dirs = {}
    if os.path.exists(WIKI_DIR):
        try:
            for d in os.listdir(WIKI_DIR):
                full = os.path.join(WIKI_DIR, d)
                if os.path.isdir(full):
                    wiki_dirs[d] = len(os.listdir(full))
        except Exception as e:
            wiki_dirs['<err>'] = str(e)
    return jsonify({
        'status': 'ok',
        'service': 'llm-wiki',
        'is_cloud': IS_CLOUD,
        'data_root': DATA_ROOT if IS_CLOUD else BASE_DIR,
        'llm_provider': os.getenv('LLM_PROVIDER', ''),
        'llm_model': os.getenv('LLM_MODEL', ''),
        'ark_key_set': bool(os.getenv('ARK_API_KEY')),
        'dashscope_key_set': bool(os.getenv('DASHSCOPE_API_KEY')),
        'cos_configured': bool(os.getenv('COS_SECRET_ID')),
        'raw_files': raw_files,
        'wiki_dirs': wiki_dirs,
    })

# ============ API路由 ============

@app.route('/api/wiki/index')
def get_wiki_index():
    """获取Wiki索引"""
    index_path = os.path.join(WIKI_DIR, 'index.md')
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'success': True, 'content': content})
    return jsonify({'success': False, 'error': '索引文件不存在'})

@app.route('/api/wiki/list')
def list_wiki_pages():
    """列出Wiki页面（支持多种类型）"""
    pages = []
    page_types = {
        'summaries': 'summary',
        'concepts': 'concept',
        'entities': 'entity',
        'projects': 'project',
        'insights': 'insight',
        'topics': 'topic'
    }

    for dir_name, page_type in page_types.items():
        dir_path = os.path.join(WIKI_DIR, dir_name)
        if os.path.exists(dir_path):
            for f in os.listdir(dir_path):
                if f.endswith('.md'):
                    pages.append({
                        'type': page_type,
                        'name': f,
                        'path': f'/api/wiki/page/{dir_name}/{f}'
                    })

    return jsonify({'success': True, 'pages': pages})

@app.route('/api/wiki/page/<category>/<filename>')
def get_wiki_page(category, filename):
    """获取Wiki页面内容"""
    page_path = os.path.join(WIKI_DIR, category, filename)
    if os.path.exists(page_path):
        with open(page_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'success': True, 'content': content, 'filename': filename})
    return jsonify({'success': False, 'error': '页面不存在'})

@app.route('/api/wiki/delete/<category>/<filename>', methods=['DELETE'])
def delete_wiki_page(category, filename):
    """删除Wiki页面"""
    allowed_categories = ['concepts', 'entities', 'projects', 'insights', 'topics']
    if category not in allowed_categories:
        return jsonify({'success': False, 'error': '不允许删除此类型的页面'})

    page_path = os.path.join(WIKI_DIR, category, filename)

    if not os.path.abspath(page_path).startswith(os.path.abspath(WIKI_DIR)):
        return jsonify({'success': False, 'error': '非法路径'})

    if os.path.exists(page_path):
        os.remove(page_path)
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '页面不存在'})

@app.route('/api/wiki/cleanup-orphans', methods=['POST'])
def cleanup_orphans():
    """清理孤立页面"""
    deleted = []

    summaries_dir = os.path.join(WIKI_DIR, 'summaries')
    existing_summaries = set()
    if os.path.exists(summaries_dir):
        for f in os.listdir(summaries_dir):
            if f.endswith('.md'):
                existing_summaries.add(f[:-3])

    for category in ['concepts', 'entities', 'projects', 'insights', 'topics']:
        category_dir = os.path.join(WIKI_DIR, category)
        if not os.path.exists(category_dir):
            continue

        for page_file in os.listdir(category_dir):
            if not page_file.endswith('.md'):
                continue

            page_path = os.path.join(category_dir, page_file)
            with open(page_path, 'r', encoding='utf-8') as f:
                content = f.read()

            import re
            source_match = re.search(r'source:\s*\[\[summaries/([^\]]+)\]\]', content)
            if source_match:
                summary_name = source_match.group(1)
                if summary_name not in existing_summaries:
                    os.remove(page_path)
                    deleted.append(page_file)

    return jsonify({'success': True, 'deleted': deleted})

@app.route('/api/raw/list')
def list_raw_files():
    """列出原始素材（带元数据）"""
    files = []
    if os.path.exists(RAW_DIR):
        all_names = set(os.listdir(RAW_DIR))
        for f in sorted(all_names):
            file_path = os.path.join(RAW_DIR, f)
            if not os.path.isfile(file_path):
                continue
            ext = os.path.splitext(f)[1].lower()
            # 列出 .md 提取版 + 没有 .md 提取版的二进制原文
            is_md = ext in ('.md', '.markdown')
            stem = os.path.splitext(f)[0]
            has_md_sibling = (stem + '.md') in all_names or (stem + '.markdown') in all_names
            if not is_md and has_md_sibling:
                continue  # 二进制原文已有 .md 提取版，不重复显示

            metadata = {'title': f, 'source': None, 'imported_at': None}
            if is_md:
                try:
                    # 自己手写一个 frontmatter 行解析：避免 YAML 因 source 里包含冒号或截断而抛错
                    with open(file_path, 'r', encoding='utf-8') as fp:
                        head = fp.read(4096)
                    if head.startswith('---'):
                        body = head[3:]
                        end = body.find('\n---')
                        fm_block = body[:end] if end != -1 else body
                        for line in fm_block.splitlines():
                            line = line.strip()
                            if not line or ':' not in line:
                                continue
                            key, _, val = line.partition(':')
                            key = key.strip().lower()
                            val = val.strip()
                            if key in ('title', 'source', 'imported_at') and val:
                                metadata[key] = val
                except Exception:
                    pass

            files.append({
                'name': f,
                'filename': f,
                'size': os.path.getsize(file_path),
                'imported_at': metadata['imported_at'],
                'source': metadata['source'],
                'title': metadata['title'],
                'type': 'text' if is_md else ext.lstrip('.') or 'binary'
            })

    return jsonify({'success': True, 'files': files})

@app.route('/api/raw/delete/<filename>', methods=['DELETE'])
def delete_raw_file(filename):
    """删除原始素材及其关联的Wiki页面"""
    file_path = os.path.join(RAW_DIR, filename)

    if not os.path.abspath(file_path).startswith(os.path.abspath(RAW_DIR)):
        return jsonify({'success': False, 'error': '非法路径'})

    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': '文件不存在'})

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    import frontmatter
    try:
        parsed = frontmatter.loads(content)
        title = parsed.get('title', '')
    except:
        title = filename

    os.remove(file_path)

    related_pages = []

    summaries_dir = os.path.join(WIKI_DIR, 'summaries')
    if os.path.exists(summaries_dir):
        for page_file in os.listdir(summaries_dir):
            if not page_file.endswith('.md'):
                continue
            page_path = os.path.join(summaries_dir, page_file)
            with open(page_path, 'r', encoding='utf-8') as fp:
                page_content = fp.read()
            if title in page_content:
                os.remove(page_path)
                related_pages.append(page_file)

    for category in ['concepts', 'entities', 'projects', 'insights', 'topics']:
        category_dir = os.path.join(WIKI_DIR, category)
        if os.path.exists(category_dir):
            for page_file in os.listdir(category_dir):
                if not page_file.endswith('.md'):
                    continue
                page_path = os.path.join(category_dir, page_file)
                with open(page_path, 'r', encoding='utf-8') as fp:
                    page_content = fp.read()
                if f'[[summaries/{title}]]' in page_content or f'[[summaries/{title.replace("_", "-")}]]' in page_content:
                    os.remove(page_path)
                    related_pages.append(page_file)

    return jsonify({'success': True, 'deleted_raw': filename, 'deleted_pages': related_pages})

@app.route('/api/raw/view/<path:filename>')
def view_raw_file(filename):
    """查看原始素材内容"""
    file_path = os.path.join(RAW_DIR, filename)

    if not os.path.abspath(file_path).startswith(os.path.abspath(RAW_DIR)):
        return jsonify({'success': False, 'error': '非法路径'})

    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': '文件不存在'})

    # 二进制原文（PDF/Word/音视频/图片）：返回提示，引导查看同名 .md
    text_exts = ('.md', '.markdown', '.txt', '.json', '.csv', '.log', '.html', '.htm')
    if not filename.lower().endswith(text_exts):
        # 尝试找同名 .md 提取版
        stem = os.path.splitext(file_path)[0]
        for cand in (stem + '.md', file_path + '.md'):
            if os.path.exists(cand):
                try:
                    with open(cand, 'r', encoding='utf-8') as f:
                        content = f.read()
                    return jsonify({'success': True, 'content': content,
                                    'filename': os.path.basename(cand)})
                except Exception as e:
                    return jsonify({'success': False, 'error': f'读取失败: {e}'})
        return jsonify({'success': False,
                        'error': f'这是二进制文件（{os.path.splitext(filename)[1]}），无在线预览；可在「素材列表」找它的 .md 提取版'})

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'success': True, 'content': content, 'filename': filename})
    except UnicodeDecodeError:
        return jsonify({'success': False, 'error': '文件不是 UTF-8 文本，无法预览'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'读取失败: {e}'})

@app.route('/api/config/get')
def get_config_api():
    """获取配置"""
    config = get_config()
    if config:
        safe_config = config.copy()
        if 'model' in safe_config and 'api_key' in safe_config['model']:
            safe_config['model']['api_key'] = '******'
        if 'asr' in safe_config and 'access_key_secret' in safe_config['asr']:
            safe_config['asr']['access_key_secret'] = '******'
        if 'feishu' in safe_config and 'app_secret' in safe_config['feishu']:
            safe_config['feishu']['app_secret'] = '******'
        return jsonify({'success': True, 'config': safe_config})

    config_path = os.path.join(CONFIG_DIR, 'settings.json')
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if 'model' in config and 'api_key' in config['model']:
            config['model']['api_key'] = '******' if config['model']['api_key'] else ''
        if 'asr' in config:
            if 'access_key_secret' in config['asr']:
                config['asr']['access_key_secret'] = '******' if config['asr']['access_key_secret'] else ''
        if 'feishu' in config:
            if 'app_secret' in config['feishu']:
                config['feishu']['app_secret'] = '******' if config['feishu']['app_secret'] else ''
        return jsonify({'success': True, 'config': config})
    return jsonify({'success': False, 'error': '配置文件不存在'})

@app.route('/api/config/save', methods=['POST'])
def save_config_api():
    """保存配置"""
    config_path = os.path.join(CONFIG_DIR, 'settings.json')
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        existing = {}
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                existing = json.load(f)

        new_config = request.json

        if 'model' in new_config:
            if 'model' not in existing:
                existing['model'] = {}
            for sub_key in ['provider', 'name', 'base_url', 'api_key']:
                if sub_key in new_config['model']:
                    val = new_config['model'][sub_key]
                    if sub_key == 'api_key' and (not val or val == '******'):
                        val = existing['model'].get('api_key', '')
                    existing['model'][sub_key] = val

        if 'asr' in new_config:
            if 'asr' not in existing:
                existing['asr'] = {}
            for sub_key in ['appkey', 'access_key_id', 'access_key_secret', 'token']:
                if sub_key in new_config['asr']:
                    val = new_config['asr'][sub_key]
                    if sub_key == 'access_key_secret' and (not val or val == '******'):
                        val = existing['asr'].get('access_key_secret', '')
                    existing['asr'][sub_key] = val

        if 'compile' in new_config:
            existing['compile'] = new_config['compile']

        if 'lint' in new_config:
            existing['lint'] = new_config['lint']

        if 'feishu' in new_config:
            if 'feishu' not in existing:
                existing['feishu'] = {}
            for sub_key in ['app_id', 'app_secret']:
                if sub_key in new_config['feishu']:
                    val = new_config['feishu'][sub_key]
                    if sub_key == 'app_secret' and (not val or val == '******'):
                        val = existing['feishu'].get('app_secret', '')
                    existing['feishu'][sub_key] = val

        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

def _history_file_path():
    """导入历史文件路径：云上放 DATA_ROOT/data，本地放 BASE_DIR/data"""
    base = DATA_ROOT if IS_CLOUD else BASE_DIR
    return os.path.join(base, 'data', 'import_history.json')

@app.route('/api/import/history', methods=['GET'])
def get_import_history():
    """获取导入历史"""
    try:
        history_file = _history_file_path()
        # 云上若本地缺失，先尝试从 COS 拉取一次（容错 worker 间内存隔离/重启后未拉取）
        if IS_CLOUD and not os.path.exists(history_file):
            try:
                storage = get_cos_storage()
                if storage:
                    key = _local_to_cos_key(history_file)
                    if key:
                        os.makedirs(os.path.dirname(history_file), exist_ok=True)
                        storage.download_file(key, history_file)
            except Exception as e:
                print(f"[history] pull from COS failed: {e}")

        if os.path.exists(history_file):
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)

            # 把僵尸 uploading 记录（超过 10 分钟还没出终态，多半是后端进程被重启了）标成 error
            try:
                from datetime import timedelta
                now = datetime.now()
                stale_cutoff = timedelta(minutes=10)
                changed = False
                for item in history:
                    if item.get('status') == 'uploading':
                        ts = item.get('timestamp', '')
                        try:
                            t = datetime.fromisoformat(ts)
                            if now - t > stale_cutoff:
                                item['status'] = 'error'
                                item['message'] = '⚠️ 已中断（服务重启或超时），请重新上传'
                                changed = True
                        except Exception:
                            pass
                if changed:
                    with open(history_file, 'w', encoding='utf-8') as f:
                        json.dump(history, f, ensure_ascii=False, indent=2)
                    try:
                        cos_push_file(history_file)
                    except Exception:
                        pass
            except Exception:
                pass

            return jsonify({'success': True, 'history': history})
        return jsonify({'success': True, 'history': []})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/import/history', methods=['POST'])
def save_import_history():
    """保存导入历史；同一 filename 的进行中记录会被覆盖（避免堆积重复条目）"""
    try:
        data = request.json or {}
        filename = data.get('filename', '')
        status = data.get('status', 'success')
        message = data.get('message', '')
        pages_created = data.get('pages_created', 0)

        history_file = _history_file_path()
        os.makedirs(os.path.dirname(history_file), exist_ok=True)

        # 写之前先尝试拉一次远端（避免本地丢了之后又把空记录推回 COS 覆盖正确数据）
        if IS_CLOUD and not os.path.exists(history_file):
            try:
                storage = get_cos_storage()
                if storage:
                    key = _local_to_cos_key(history_file)
                    if key:
                        storage.download_file(key, history_file)
            except Exception as e:
                print(f"[history] pre-write pull failed: {e}")

        existing = []
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        # 同名记录已存在：若是进行中→更新；若已是终态→不再回写为进行中
        terminal = ('success', 'error')
        merged = []
        replaced = False
        for item in existing:
            if item.get('filename') == filename and not replaced:
                # 已经终态的不允许被进行中覆盖
                if item.get('status') in terminal and status not in terminal:
                    merged.append(item)
                else:
                    merged.append({
                        'filename': filename,
                        'status': status,
                        'message': message,
                        'timestamp': datetime.now().isoformat(),
                        'pages_created': pages_created or item.get('pages_created', 0),
                    })
                replaced = True
            else:
                merged.append(item)

        if not replaced:
            merged.insert(0, {
                'filename': filename,
                'status': status,
                'message': message,
                'timestamp': datetime.now().isoformat(),
                'pages_created': pages_created,
            })

        merged = merged[:100]

        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

        # 显式再 push 一次 COS，确保 monkey-patch 失效时也能持久化
        try:
            cos_push_file(history_file)
        except Exception as e:
            print(f"[history] explicit cos push failed: {e}")

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/diag/cos')
def diag_cos():
    """诊断接口：返回 COS 配置状态以及关键文件是否存在，用来排查持久化丢失"""
    info = {
        'IS_CLOUD': IS_CLOUD,
        'DATA_ROOT': DATA_ROOT if IS_CLOUD else BASE_DIR,
        'env': {
            'COS_SECRET_ID': bool(os.getenv('COS_SECRET_ID')),
            'COS_SECRET_KEY': bool(os.getenv('COS_SECRET_KEY')),
            'COS_BUCKET_NAME': os.getenv('COS_BUCKET_NAME') or '',
            'COS_REGION': os.getenv('COS_REGION') or '',
            'USE_COS_STORAGE': os.getenv('USE_COS_STORAGE') or '',
            'RENDER': bool(os.getenv('RENDER')),
        },
    }
    storage = get_cos_storage()
    info['cos_storage_ready'] = storage is not None
    history_file = _history_file_path()
    info['history_file'] = history_file
    info['history_file_exists_local'] = os.path.exists(history_file)
    if storage:
        try:
            key = _local_to_cos_key(history_file)
            info['history_cos_key'] = key
            info['history_file_exists_cos'] = storage.file_exists(key) if key else False
            info['cos_raw_count'] = len(storage.list_files_paged('raw/'))
            info['cos_data_count'] = len(storage.list_files_paged('data/'))
        except Exception as e:
            info['cos_check_error'] = str(e)
    return jsonify(info)

@app.route('/api/diag/ytdlp')
def diag_ytdlp():
    """诊断 yt-dlp 是否可用 + 抓取目标 URL 的真实错误"""
    import subprocess, shutil
    info = {
        'yt_dlp_path': shutil.which('yt-dlp'),
    }
    try:
        v = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=10)
        info['version'] = (v.stdout or v.stderr).strip()
    except Exception as e:
        info['version_error'] = str(e)
    test_url = request.args.get('url', '')
    if test_url:
        try:
            r = subprocess.run(
                ['yt-dlp', '--dump-json', '--no-download', '--no-warnings', test_url],
                capture_output=True, text=True, timeout=30
            )
            info['returncode'] = r.returncode
            info['stderr_tail'] = (r.stderr or '')[-1500:]
            info['stdout_len'] = len(r.stdout or '')
        except Exception as e:
            info['run_error'] = str(e)
    return jsonify(info)


@app.route('/api/import/file', methods=['POST'])
def import_file():
    """导入本地文件（小于5MB直接处理）"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': '没有上传文件'})

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': '没有选择文件'})

        config = get_config() or {}
        importer = get_importer(RAW_DIR, config)
        result = importer.import_file(file.read(), file.filename)

        if result.get('success'):
            try:
                compiler = get_compiler()
                compile_result = compiler.compile_all()
                result['compile'] = compile_result
            except Exception as e:
                result['compile_error'] = str(e)

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/import/large-file/init', methods=['POST'])
def init_large_file_upload():
    """初始化大文件上传（返回预签名URL或直接处理）"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': '没有上传文件'})

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': '没有选择文件'})

        file_size = request.content_length or 0
        filename = file.filename

        # 生成唯一任务ID
        task_id = str(uuid.uuid4())

        # 如果文件大于5MB且有COS配置，使用COS上传模式
        storage = get_cos_storage()
        if storage and file_size > 5 * 1024 * 1024:
            # 先检查文件大小 - 如果太大直接返回错误避免超时
            if file_size > 100 * 1024 * 1024:  # > 100MB
                return jsonify({'success': False, 'error': '文件太大，请压缩后再上传'})

            # 保存到本地临时目录
            temp_dir = os.path.join(BASE_DIR, 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            local_path = os.path.join(temp_dir, f"{task_id}_{filename}")

            # 使用安全方式保存，不直接传文件对象
            try:
                file.save(local_path)
            except Exception as save_error:
                return jsonify({'success': False, 'error': f'文件保存失败: {str(save_error)}'})

            # 上传到COS
            cos_key = f"uploads/{task_id}/{filename}"
            if storage.upload_file(local_path, cos_key):
                # 删除本地文件
                os.remove(local_path)

                # 创建处理任务
                with processing_lock:
                    processing_tasks[task_id] = {
                        'status': 'uploading',
                        'filename': filename,
                        'cos_key': cos_key,
                        'progress': 0,
                        'message': '文件上传中...'
                    }

                return jsonify({
                    'success': True,
                    'task_id': task_id,
                    'mode': 'cos',
                    'message': '文件已上传，开始处理...'
                })
            else:
                return jsonify({'success': False, 'error': '文件上传失败'})

        # 小文件路径：保存内容到内存任务里，异步处理，立即返回 task_id
        # 这里避免同步跑 import+compile 触发 Render 30s worker 超时（→ 502 空响应 → 前端 JSON 解析报错）
        file_content = file.read()
        with processing_lock:
            processing_tasks[task_id] = {
                'status': 'pending',
                'filename': filename,
                'progress': 0,
                'message': '已接收，等待处理...',
                '_inline_content': file_content,  # 仅小文件路径用
            }

        return jsonify({
            'success': True,
            'task_id': task_id,
            'mode': 'inline',
            'message': '已接收，处理中...'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/import/large-file/status/<task_id>')
def get_large_file_status(task_id):
    """获取大文件处理状态"""
    with processing_lock:
        if task_id in processing_tasks:
            return jsonify({'success': True, 'task': processing_tasks[task_id]})
        return jsonify({'success': False, 'error': '任务不存在'})

@app.route('/api/import/large-file/process/<task_id>', methods=['POST'])
def process_large_file(task_id):
    """触发文件异步处理（既支持 COS 上传的大文件，也支持内联的小文件）"""
    with processing_lock:
        if task_id not in processing_tasks:
            return jsonify({'success': False, 'error': '任务不存在'})

    task = None
    with processing_lock:
        task = processing_tasks[task_id]

    is_inline = '_inline_content' in task
    storage = None
    if not is_inline:
        storage = get_cos_storage()
        if not storage:
            return jsonify({'success': False, 'error': 'COS未配置'})

    def process_file():
        try:
            with processing_lock:
                processing_tasks[task_id]['status'] = 'processing'
                processing_tasks[task_id]['progress'] = 10
                processing_tasks[task_id]['message'] = '准备文件...'

            if is_inline:
                file_content = task['_inline_content']
                # 处理完释放内存
                with processing_lock:
                    processing_tasks[task_id].pop('_inline_content', None)
            else:
                # 下载文件到临时目录
                temp_dir = os.path.join(BASE_DIR, 'temp')
                os.makedirs(temp_dir, exist_ok=True)
                local_path = os.path.join(temp_dir, task['filename'])

                with processing_lock:
                    processing_tasks[task_id]['progress'] = 20
                    processing_tasks[task_id]['message'] = '下载文件...'

                cos_key = task['cos_key']
                storage.download_file(cos_key, local_path)

                with processing_lock:
                    processing_tasks[task_id]['progress'] = 40
                    processing_tasks[task_id]['message'] = '提取内容...'

                with open(local_path, 'rb') as f:
                    file_content = f.read()

                os.remove(local_path)

            # 判断文件类型并导入
            filename = task['filename'].lower()
            config = get_config() or {}
            importer = get_importer(RAW_DIR, config)

            with processing_lock:
                processing_tasks[task_id]['progress'] = 60
                processing_tasks[task_id]['message'] = '导入内容...'

            if filename.endswith('.pdf'):
                result = importer.import_pdf(file_content, task['filename'])
            else:
                result = importer.import_file(file_content, task['filename'])

            # 清理COS文件（仅大文件模式）
            if not is_inline:
                try:
                    storage.delete_file(task['cos_key'])
                except:
                    pass

            with processing_lock:
                processing_tasks[task_id]['progress'] = 80
                processing_tasks[task_id]['message'] = '生成知识页面...'

            if result.get('success'):
                try:
                    compiler = get_compiler()
                    # 只编译刚导入的这个文件，不要每次都遍历整个 raw 目录
                    just_imported = result.get('filename')
                    if just_imported:
                        compile_result = compiler.compile_one(just_imported)
                        # 统一一下返回结构，保持和 compile_all 一致
                        compile_result = {
                            'processed': 1 if compile_result.get('success') else 0,
                            'pages_created': compile_result.get('pages_created', 0),
                            'errors': [] if compile_result.get('success') else [compile_result.get('error', '')],
                        }
                        # 更新索引
                        try:
                            compiler._update_index()
                        except Exception:
                            pass
                    else:
                        compile_result = compiler.compile_all()
                    result['compile'] = compile_result
                except Exception as e:
                    result['compile_error'] = str(e)

            with processing_lock:
                if not result.get('success', False):
                    processing_tasks[task_id]['status'] = 'failed'
                    processing_tasks[task_id]['progress'] = 100
                    processing_tasks[task_id]['message'] = result.get('error', '解析失败')
                    processing_tasks[task_id]['result'] = {
                        'success': False,
                        'error': result.get('error', '解析失败'),
                        'pages_created': 0,
                    }
                else:
                    processing_tasks[task_id]['status'] = 'completed'
                    processing_tasks[task_id]['progress'] = 100
                    processing_tasks[task_id]['message'] = '处理完成'
                    processing_tasks[task_id]['result'] = {
                        'success': True,
                        'filename': result.get('filename', ''),
                        'pages_created': result.get('compile', {}).get('pages_created', 0) if result.get('compile') else 0
                    }

        except Exception as e:
            with processing_lock:
                processing_tasks[task_id]['status'] = 'failed'
                processing_tasks[task_id]['message'] = f'处理失败: {str(e)}'

    # 启动后台线程处理
    thread = threading.Thread(target=process_file)
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'message': '开始处理文件'})

@app.route('/api/import/url', methods=['POST'])
def import_url():
    """智能抓取链接内容（支持网页、视频、公众号、播客音频）"""
    try:
        data = request.json
        url = data.get('url')

        if not url:
            return jsonify({'success': False, 'error': 'URL不能为空'})

        # 小宇宙播客单集
        if 'xiaoyuzhoufm.com/episode/' in url:
            try:
                import requests
                headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                resp = requests.get(url, headers=headers, timeout=10)

                # 优先：从 __NEXT_DATA__ 解析（最稳）
                episode = None
                m_next = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', resp.text, re.S)
                if m_next:
                    try:
                        next_data = json.loads(m_next.group(1))
                        page_props = (next_data.get('props') or {}).get('pageProps') or {}
                        if page_props.get('statusCode') and page_props['statusCode'] != 200:
                            return jsonify({'success': False, 'error': f'小宇宙返回错误：{page_props["statusCode"]}（链接可能已失效）'})
                        episode = page_props.get('episode')
                    except Exception:
                        pass

                title = (episode or {}).get('title') or '小宇宙播客'
                audio_url = ((episode or {}).get('enclosure') or {}).get('url') \
                            or (((episode or {}).get('media') or {}).get('source') or {}).get('url')
                shownotes_html = (episode or {}).get('shownotes') or ''
                description_text = (episode or {}).get('description') or ''
                podcast_title = (((episode or {}).get('podcast') or {}).get('title')) or ''

                # 兜底：旧版 regex
                if not audio_url:
                    m = re.search(r'https://media\.xyzcdn\.net/[^"]+\.m4a', resp.text)
                    if m:
                        audio_url = m.group(0)

                # shownotes 转纯文本（去 HTML 标签）
                shownotes_text = ''
                if shownotes_html:
                    try:
                        from bs4 import BeautifulSoup
                        shownotes_text = BeautifulSoup(shownotes_html, 'html.parser').get_text('\n', strip=True)
                    except Exception:
                        shownotes_text = re.sub(r'<[^>]+>', '', shownotes_html).strip()

                # 决定走哪条路：有 ASR 配置 → 返回 audio_url 让前端走音频转写
                # 否则 → 直接把 shownotes 当内容导入（务实兜底）
                config_check = get_config() or {}
                has_asr = bool((config_check.get('asr') or {}).get('appkey')) or bool(os.getenv('ASR_APP_KEY'))

                if has_asr and audio_url:
                    return jsonify({
                        'success': True,
                        'audio_url': audio_url,
                        'title': title,
                        'source': url,
                        'type': 'podcast'
                    })

                # 无 ASR：用 shownotes + description 当内容直接导入
                content_pieces = [f"# {title}"]
                if podcast_title:
                    content_pieces.append(f"**播客**：{podcast_title}")
                content_pieces.append(f"**链接**：{url}")
                content_pieces.append('')
                if shownotes_text:
                    content_pieces.append('## 节目说明')
                    content_pieces.append(shownotes_text)
                elif description_text:
                    content_pieces.append('## 简介')
                    content_pieces.append(description_text)
                else:
                    return jsonify({'success': False, 'error': '页面没有 shownotes，且未配置 ASR，无法导入。请在环境变量配置 ASR_APP_KEY 等开启音频转写'})

                if audio_url:
                    content_pieces.append('')
                    content_pieces.append(f"> 注：本集音频地址 {audio_url}（未配置 ASR，仅导入节目文字说明；如需完整文字稿请配置 ASR_APP_KEY）")

                final_content = '\n\n'.join(content_pieces)

                importer_inst = get_importer(RAW_DIR, config_check)
                import_result = importer_inst.import_text(final_content, title, f'小宇宙: {url}')
                try:
                    compiler = get_compiler()
                    if hasattr(compiler, 'compile_one') and import_result.get('filename'):
                        import_result['compile'] = compiler.compile_one(import_result['filename'])
                    else:
                        import_result['compile'] = compiler.compile_all()
                except Exception as e:
                    import_result['compile_error'] = str(e)
                import_result['fetch_type'] = 'podcast_shownotes'
                return jsonify(import_result)
            except Exception as e:
                return jsonify({'success': False, 'error': f'小宇宙抓取失败: {str(e)}'})

        fetcher = get_smart_fetcher()
        result = fetcher.fetch(url)

        if result['success']:
            config = get_config() or {}
            importer = get_importer(RAW_DIR, config)
            import_result = importer.import_text(
                result['content'],
                result['title'],
                result['source']
            )

            try:
                compiler = get_compiler()
                compile_result = compiler.compile_all()
                import_result['compile'] = compile_result
            except Exception as e:
                import_result['compile_error'] = str(e)

            if 'platform' in result:
                import_result['fetch_type'] = 'video'
                import_result['has_transcript'] = result.get('has_transcript', False)
            elif 'fetch_type' not in import_result:
                import_result['fetch_type'] = 'web'
            return jsonify(import_result)
        else:
            return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/import/wechat', methods=['POST'])
def import_wechat():
    """抓取微信公众号文章"""
    try:
        data = request.json
        url = data.get('url')

        if not url:
            return jsonify({'success': False, 'error': 'URL不能为空'})

        fetcher = get_wechat_fetcher()
        result = fetcher.fetch_article(url)

        if result['success']:
            config = get_config() or {}
            importer = get_importer(RAW_DIR, config)
            import_result = importer.import_text(
                result['content'],
                result['title'],
                result['source']
            )

            if import_result.get('success'):
                try:
                    compiler = get_compiler()
                    compile_result = compiler.compile_all()
                    import_result['compile'] = compile_result
                except Exception as e:
                    import_result['compile_error'] = str(e)

            return jsonify(import_result)
        else:
            return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/import/pdf', methods=['POST'])
def import_pdf():
    """导入PDF文档"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': '没有上传文件'})

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': '没有选择文件'})

        importer = get_importer(RAW_DIR)
        result = importer.import_pdf(file.read(), file.filename)

        if result.get('success'):
            try:
                compiler = get_compiler()
                compile_result = compiler.compile_all()
                result['compile'] = compile_result
            except Exception as e:
                result['compile_error'] = str(e)

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/import/podcast/episode', methods=['POST'])
def import_podcast_episode():
    """导入播客单集（下载音频 + ASR 转写，整条流程异步执行）"""
    try:
        data = request.json or {}
        audio_url = data.get('audio_url')
        title = data.get('title', '播客单集')
        podcast_title = data.get('podcast_title', '')

        if not audio_url:
            return jsonify({'success': False, 'error': '音频URL不能为空'})

        task_id = str(uuid.uuid4())
        with processing_lock:
            processing_tasks[task_id] = {
                'status': 'pending',
                'filename': f'{podcast_title}_{title}'.strip('_') or title,
                'progress': 0,
                'message': '已接收，准备下载音频...',
            }

        def run():
            audio_path = None
            try:
                with processing_lock:
                    processing_tasks[task_id]['status'] = 'processing'
                    processing_tasks[task_id]['progress'] = 10
                    processing_tasks[task_id]['message'] = '下载音频中...'

                import requests as _rq
                headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'}
                # 流式下载，加逐 chunk 超时和总时间上限
                download_deadline = time.time() + 180  # 最多 3 分钟下载
                with _rq.get(audio_url, headers=headers, timeout=(15, 30), stream=True) as audio_resp:
                    if audio_resp.status_code != 200:
                        raise RuntimeError(f'音频下载失败 HTTP {audio_resp.status_code}')
                    timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
                    safe_pt = re.sub(r'[\\/:*?"<>|]', '_', podcast_title or '')
                    safe_t = re.sub(r'[\\/:*?"<>|]', '_', title or '')
                    audio_filename = f"{timestamp}_{safe_pt}_{safe_t}.m4a"
                    audio_path = os.path.join(RAW_DIR, audio_filename)
                    downloaded = 0
                    last_progress_time = time.time()
                    with open(audio_path, 'wb') as f:
                        for chunk in audio_resp.iter_content(chunk_size=1024 * 256):
                            if time.time() > download_deadline:
                                raise RuntimeError('音频下载超时（3分钟），海外服务器访问国内CDN可能受限')
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                # 每 5MB 更新一次进度提示
                                now = time.time()
                                if now - last_progress_time > 10:
                                    last_progress_time = now
                                    with processing_lock:
                                        processing_tasks[task_id]['message'] = f'下载音频中... {downloaded // (1024*1024)}MB'
                if downloaded < 1000:
                    raise RuntimeError('音频文件太小，可能下载不完整')

                with processing_lock:
                    processing_tasks[task_id]['progress'] = 40
                    processing_tasks[task_id]['message'] = 'AI 转写中（可能需要几分钟）...'

                config_inner = get_config() or {}
                importer_inner = get_importer(RAW_DIR, config_inner)
                transcript = importer_inner._speech_to_text(audio_path)

                # 删除原音频
                try:
                    if audio_path and os.path.exists(audio_path):
                        os.remove(audio_path)
                except Exception:
                    pass

                # _speech_to_text 失败时会返回 "[错误: ...]" 之类的占位串而不是空 → 显式识别
                if not transcript:
                    raise RuntimeError('转写失败（无返回文本）')
                t_stripped = transcript.strip()
                if t_stripped.startswith('[错误') or t_stripped.startswith('[语音识别失败') or t_stripped.startswith('[音频'):
                    raise RuntimeError(f'ASR 返回错误：{t_stripped[:300]}')

                with processing_lock:
                    processing_tasks[task_id]['progress'] = 70
                    processing_tasks[task_id]['message'] = '生成知识页面...'

                import_result = importer_inner.import_text(
                    transcript, title, f'播客: {podcast_title}'
                )
                if import_result.get('success'):
                    try:
                        compiler = get_compiler()
                        if hasattr(compiler, 'compile_one') and import_result.get('filename'):
                            import_result['compile'] = compiler.compile_one(import_result['filename'])
                        else:
                            import_result['compile'] = compiler.compile_all()
                    except Exception as e:
                        import_result['compile_error'] = str(e)

                with processing_lock:
                    processing_tasks[task_id]['status'] = 'completed'
                    processing_tasks[task_id]['progress'] = 100
                    processing_tasks[task_id]['message'] = '处理完成'
                    processing_tasks[task_id]['result'] = import_result
            except Exception as e:
                with processing_lock:
                    processing_tasks[task_id]['status'] = 'completed'
                    processing_tasks[task_id]['message'] = f'处理失败: {e}'
                    processing_tasks[task_id]['result'] = {'success': False, 'error': str(e)}
                # 清理半成品
                try:
                    if audio_path and os.path.exists(audio_path):
                        os.remove(audio_path)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()

        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': '已开始下载与转写，请轮询 /api/import/large-file/status/<task_id>'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/import/image', methods=['POST'])
def import_image():
    """导入图片（OCR识别）"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': '没有上传文件'})

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': '没有选择文件'})

        config = get_config() or {}
        importer = get_importer(RAW_DIR, config)
        result = importer.import_image(file.read(), file.filename)

        if result.get('success'):
            try:
                compiler = get_compiler()
                compile_result = compiler.compile_all()
                result['compile'] = compile_result
            except Exception as e:
                result['compile_error'] = str(e)

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/import/podcast/rss', methods=['POST'])
def import_podcast_rss():
    """导入播客RSS"""
    try:
        data = request.json
        url = data.get('url')

        if not url:
            return jsonify({'success': False, 'error': 'URL不能为空'})

        fetcher = PodcastFetcher()
        result = fetcher.fetch_rss(url)

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/wiki/compile', methods=['POST'])
def compile_wiki():
    """触发Wiki编译"""
    try:
        compiler = get_compiler()
        result = compiler.compile_all()
        return jsonify({
            'success': True,
            'processed': result['processed'],
            'pages_created': result['pages_created'],
            'errors': result.get('errors', [])
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/export')
def export_wiki():
    """导出Wiki数据"""
    import zipfile
    from datetime import datetime

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    zip_filename = f'llm_wiki_export_{timestamp}.zip'
    temp_dir = '/tmp'
    zip_path = os.path.join(temp_dir, zip_filename)

    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for root, dirs, files in os.walk(WIKI_DIR):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, WIKI_DIR)
                zipf.write(file_path, arcname)

    return send_from_directory(temp_dir, zip_filename, as_attachment=True)

@app.route('/api/query', methods=['POST'])
def query_wiki():
    """查询Wiki知识库"""
    try:
        data = request.json
        query = data.get('query', '')

        if not query:
            return jsonify({'success': False, 'error': '问题不能为空'})

        config = get_config() or {}
        llm = get_llm()

        index_path = os.path.join(WIKI_DIR, 'index.md')
        index_content = ''
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                index_content = f.read()

        summaries_content = ''
        summaries_dir = os.path.join(WIKI_DIR, 'summaries')
        if os.path.exists(summaries_dir):
            for f in os.listdir(summaries_dir)[:5]:
                if f.endswith('.md'):
                    with open(os.path.join(summaries_dir, f), 'r', encoding='utf-8') as fp:
                        summaries_content += fp.read() + '\n\n'

        system_prompt = f"""你是一个知识库助手，基于提供的上下文回答用户问题。
如果没有相关信息，请明确说明。

上下文：
{index_content[:2000]}
{summaries_content[:3000]}
"""

        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': query}
        ]

        answer = llm.chat(messages)

        return jsonify({
            'success': True,
            'answer': answer,
            'query': query
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/query/archive', methods=['POST'])
def archive_query():
    """归档问答"""
    try:
        data = request.json
        query = data.get('query', '')
        answer = data.get('answer', '')

        if not query or not answer:
            return jsonify({'success': False, 'error': '问题和答案不能为空'})

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_query = re.sub(r'[^\w\-]', '_', query[:30])
        filename = f"q-{timestamp}-{safe_query}.md"

        archive_path = os.path.join(WIKI_DIR, 'archives', filename)

        os.makedirs(os.path.join(WIKI_DIR, 'archives'), exist_ok=True)

        content = f"""---
query: {query}
answer: {answer}
archived_at: {datetime.now().isoformat()}
---

# {query}

## 回答

{answer}
"""
        with open(archive_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/chat/sync', methods=['POST'])
def sync_chat_history():
    """同步对话历史到服务器"""
    try:
        data = request.json
        chats = data.get('chats', {})

        # 保存到服务器端文件
        sync_file = os.path.join(BASE_DIR, 'data', 'chat_history_sync.json')
        os.makedirs(os.path.join(BASE_DIR, 'data'), exist_ok=True)

        with open(sync_file, 'w', encoding='utf-8') as f:
            json.dump({
                'chats': chats,
                'synced_at': datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)

        return jsonify({'success': True, 'message': '同步成功'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)