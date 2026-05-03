"""
Wiki编译器
将原始素材编译成结构化的Wiki知识库
遵循个人知识库Schema规则
"""

import os
import re
import json
from datetime import datetime
from pathlib import Path

from llm_adapter import get_llm


class Compiler:
    """Wiki编译器"""

    # 页面类型目录映射
    PAGE_TYPES = {
        'concepts': 'concepts',
        'entities': 'entities',
        'projects': 'projects',
        'insights': 'insights',
        'topics': 'topics',
        'summaries': 'summaries'
    }

    def __init__(self, raw_dir: str, wiki_dir: str, config_dir: str):
        self.raw_dir = raw_dir
        self.wiki_dir = wiki_dir
        self.config_dir = config_dir

        # 确保所有目录存在
        for dir_name in self.PAGE_TYPES.values():
            os.makedirs(os.path.join(wiki_dir, dir_name), exist_ok=True)

        # 读取schema作为系统提示
        self.schema = self._load_schema()
        self.llm = get_llm()

    def _load_schema(self) -> str:
        """加载schema文件"""
        schema_path = os.path.join(self.config_dir, 'schema.md')
        if os.path.exists(schema_path):
            with open(schema_path, 'r', encoding='utf-8') as f:
                return f.read()
        return ""

    def compile_all(self) -> dict:
        """编译所有未处理的素材"""
        results = {
            'processed': 0,
            'pages_created': 0,
            'errors': []
        }

        raw_files = self._get_raw_files()

        for raw_file in raw_files:
            try:
                result = self.compile_one(raw_file)
                if result['success']:
                    results['processed'] += 1
                    results['pages_created'] += result.get('pages_created', 0)
                else:
                    results['errors'].append(f"{raw_file}: {result.get('error', '未知错误')}")
            except Exception as e:
                results['errors'].append(f"{raw_file}: {str(e)}")

        # 更新索引
        self._update_index()

        return results

    def compile_one(self, raw_file: str) -> dict:
        """编译单个素材"""
        raw_path = os.path.join(self.raw_dir, raw_file)

        if self._is_already_compiled(raw_file):
            return {'success': True, 'message': '已编译过', 'pages_created': 0}

        content = self._read_raw_file(raw_path)
        if not content:
            return {'success': False, 'error': '无法读取文件内容'}

        metadata = self._extract_metadata(content)
        clean_content = self._clean_content(content)

        # 使用LLM按照schema处理内容
        analysis = self._analyze_content(clean_content, metadata)

        pages_created = 0

        # 创建摘要页
        if analysis.get('summary'):
            self._create_summary_page(analysis['summary'], metadata, raw_file)
            pages_created += 1

        # 创建概念页
        for concept in analysis.get('concepts', []):
            self._create_concept_page(concept, metadata, raw_file)
            pages_created += 1

        # 创建实体页
        for entity in analysis.get('entities', []):
            self._create_entity_page(entity, metadata, raw_file)
            pages_created += 1

        # 创建项目/案例页
        for project in analysis.get('projects', []):
            self._create_project_page(project, metadata, raw_file)
            pages_created += 1

        # 创建洞察页
        for insight in analysis.get('insights', []):
            self._create_insight_page(insight, metadata, raw_file)
            pages_created += 1

        # 创建主题页
        for topic in analysis.get('topics', []):
            self._create_topic_page(topic, metadata, raw_file)
            pages_created += 1

        # 记录编译日志
        self._log_compilation(raw_file, pages_created, analysis)

        return {
            'success': True,
            'pages_created': pages_created,
            'analysis': analysis
        }

    def _get_raw_files(self) -> list:
        """获取raw目录中的文件列表"""
        files = []
        if os.path.exists(self.raw_dir):
            for f in os.listdir(self.raw_dir):
                if f.endswith('.md') and not f.startswith('.'):
                    files.append(f)
        return sorted(files)

    def _is_already_compiled(self, raw_file: str) -> bool:
        """检查是否已编译"""
        log_path = os.path.join(self.wiki_dir, 'log.md')
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8') as f:
                if raw_file in f.read():
                    return True
        return False

    def _read_raw_file(self, path: str) -> str:
        """读取原始文件内容"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except:
            return ''

    def _clean_content(self, content: str) -> str:
        """清理内容，去除frontmatter"""
        if content.startswith('---'):
            parts = content.split('---')
            if len(parts) >= 3:
                return parts[2].strip()
        return content

    def _extract_metadata(self, content: str) -> dict:
        """提取Markdown文件的元数据"""
        metadata = {
            'title': '未知标题',
            'source': '未知来源',
            'imported_at': datetime.now().isoformat()
        }

        if content.startswith('---'):
            parts = content.split('---')
            if len(parts) >= 3:
                frontmatter = parts[1]
                for line in frontmatter.strip().split('\n'):
                    if ':' in line:
                        key, value = line.split(':', 1)
                        metadata[key.strip()] = value.strip()

        return metadata

    def _analyze_content(self, content: str, metadata: dict) -> dict:
        """使用LLM分析内容，按照schema分类提取知识"""
        max_length = 10000
        if len(content) > max_length:
            content = content[:max_length] + "\n...(内容过长已截断)"

        prompt = f"""请分析以下内容，按照知识库Schema提取结构化知识。

