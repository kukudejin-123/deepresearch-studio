"""
app.tools 工具层测试

- generate_markdown / read_file_content：真实文件读写，验证会话目录隔离；
- db_tools：mock mysql.connector，验证工具输出格式与错误处理；
- vector_store_tools：用临时 FAISS 目录验证集合发现与检索提示。
"""

from pathlib import Path

import pytest

from app.tools.markdown_tools import generate_markdown
from app.tools.upload_file_read_tool import read_file_content


class TestMarkdownTool:
    """generate_markdown 工具验证"""

    def test_writes_markdown_into_session_dir(self, session_dir):
        """生成的 Markdown 必须落在当前会话目录"""
        result = generate_markdown.invoke({"content": "# 标题\n正文", "filename": "测试报告"})

        assert "已成功生成" in result
        assert (session_dir / "测试报告.md").read_text(encoding="utf-8") == "# 标题\n正文"

    def test_appends_md_suffix_only_once(self, session_dir):
        """已带 .md 后缀时不应重复追加"""
        generate_markdown.invoke({"content": "正文", "filename": "报告.md"})

        assert (session_dir / "报告.md").exists()
        assert not (session_dir / "报告.md.md").exists()

    def test_creates_subdirectory(self, session_dir):
        """模型指定子目录时应自动创建"""
        generate_markdown.invoke({"content": "正文", "filename": "报告.md", "path": "sub_dir"})

        assert (session_dir / "sub_dir" / "报告.md").exists()

    def test_relative_traversal_is_clamped(self, session_dir):
        """模型传入 '../' 相对路径穿越时，产物必须被收敛回会话目录"""
        result = generate_markdown.invoke({"content": "正文", "filename": "../逃逸文件.md"})

        assert "已成功生成" in result
        assert (session_dir / "逃逸文件.md").exists()
        assert not (session_dir.parent / "逃逸文件.md").exists()

    def test_absolute_path_outside_session_is_clamped(self, session_dir, tmp_path):
        """模型传入会话目录外的绝对路径时，同样收敛回会话目录"""
        outside = tmp_path / "outside.md"

        result = generate_markdown.invoke(
            {"content": "正文", "filename": "报告.md", "path": str(tmp_path)}
        )

        assert "已成功生成" in result
        assert (session_dir / "报告.md").exists()
        assert not outside.exists()


class TestUploadFileReadTool:
    """read_file_content 工具验证"""

    def test_reads_markdown(self, session_dir):
        """Markdown 文件直接返回全文"""
        (session_dir / "笔记.md").write_text("第一行\n第二行", encoding="utf-8")

        result = read_file_content.invoke({"filename": "笔记.md"})

        assert result == "第一行\n第二行"

    def test_missing_file_returns_error_message(self, session_dir):
        """文件不存在时返回中文错误提示，不抛异常"""
        result = read_file_content.invoke({"filename": "不存在.md"})

        assert "不存在" in result
        assert "错误" in result

    def test_reads_pdf(self, session_dir):
        """PDF 文件通过 pypdf 提取文本"""
        source = (
            Path(__file__).parent.parent / "examples" / "test_docs" / "sub_dir" / "测试文件.pdf"
        )
        if not source.exists():
            pytest.skip("示例 PDF 不存在")

        target = session_dir / source.name
        target.write_bytes(source.read_bytes())

        result = read_file_content.invoke({"filename": source.name})

        assert "错误" not in result[:10]
        assert len(result.strip()) > 0

    def test_unknown_suffix_falls_back_to_text(self, session_dir):
        """未知后缀先按 UTF-8 文本兜底读取"""
        (session_dir / "data.abc").write_text("纯文本内容", encoding="utf-8")

        result = read_file_content.invoke({"filename": "data.abc"})

        assert result == "纯文本内容"


class TestDbTools:
    """db_tools 工具验证（mock MySQL 连接）"""

    @pytest.fixture
    def mock_connect(self, monkeypatch):
        """伪造 mysql.connector.connect，返回假连接与假游标"""
        from app.tools import db_tools

        class FakeCursor:
            description = [("id",), ("name",)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql):
                self.executed_sql = sql

            def fetchall(self):
                return [("drugs",), ("inventory",)]

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def cursor(self):
                return FakeCursor()

        monkeypatch.setattr(db_tools, "connect", lambda **kwargs: FakeConn())
        return db_tools

    def test_list_sql_tables(self, mock_connect, monkeypatch):
        """表名列表工具返回逗号拼接的表名"""
        monkeypatch.setenv("MYSQL_USER", "root")
        monkeypatch.setenv("MYSQL_PASSWORD", "root")
        monkeypatch.setenv("MYSQL_DATABASE", "deepresearch_db")

        result = mock_connect.list_sql_tables.invoke({})

        assert "drugs" in result
        assert "inventory" in result

    def test_get_table_data_returns_csv(self, mock_connect, monkeypatch):
        """表数据预览返回 CSV 格式（首行列名）"""
        monkeypatch.setenv("MYSQL_USER", "root")
        monkeypatch.setenv("MYSQL_PASSWORD", "root")
        monkeypatch.setenv("MYSQL_DATABASE", "deepresearch_db")

        result = mock_connect.get_table_data.invoke({"table_name": "drugs"})

        assert result.splitlines()[0] == "id,name"

    def test_missing_required_config_raises(self, mock_connect, monkeypatch):
        """缺少 user/password/database 配置时应抛出明确错误"""
        monkeypatch.delenv("MYSQL_USER", raising=False)
        monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
        monkeypatch.delenv("MYSQL_DATABASE", raising=False)

        with pytest.raises(ValueError, match="缺失数据库核心配置"):
            mock_connect.get_db_config()


class TestVectorStoreTools:
    """vector_store_tools 工具验证（临时向量库目录）"""

    @pytest.fixture
    def kb_with_collection(self, tmp_path, monkeypatch):
        """构造一个包含假集合元数据的向量库目录，并替换工具内的知识库实例"""
        from app.rag.vector_store import VectorKnowledgeBase
        from app.tools import vector_store_tools

        store_root = tmp_path / "vector_store"
        collection_dir = store_root / "测试集合"
        collection_dir.mkdir(parents=True)
        import json

        (collection_dir / "meta.json").write_text(
            json.dumps(
                {
                    "name": "测试集合",
                    "chunk_count": 2,
                    "file_names": ["a.pdf", "b.md"],
                    "embedding_model": "text-embedding-v3-test",
                    "created_at": "2026-10-14 10:00:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(vector_store_tools, "knowledge_base", VectorKnowledgeBase(store_root))
        return vector_store_tools

    def test_list_collections(self, kb_with_collection):
        """集合列表包含名称、分片数和文件名"""
        result = kb_with_collection.list_knowledge_collections.invoke({})

        assert "测试集合" in result
        assert "a.pdf" in result
        assert "分片数量: 2" in result

    def test_search_unknown_collection_returns_error(self, kb_with_collection):
        """检索不存在的集合时返回中文错误提示"""
        result = kb_with_collection.search_knowledge_base.invoke(
            {"question": "测试", "collection": "不存在的集合"}
        )

        assert "检索失败" in result
        assert "不存在的集合" in result
