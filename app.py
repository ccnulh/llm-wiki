"""
LLM Wiki - Flask 主应用
"""

import os
import json
import re
import uuid
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
RAW_DIR = os.path.join(BASE_DIR, 'raw')
WIKI_DIR = os.path.join(BASE_DIR, 'wiki')
CONFIG_DIR = os.path.join(BASE_DIR, 'config')

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

def get_config():
    """获取配置（优先从环境变量）"""
    config = {}

    # 阿里云 DashScope
    if os.getenv('DASHSCOPE_API_KEY'):
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
    """健康检查"""
    return jsonify({'status': 'ok', 'service': 'llm-wiki'})

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
        for f in os.listdir(RAW_DIR):
            if f.endswith('.md'):
                file_path = os.path.join(RAW_DIR, f)
                metadata = {'title': f, 'source': None, 'imported_at': None}
                try:
                    with open(file_path, 'r', encoding='utf-8') as fp:
                        content = fp.read(500)
                        if content:
                            import frontmatter
                            try:
                                parsed = frontmatter.loads(content)
                                metadata['title'] = parsed.get('title', f)
                                metadata['source'] = parsed.get('source', None)
                                metadata['imported_at'] = parsed.get('imported_at', None)
                            except:
                                pass
                except:
                    pass

                files.append({
                    'name': f,
                    'filename': f,
                    'size': os.path.getsize(file_path),
                    'imported_at': metadata['imported_at'],
                    'source': metadata['source'],
                    'title': metadata['title'],
                    'type': 'url'
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

@app.route('/api/raw/view/<filename>')
def view_raw_file(filename):
    """查看原始素材内容"""
    file_path = os.path.join(RAW_DIR, filename)

    if not os.path.abspath(file_path).startswith(os.path.abspath(RAW_DIR)):
        return jsonify({'success': False, 'error': '非法路径'})

    if os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'success': True, 'content': content, 'filename': filename})
    return jsonify({'success': False, 'error': '文件不存在'})

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

        # 如果有COS配置，上传文件到COS
        storage = get_cos_storage()
        if storage and file_size > 5 * 1024 * 1024:  # > 5MB
            # 保存到本地临时目录
            temp_dir = os.path.join(BASE_DIR, 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            local_path = os.path.join(temp_dir, f"{task_id}_{filename}")
            file.save(local_path)

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

        # 小文件直接处理
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

@app.route('/api/import/large-file/status/<task_id>')
def get_large_file_status(task_id):
    """获取大文件处理状态"""
    with processing_lock:
        if task_id in processing_tasks:
            return jsonify({'success': True, 'task': processing_tasks[task_id]})
        return jsonify({'success': False, 'error': '任务不存在'})

@app.route('/api/import/large-file/process/<task_id>', methods=['POST'])
def process_large_file(task_id):
    """触发大文件异步处理"""
    with processing_lock:
        if task_id not in processing_tasks:
            return jsonify({'success': False, 'error': '任务不存在'})

    storage = get_cos_storage()
    if not storage:
        return jsonify({'success': False, 'error': 'COS未配置'})

    task = None
    with processing_lock:
        task = processing_tasks[task_id]

    def process_file():
        try:
            with processing_lock:
                processing_tasks[task_id]['status'] = 'processing'
                processing_tasks[task_id]['progress'] = 10
                processing_tasks[task_id]['message'] = '下载文件...'

            # 下载文件到临时目录
            temp_dir = os.path.join(BASE_DIR, 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            local_path = os.path.join(temp_dir, task['filename'])

            with processing_lock:
                processing_tasks[task_id]['progress'] = 20
                processing_tasks[task_id]['message'] = '处理中...'

            # 下载COS文件
            cos_key = task['cos_key']
            storage.download_file(cos_key, local_path)

            with processing_lock:
                processing_tasks[task_id]['progress'] = 40
                processing_tasks[task_id]['message'] = '提取内容...'

            # 读取文件
            with open(local_path, 'rb') as f:
                file_content = f.read()

            # 删除临时文件
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

            # 清理COS文件
            try:
                storage.delete_file(cos_key)
            except:
                pass

            with processing_lock:
                processing_tasks[task_id]['progress'] = 80
                processing_tasks[task_id]['message'] = '生成知识页面...'

            if result.get('success'):
                try:
                    compiler = get_compiler()
                    compile_result = compiler.compile_all()
                    result['compile'] = compile_result
                except Exception as e:
                    result['compile_error'] = str(e)

            with processing_lock:
                processing_tasks[task_id]['status'] = 'completed'
                processing_tasks[task_id]['progress'] = 100
                processing_tasks[task_id]['message'] = '处理完成'
                processing_tasks[task_id]['result'] = {
                    'success': result.get('success', False),
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
                headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
                resp = requests.get(url, headers=headers, timeout=10)
                audio_url_match = re.search(r'https://media\.xyzcdn\.net/[^"]+\.m4a', resp.text)
                if audio_url_match:
                    audio_url = audio_url_match.group(0)
                    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', resp.text)
                    title = title_match.group(1) if title_match else '小宇宙播客'
                    return jsonify({
                        'success': True,
                        'audio_url': audio_url,
                        'title': title,
                        'source': url,
                        'type': 'podcast'
                    })
            except Exception as e:
                return jsonify({'success': False, 'error': f'小宇宙音频提取失败: {str(e)}'})

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
    """导入播客单集（下载音频并转写）"""
    try:
        data = request.json
        audio_url = data.get('audio_url')
        title = data.get('title', '播客单集')
        podcast_title = data.get('podcast_title', '')

        if not audio_url:
            return jsonify({'success': False, 'error': '音频URL不能为空'})

        # 下载音频
        import requests
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
        audio_resp = requests.get(audio_url, headers=headers, timeout=60)
        if audio_resp.status_code != 200:
            return jsonify({'success': False, 'error': '音频下载失败'})

        # 保存音频
        timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        audio_filename = f"{timestamp}_{podcast_title}_{title}.m4a"
        audio_path = os.path.join(RAW_DIR, audio_filename)
        with open(audio_path, 'wb') as f:
            f.write(audio_resp.content)

        # 转写
        config = get_config() or {}
        importer = get_importer(RAW_DIR, config)
        transcript = importer._speech_to_text_whisper(audio_path)

        # 删除音频文件
        os.remove(audio_path)

        if transcript:
            import_result = importer.import_text(
                transcript,
                title,
                f'播客: {podcast_title}'
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
            return jsonify({'success': False, 'error': '转写失败'})

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)