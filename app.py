"""
LLM Wiki - Flask 主应用
"""

import os
import json
import re
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
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(BASE_DIR, 'raw')
WIKI_DIR = os.path.join(BASE_DIR, 'wiki')
CONFIG_DIR = os.path.join(BASE_DIR, 'config')

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

    # 页面类型映射
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
    # 安全检查：只允许删除特定目录下的文件
    allowed_categories = ['summaries', 'concepts', 'entities', 'projects', 'insights', 'topics', 'archives']

    if category not in allowed_categories:
        return jsonify({'success': False, 'error': '不允许删除此类型的页面'})

    page_path = os.path.join(WIKI_DIR, category, filename)

    # 安全检查：确保文件路径在WIKI_DIR内
    if not os.path.abspath(page_path).startswith(os.path.abspath(WIKI_DIR)):
        return jsonify({'success': False, 'error': '非法路径'})

    if os.path.exists(page_path):
        try:
            os.remove(page_path)
            # 更新索引
            compiler = get_compiler()
            compiler._update_index()
            return jsonify({'success': True, 'message': '页面已删除'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return jsonify({'success': False, 'error': '页面不存在'})

@app.route('/api/raw/delete/<filename>', methods=['DELETE'])
def delete_raw_file(filename):
    """删除原始素材及其关联的Wiki页面"""
    file_path = os.path.join(RAW_DIR, filename)

    # 安全检查
    if not os.path.abspath(file_path).startswith(os.path.abspath(RAW_DIR)):
        return jsonify({'success': False, 'error': '非法路径'})

    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': '文件不存在'})

    try:
        # 读取 raw 文件获取标题
        raw_title = filename.replace('.md', '').replace('_', ' ')
        raw_title_clean = re.sub(r'\d{4}-\d{2}-\d{2}_\d{6}_', '', raw_title)  # 去掉时间戳
        raw_content = ''
        raw_source = raw_title_clean  # 用于 source 匹配

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                raw_content = f.read()
            # 从 frontmatter 提取标题
            if raw_content.startswith('---'):
                parts = raw_content.split('---', 2)
                if len(parts) >= 2:
                    for line in parts[1].split('\n'):
                        if ':' in line and 'title' in line.lower():
                            raw_title_clean = line.split(':', 1)[1].strip()
                            break
                        if ':' in line and 'source' in line.lower():
                            raw_source = line.split(':', 1)[1].strip()
                            break
        except UnicodeDecodeError:
            # 二进制文件（如 PDF），使用文件名作为标题
            raw_content = ''

        print(f"删除 raw: {filename}, title: {raw_title_clean}")

        # 收集所有要删除的页面
        related_pages = []

        # 第一步：删除 summaries 目录中引用此 raw 的页面
        summaries_dir = os.path.join(WIKI_DIR, 'summaries')
        if os.path.exists(summaries_dir):
            for page_file in os.listdir(summaries_dir):
                if not page_file.endswith('.md'):
                    continue
                page_path = os.path.join(summaries_dir, page_file)
                with open(page_path, 'r', encoding='utf-8') as f:
                    page_content = f.read()
                # 检查 frontmatter 中的 raw_file 字段
                if page_content.startswith('---'):
                    parts = page_content.split('---', 2)
                    if len(parts) >= 2:
                        for line in parts[1].split('\n'):
                            if ':' in line and 'raw_file' in line:
                                value = line.split(':', 1)[1].strip()
                                if value == filename:
                                    related_pages.append({
                                        'category': 'summaries',
                                        'filename': page_file,
                                        'path': page_path
                                    })
                                    print(f"找到关联 summary: {page_file}")
                                    break

        # 第二步：遍历所有 wiki 目录，查找引用此 raw 标题的页面
        for category in ['concepts', 'entities', 'projects', 'insights', 'topics']:
            category_dir = os.path.join(WIKI_DIR, category)
            if os.path.exists(category_dir):
                for page_file in os.listdir(category_dir):
                    if not page_file.endswith('.md'):
                        continue
                    page_path = os.path.join(category_dir, page_file)
                    with open(page_path, 'r', encoding='utf-8') as f:
                        page_content = f.read()

                    # 检查是否包含 wiki 链接到某个 summary
                    wiki_links = re.findall(r'\[\[([^\]]+)\]\]', page_content)

                    # 同时检查 source 字段是否匹配
                    page_source = ''
                    if page_content.startswith('---'):
                        parts = page_content.split('---', 2)
                        if len(parts) >= 2:
                            for line in parts[1].split('\n'):
                                if ':' in line and 'source' in line.lower():
                                    page_source = line.split(':', 1)[1].strip().lower()
                                    break

                    is_related = False

                    # 检查 wiki 链接
                    for link in wiki_links:
                        link_clean = link.replace('.md', '').lower()
                        # 去掉 summaries/ 前缀
                        if '/' in link_clean:
                            link_clean = link_clean.split('/')[-1]
                        # 检查是否匹配 raw_title_clean（去掉时间戳）
                        title_lower = raw_title_clean.lower()
                        if title_lower in link_clean or link_clean in title_lower:
                            is_related = True
                            print(f"找到关联页面 (wiki link): {category}/{page_file}, link: {link}")
                            break

                    # 检查 source 是否匹配
                    if not is_related and page_source:
                        title_lower = raw_title_clean.lower()
                        if title_lower in page_source or page_source in title_lower:
                            is_related = True
                            print(f"找到关联页面 (source): {category}/{page_file}, source: {page_source}")

                    if is_related:
                        related_pages.append({
                            'category': category,
                            'filename': page_file,
                            'path': page_path
                        })

        # 第三步：删除所有关联的 Wiki 页面
        deleted_pages = []
        for page in related_pages:
            try:
                if os.path.exists(page['path']):
                    os.remove(page['path'])
                    deleted_pages.append(f"{page['category']}/{page['filename']}")
                    print(f"已删除: {page['path']}")
            except Exception as e:
                print(f"删除页面失败: {page['path']}, error: {e}")

        # 第四步：删除原始素材
        os.remove(file_path)
        print(f"已删除 raw: {file_path}")

        # 第五步：更新索引
        compiler = get_compiler()
        compiler._update_index()

        return jsonify({
            'success': True,
            'message': f'已删除素材 {filename} 及其关联的 {len(deleted_pages)} 个Wiki页面',
            'deleted_raw': filename,
            'deleted_pages': deleted_pages
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/wiki/cleanup-orphans', methods=['POST'])
def cleanup_orphan_pages():
    """清理孤立的Wiki页面（引用了不存在的summary的页面）"""
    try:
        deleted = []

        # 获取所有存在的 summary 文件名（去掉时间戳）
        summaries_dir = os.path.join(WIKI_DIR, 'summaries')
        existing_summaries = set()
        if os.path.exists(summaries_dir):
            for f in os.listdir(summaries_dir):
                if f.endswith('.md'):
                    # 去掉时间戳和 .md
                    clean_name = re.sub(r'\d{4}-\d{2}-\d{2}_\d{6}_', '', f).replace('.md', '')
                    existing_summaries.add(clean_name)
                    existing_summaries.add(f)
                    existing_summaries.add(f.replace('.md', ''))

        print(f"存在的 summaries: {existing_summaries}")

        # 遍历所有 wiki 目录，查找引用不存在 summary 的页面
        for category in ['concepts', 'entities', 'projects', 'insights', 'topics']:
            category_dir = os.path.join(WIKI_DIR, category)
            if not os.path.exists(category_dir):
                continue

            for page_file in os.listdir(category_dir):
                if not page_file.endswith('.md'):
                    continue
                page_path = os.path.join(category_dir, page_file)

                try:
                    with open(page_path, 'r', encoding='utf-8') as f:
                        page_content = f.read()
                except:
                    continue

                # 提取 wiki 链接
                wiki_links = re.findall(r'\[\[([^\]]+)\]\]', page_content)

                is_orphan = False
                for link in wiki_links:
                    link_clean = link.replace('.md', '').lower()
                    # 提取 summary 后的名称
                    if '/' in link_clean:
                        link_name = link_clean.split('/')[-1]
                    else:
                        link_name = link_clean

                    # 检查这个 summary 是否存在
                    summary_exists = False
                    for summary in existing_summaries:
                        summary_lower = summary.lower()
                        if link_name in summary_lower or summary_lower in link_name:
                            summary_exists = True
                            break

                    if not summary_exists:
                        is_orphan = True
                        print(f"孤立页面: {category}/{page_file}, 引用的 summary: {link}")
                        break

                if is_orphan:
                    try:
                        os.remove(page_path)
                        deleted.append(f"{category}/{page_file}")
                        print(f"已删除孤立页面: {page_path}")
                    except Exception as e:
                        print(f"删除失败: {page_path}, error: {e}")

        # 更新索引
        compiler = get_compiler()
        compiler._update_index()

        return jsonify({
            'success': True,
            'message': f'已清理 {len(deleted)} 个孤立页面',
            'deleted': deleted
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/raw/list')
def list_raw_files():
    """列出原始素材（带元数据）"""
    files = []
    if os.path.exists(RAW_DIR):
        for f in os.listdir(RAW_DIR):
            if f.endswith('.md'):
                file_path = os.path.join(RAW_DIR, f)
                # 解析 frontmatter
                metadata = {'title': f, 'source': None, 'imported_at': None}
                try:
                    with open(file_path, 'r', encoding='utf-8') as file:
                        content = file.read()
                        if content.startswith('---'):
                            parts = content.split('---', 2)
                            if len(parts) >= 3:
                                for line in parts[1].strip().split('\n'):
                                    if ':' in line:
                                        key, val = line.split(':', 1)
                                        metadata[key.strip()] = val.strip()
                except:
                    pass

                files.append({
                    'name': f,
                    'title': metadata.get('title', f.replace('.md', '')),
                    'source': metadata.get('source'),
                    'imported_at': metadata.get('imported_at'),
                    'size': os.path.getsize(file_path),
                    'type': 'url' if metadata.get('source') else 'file'
                })
    return jsonify({'success': True, 'files': files})

@app.route('/api/raw/view/<filename>')
def view_raw_file(filename):
    """查看原始素材内容"""
    file_path = os.path.join(RAW_DIR, filename)

    # 安全检查
    if not os.path.abspath(file_path).startswith(os.path.abspath(RAW_DIR)):
        return jsonify({'success': False, 'error': '非法路径'})

    if os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'success': True, 'content': content, 'filename': filename})
    return jsonify({'success': False, 'error': '文件不存在'})

@app.route('/api/config/get')
def get_config():
    """获取配置"""
    config_path = os.path.join(CONFIG_DIR, 'settings.json')
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        # 不返回敏感信息
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
def save_config():
    """保存配置"""
    config_path = os.path.join(CONFIG_DIR, 'settings.json')
    try:
        # 读取现有配置
        existing = {}
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                existing = json.load(f)

        # 更新配置
        new_config = request.json

        # 更新模型配置
        if 'model' in new_config:
            if 'model' not in existing:
                existing['model'] = {}
            for sub_key in ['provider', 'name', 'base_url', 'api_key']:
                if sub_key in new_config['model']:
                    val = new_config['model'][sub_key]
                    if sub_key == 'api_key' and (not val or val == '******'):
                        val = existing['model'].get('api_key', '')
                    existing['model'][sub_key] = val

        # 更新ASR配置
        if 'asr' in new_config:
            if 'asr' not in existing:
                existing['asr'] = {}
            for sub_key in ['appkey', 'access_key_id', 'access_key_secret', 'token']:
                if sub_key in new_config['asr']:
                    val = new_config['asr'][sub_key]
                    if sub_key == 'access_key_secret' and (not val or val == '******'):
                        val = existing['asr'].get('access_key_secret', '')
                    existing['asr'][sub_key] = val

        # 更新编译配置
        if 'compile' in new_config:
            existing['compile'] = new_config['compile']

        # 更新Lint配置
        if 'lint' in new_config:
            existing['lint'] = new_config['lint']

        # 更新飞书配置
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

# ============ 导入API ============

@app.route('/api/import/file', methods=['POST'])
def import_file():
    """导入本地文件（支持多种格式）"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': '没有上传文件'})

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': '没有选择文件'})

        # 加载配置
        config_path = os.path.join(CONFIG_DIR, 'settings.json')
        config = {}
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

        importer = get_importer(RAW_DIR, config)
        result = importer.import_file(file.read(), file.filename)

        # 自动触发编译，生成知识点
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

@app.route('/api/import/url', methods=['POST'])
def import_url():
    """智能抓取链接内容（支持网页、视频、公众号、播客音频）"""
    try:
        data = request.json
        url = data.get('url')

        if not url:
            return jsonify({'success': False, 'error': 'URL不能为空'})

        # 小宇宙播客单集 - 下载音频转写
        if 'xiaoyuzhoufm.com/episode/' in url:
            try:
                import requests
                headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
                resp = requests.get(url, headers=headers, timeout=10)
                # 提取音频URL
                audio_url_match = re.search(r'https://media\.xyzcdn\.net/[^"]+\.m4a', resp.text)
                if audio_url_match:
                    audio_url = audio_url_match.group(0)
                    # 提取标题
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

        # 使用智能抓取器
        fetcher = get_smart_fetcher()
        result = fetcher.fetch(url)

        if result['success']:
            # 加载配置
            config_path = os.path.join(CONFIG_DIR, 'settings.json')
            config = {}
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            importer = get_importer(RAW_DIR, config)
            import_result = importer.import_text(
                result['content'],
                result['title'],
                result['source']
            )

            # 自动触发编译，生成知识点
            try:
                compiler = get_compiler()
                compile_result = compiler.compile_all()
                import_result['compile'] = compile_result
            except Exception as e:
                import_result['compile_error'] = str(e)

            # 添加抓取类型信息
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
            # 加载配置
            config_path = os.path.join(CONFIG_DIR, 'settings.json')
            config = {}
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            importer = get_importer(RAW_DIR, config)
            import_result = importer.import_text(
                result['content'],
                result['title'],
                result['source']
            )

            # 自动触发编译，生成知识点
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

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/import/podcast/episode', methods=['POST'])
def import_podcast_episode():
    """导入播客单集（下载音频并转写）"""
    try:
        data = request.json
        audio_url = data.get('audio_url')
        episode_title = data.get('title', '未知标题')
        podcast_title = data.get('podcast_title', '播客')

        if not audio_url:
            return jsonify({'success': False, 'error': '音频URL不能为空'})

        # 使用 PodcastFetcher 下载音频
        fetcher = PodcastFetcher()
        download_result = fetcher.download_episode(audio_url, episode_title, podcast_title)

        if not download_result.get('success'):
            return jsonify(download_result)

        audio_path = download_result['audio_path']

        # 加载配置
        config_path = os.path.join(CONFIG_DIR, 'settings.json')
        config = {}
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

        importer = get_importer(RAW_DIR, config)

        # 使用 Whisper 进行转写
        transcript = importer._speech_to_text_whisper(audio_path)

        # 清理临时音频文件
        try:
            parent_dir = os.path.dirname(audio_path)
            os.unlink(audio_path)
            os.rmdir(parent_dir)
        except:
            pass

        if transcript.startswith('[') and transcript.endswith(']') and '错误' in transcript:
            return jsonify({'success': False, 'error': transcript})

        # 导入为素材
        import_result = importer.import_text(
            transcript,
            title=f"{podcast_title} - {episode_title}",
            source=f'播客转写: {podcast_title}'
        )

        # 自动触发编译
        if import_result.get('success'):
            try:
                compiler = get_compiler()
                compile_result = compiler.compile_all()
                import_result['compile'] = compile_result
            except Exception as e:
                import_result['compile_error'] = str(e)

        return jsonify(import_result)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/import/podcast/rss', methods=['POST'])
def import_podcast_rss():
    """获取播客RSS信息（不下载）"""
    try:
        data = request.json
        rss_url = data.get('url')

        if not rss_url:
            return jsonify({'success': False, 'error': 'RSS地址不能为空'})

        fetcher = PodcastFetcher()
        result = fetcher.fetch_rss(rss_url)

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ============ 编译API ============

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
            'errors': result['errors']
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ============ 查询API ============

@app.route('/api/query', methods=['POST'])
def query_wiki():
    """问答查询（支持对话历史）"""
    try:
        data = request.json
        query = data.get('query', '')
        history = data.get('history', [])  # 对话历史

        if not query:
            return jsonify({'success': False, 'error': '问题不能为空'})

        # 读取Wiki索引了解知识库结构
        index_path = os.path.join(WIKI_DIR, 'index.md')
        index_content = ''
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                index_content = f.read()

        # 读取相关摘要
        summaries_content = ''
        summaries_dir = os.path.join(WIKI_DIR, 'summaries')
        if os.path.exists(summaries_dir):
            for f in os.listdir(summaries_dir)[:5]:  # 最多读取5篇
                if f.endswith('.md'):
                    path = os.path.join(summaries_dir, f)
                    with open(path, 'r', encoding='utf-8') as file:
                        summaries_content += file.read() + '\n\n---\n\n'

        # 构建上下文
        context = f"""# 知识库内容

## 索引
{index_content}

## 摘要内容
{summaries_content[:8000]}
"""

        # 系统提示词
        system_prompt = """请基于当前知识库（优先使用已有Wiki页面，而非原始资料）回答我的问题，并严格遵循以下规则：

1. 先从 index.md 中定位相关页面
2. 只读取必要页面，不要全量扫描
3. 综合多个页面进行分析，而不是逐条复述
4. 输出结构化答案（分点说明，而非长段文字）
5. 给出明确结论 + 你的推理过程

在回答结束后，请执行：
6. 判断本次回答是否具备"长期价值"
7. 如果有价值，请将其整理为一条"Insight（洞察页）"，包含：
   * 问题背景
   * 分析逻辑
   * 核心结论
   * 适用场景
   * 关联页面

以下是你的知识库内容：

{context}"""

        # 构建消息列表
        messages = [{'role': 'system', 'content': system_prompt}]

        # 添加历史消息（最近10条）
        for msg in history[-10:]:
            if msg['role'] in ['user', 'assistant']:
                messages.append({'role': msg['role'], 'content': msg['content']})

        # 添加当前问题
        messages.append({'role': 'user', 'content': query})

        # 调用LLM回答
        llm = get_llm()
        answer = llm.chat(messages)

        return jsonify({'success': True, 'answer': answer})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/query/archive', methods=['POST'])
def archive_query():
    """存档问答结果"""
    try:
        data = request.json
        query = data.get('query', '')
        answer = data.get('answer', '')

        if not query or not answer:
            return jsonify({'success': False, 'error': '内容和答案不能为空'})

        # 保存到archives目录
        timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        safe_query = re.sub(r'[^\w\-]', '_', query[:30])
        filename = f"q-{timestamp}-{safe_query}.md"

        archive_path = os.path.join(WIKI_DIR, 'archives', filename)

        content = f"""---
query: {query}
created_at: {datetime.now().isoformat()}
---

# 问答存档

## 问题
{query}

## 回答
{answer}
"""

        with open(archive_path, 'w', encoding='utf-8') as f:
            f.write(content)

        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ============ 导出API ============

@app.route('/api/export/markdown')
def export_markdown():
    """导出Markdown包"""
    import zipfile
    import tempfile

    # 创建临时zip文件
    temp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(temp_dir, 'wiki_export.zip')

    with zipfile.ZipFile(zip_path, 'w') as zipf:
        # 添加wiki目录
        for root, dirs, files in os.walk(WIKI_DIR):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, WIKI_DIR)
                zipf.write(file_path, arcname)

    return send_from_directory(temp_dir, 'wiki_export.zip', as_attachment=True)

if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5001
    app.run(debug=True, port=port)