内容标题：{metadata.get('title', '未知')}
内容来源：{metadata.get('source', '未知')}

内容：
{content}

请以JSON格式输出以下内容：

1. summary: 一段200-300字的核心摘要
2. concepts: 识别出的概念列表，每个包含 name(名称) 和 definition(定义)
3. entities: 识别出的实体（人/公司/产品），每个包含 name(名称) 和 description(简述)
4. projects: 识别出的项目/案例，每个包含 name(名称) 和 description(简述)
5. insights: 提炼出的洞察，每个包含 title(标题) 和 content(内容)
6. topics: 相关主题，每个包含 name(名称) 和 description(简述)

注意：
- 洞察是最重要的，要从内容中提炼可复用的知识
- 只提取有价值的信息，避免冗余
- 如果某类信息不存在，返回空数组

输出格式示例：
{{
  "summary": "核心摘要内容...",
  "concepts": [
    {{"name": "概念名", "definition": "定义..."}}
  ],
  "entities": [
    {{"name": "实体名", "description": "描述..."}}
  ],
  "projects": [
    {{"name": "项目名", "description": "描述..."}}
  ],
  "insights": [
    {{"title": "洞察标题", "content": "洞察内容..."}}
  ],
  "topics": [
    {{"name": "主题名", "description": "描述..."}}
  ]
}}
"""

        try:
            response = self.llm.chat([
                {'role': 'system', 'content': '你是一个知识架构师，擅长从内容中提取结构化知识。严格按照JSON格式输出。'},
                {'role': 'user', 'content': prompt}
            ])

            # 解析JSON响应
            # 尝试提取JSON部分
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                return json.loads(json_match.group())
            else:
                return {'summary': response, 'concepts': [], 'entities': [], 'projects': [], 'insights': [], 'topics': []}

        except Exception as e:
            print(f"分析内容失败: {e}")
            return {'summary': '', 'concepts': [], 'entities': [], 'projects': [], 'insights': [], 'topics': []}

    def _create_summary_page(self, summary: str, metadata: dict, raw_file: str):
        """创建摘要页"""
        safe_title = re.sub(r'[^\w\-]', '_', metadata.get('title', 'article'))[:50]
        timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        filename = f"{timestamp}_{safe_title}.md"

        content = f"""---
title: {metadata.get('title', '未知标题')}
source: {metadata.get('source', '未知来源')}
raw_file: {raw_file}
created_at: {datetime.now().isoformat()}
---

# {metadata.get('title', '未知标题')}

{summary}

## 来源
- 原始文件: {raw_file}
"""
        self._save_page('summaries', filename, content)

    def _create_concept_page(self, concept: dict, metadata: dict, raw_file: str):
        """创建概念页"""
        name = concept.get('name', '未命名概念')
        safe_name = re.sub(r'[^\w\-]', '_', name.lower())[:50]
        filename = f"{safe_name}.md"

        # 检查是否已存在
        existing = self._load_page('concepts', filename)

        if existing:
            # 追加来源
            content = existing + f"\n\n- 来源: [[summaries/{metadata.get('title', 'article')}]]"
        else:
            content = f"""---
name: {name}
created_at: {datetime.now().isoformat()}
---

# {name}

## 定义
{concept.get('definition', '待补充')}

## 核心要素
（待补充）

## 示例
（待补充）

## 相关概念
（自动生成）

## 来源
- [[summaries/{metadata.get('title', 'article')}]]
"""
        self._save_page('concepts', filename, content)

    def _create_entity_page(self, entity: dict, metadata: dict, raw_file: str):
        """创建实体页"""
        name = entity.get('name', '未命名实体')
        safe_name = re.sub(r'[^\w\-]', '_', name.lower())[:50]
        filename = f"{safe_name}.md"

        existing = self._load_page('entities', filename)

        if not existing:
            content = f"""---
name: {name}
created_at: {datetime.now().isoformat()}
---

# {name}

## 概述
{entity.get('description', '待补充')}

## 关键事实
（待补充）

## 产品/行为
（待补充）

## 优势/劣势
（待补充）

## 相关实体
（自动生成）

## 相关概念
（自动生成）

## 来源
- [[summaries/{metadata.get('title', 'article')}]]
"""
            self._save_page('entities', filename, content)

    def _create_project_page(self, project: dict, metadata: dict, raw_file: str):
        """创建项目/案例页"""
        name = project.get('name', '未命名项目')
        safe_name = re.sub(r'[^\w\-]', '_', name.lower())[:50]
        filename = f"{safe_name}.md"

        existing = self._load_page('projects', filename)

        if not existing:
            content = f"""---
name: {name}
created_at: {datetime.now().isoformat()}
---

# {name}

## 简要说明
{project.get('description', '待补充')}

## 背景
（待补充）

## 发生了什么
（待补充）

## 关键决策
（待补充）

## 结果
（待补充）

## 经验教训
（待补充）

## 相关实体/概念
（自动生成）

## 来源
- [[summaries/{metadata.get('title', 'article')}]]
"""
            self._save_page('projects', filename, content)

    def _create_insight_page(self, insight: dict, metadata: dict, raw_file: str):
        """创建洞察页"""
        title = insight.get('title', '未命名洞察')
        safe_title = re.sub(r'[^\w\-]', '_', title.lower())[:50]
        timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        filename = f"{timestamp}_{safe_title}.md"

        content = f"""---
title: {title}
created_at: {datetime.now().isoformat()}
source: {metadata.get('title', '未知')}
---

# {title}

## 问题/背景
（从原文提取）

## 分析过程
（待补充）

## 核心结论
{insight.get('content', '待补充')}

## 适用场景
（待补充）

## 关联概念/案例
（自动生成）

## 来源
- [[summaries/{metadata.get('title', 'article')}]]
"""
        self._save_page('insights', filename, content)

    def _create_topic_page(self, topic: dict, metadata: dict, raw_file: str):
        """创建主题页"""
        name = topic.get('name', '未命名主题')
        safe_name = re.sub(r'[^\w\-]', '_', name.lower())[:50]
        filename = f"{safe_name}.md"

        existing = self._load_page('topics', filename)

        if existing:
            # 更新现有主题页
            content = existing + f"\n\n- 相关内容: [[summaries/{metadata.get('title', 'article')}]]"
        else:
            content = f"""---
name: {name}
created_at: {datetime.now().isoformat()}
---

# {name}

## 概述
{topic.get('description', '待补充')}

## 关键概念
（自动生成）

## 关键实体
（自动生成）

## 主要趋势
（待补充）

## 未解决问题
（待补充）

## 相关内容
- [[summaries/{metadata.get('title', 'article')}]]
"""
        self._save_page('topics', filename, content)

    def _save_page(self, page_type: str, filename: str, content: str):
        """保存页面"""
        dir_path = os.path.join(self.wiki_dir, page_type)
        os.makedirs(dir_path, exist_ok=True)
        filepath = os.path.join(dir_path, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

    def _load_page(self, page_type: str, filename: str) -> str:
        """加载页面"""
        filepath = os.path.join(self.wiki_dir, page_type, filename)
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        return ''

    def _update_index(self):
        """更新知识库索引"""
        index_path = os.path.join(self.wiki_dir, 'index.md')

        # 扫描所有页面类型
        pages = {}
        for page_type in self.PAGE_TYPES.values():
            dir_path = os.path.join(self.wiki_dir, page_type)
            pages[page_type] = []
            if os.path.exists(dir_path):
                for f in sorted(os.listdir(dir_path)):
                    if f.endswith('.md'):
                        pages[page_type].append(f.replace('.md', ''))

        # 生成索引
        index_content = f"""# 知识库索引

> 更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---

"""

        # 按类型组织
        type_names = {
            'concepts': '概念',
            'entities': '实体',
            'projects': '项目/案例',
            'insights': '洞察',
            'topics': '主题',
            'summaries': '摘要'
        }

        for page_type, type_name in type_names.items():
            count = len(pages.get(page_type, []))
            index_content += f"## {type_name} ({count})\n\n"

            for page in pages.get(page_type, []):
                index_content += f"- [[{page_type}/{page}]]\n"

            index_content += "\n"

        index_content += """---

> 此索引由系统自动维护，请勿手动编辑。
"""

        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(index_content)

    def _log_compilation(self, raw_file: str, pages_created: int, analysis: dict):
        """记录编译日志"""
        log_path = os.path.join(self.wiki_dir, 'log.md')

        log_entry = f"""
## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

- 编译素材: `{raw_file}`
- 创建页面: {pages_created}个
  - 概念: {len(analysis.get('concepts', []))}个
  - 实体: {len(analysis.get('entities', []))}个
  - 项目: {len(analysis.get('projects', []))}个
  - 洞察: {len(analysis.get('insights', []))}个
  - 主题: {len(analysis.get('topics', []))}个
"""
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(log_entry)


def get_compiler() -> Compiler:
    """获取编译器实例"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return Compiler(
        os.path.join(base_dir, 'raw'),
        os.path.join(base_dir, 'wiki'),
        os.path.join(base_dir, 'config')
    )