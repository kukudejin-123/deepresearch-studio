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
    """db_tools 工具验证（mock MySQL 连接池）"""

    @pytest.fixture
    def mock_connect(self, monkeypatch):
        """
        伪造 MySQL 连接池（只 mock 池构造这一层）

        保留真实的 get_connection_pool 缓存逻辑，让工具测试与池复用
        测试都能覆盖到真实代码路径；每次测试重置池单例避免串用。
        """
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

            def close(self):
                # 连接池语义：close = 归还连接
                return True

        created: list = []

        class FakeMySQLPool:
            def __init__(self, **kwargs):
                created.append(self)

            def get_connection(self):
                return FakeConn()

        monkeypatch.setattr(db_tools, "MySQLConnectionPool", FakeMySQLPool)
        monkeypatch.setattr(db_tools, "_connection_pool", None)
        monkeypatch.setattr(db_tools, "_pool_signature", None)
        # 池实例计数挂到模块上，供复用/重建断言使用
        db_tools._test_pool_instances = created
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

    def test_pool_reused_across_calls(self, mock_connect, monkeypatch):
        """同一配置下连接池实例复用（真实缓存逻辑，mock 池构造）"""
        monkeypatch.setenv("MYSQL_USER", "root")
        monkeypatch.setenv("MYSQL_PASSWORD", "root")
        monkeypatch.setenv("MYSQL_DATABASE", "deepresearch_db")

        pool_first = mock_connect.get_connection_pool()
        pool_second = mock_connect.get_connection_pool()

        assert pool_first is pool_second
        assert len(mock_connect._test_pool_instances) == 1

    def test_pool_rebuilt_on_config_change(self, mock_connect, monkeypatch):
        """配置变化时连接池重建（测试环境切换数据库）"""
        monkeypatch.setenv("MYSQL_USER", "root")
        monkeypatch.setenv("MYSQL_PASSWORD", "root")
        monkeypatch.setenv("MYSQL_DATABASE", "db_one")

        mock_connect.get_connection_pool()
        monkeypatch.setenv("MYSQL_DATABASE", "db_two")
        mock_connect.get_connection_pool()

        assert len(mock_connect._test_pool_instances) == 2

    def test_execute_sql_rejects_drop(self, mock_connect, monkeypatch):
        """工具层拦截 DROP：不连接数据库，直接返回安全策略提示"""
        monkeypatch.setenv("MYSQL_USER", "root")
        monkeypatch.setenv("MYSQL_PASSWORD", "root")
        monkeypatch.setenv("MYSQL_DATABASE", "deepresearch_db")

        result = mock_connect.execute_sql_query.invoke({"query": "DROP TABLE drugs"})

        assert "拒绝" in result
        assert "DROP" in result

    def test_get_table_data_rejects_injection(self, mock_connect, monkeypatch):
        """表名含注入字符时被白名单拦截"""
        monkeypatch.setenv("MYSQL_USER", "root")
        monkeypatch.setenv("MYSQL_PASSWORD", "root")
        monkeypatch.setenv("MYSQL_DATABASE", "deepresearch_db")

        result = mock_connect.get_table_data.invoke({"table_name": "drugs; DROP TABLE users"})

        assert "非法字符" in result


class TestKnowledgeBaseUserIsolation:
    """知识库按用户隔离验证"""

    @pytest.fixture
    def store_root(self, tmp_path, monkeypatch):
        """构造共享库 + 两个用户私有库"""
        import json

        from app.api.context import set_user_context
        from app.rag.vector_store import VectorKnowledgeBase
        from app.tools import vector_store_tools

        root = tmp_path / "vector_store"

        def make_collection(base_dir, name, file_name):
            # 集合目录使用 ASCII slug（与线上一致），中文名只存在于 meta.json
            slug = VectorKnowledgeBase(base_dir)._slugify(name)
            collection_dir = base_dir / slug
            collection_dir.mkdir(parents=True, exist_ok=True)
            (collection_dir / "meta.json").write_text(
                json.dumps({"name": name, "chunk_count": 1, "file_names": [file_name]}),
                encoding="utf-8",
            )

        # 共享库：公开演示数据
        make_collection(root, "共享集合", "public.pdf")
        # 用户 1 私有库
        make_collection(root / "user_1", "用户1私有", "mine.pdf")
        # 用户 2 私有库
        make_collection(root / "user_2", "用户2私有", "theirs.pdf")

        monkeypatch.setenv("VECTOR_STORE_DIR", str(root))
        yield vector_store_tools, set_user_context

    def test_user_sees_own_and_shared(self, store_root):
        """用户能看到自己的私有集合和共享集合"""
        tools_module, set_user_context = store_root
        set_user_context(1)
        try:
            result = tools_module.list_knowledge_collections.invoke({})
        finally:
            set_user_context(None)

        assert "用户1私有" in result
        assert "共享集合" in result

    def test_user_cannot_see_other_private(self, store_root):
        """用户看不到其他用户的私有集合"""
        tools_module, set_user_context = store_root
        set_user_context(2)
        try:
            result = tools_module.list_knowledge_collections.invoke({})
        finally:
            set_user_context(None)

        assert "用户2私有" in result
        assert "用户1私有" not in result

    def test_anonymous_sees_shared_only(self, store_root):
        """匿名上下文只能看到共享集合"""
        tools_module, _ = store_root

        result = tools_module.list_knowledge_collections.invoke({})

        assert "共享集合" in result
        assert "用户1私有" not in result
        assert "用户2私有" not in result


class TestVectorStoreTools:
    """vector_store_tools 工具验证（临时向量库目录）"""

    @pytest.fixture
    def kb_with_collection(self, tmp_path, monkeypatch):
        """构造包含假集合元数据的向量库目录，通过环境变量指向它"""
        from app.tools import vector_store_tools

        store_root = tmp_path / "vector_store"
        # 集合目录使用 ASCII slug（与线上一致），中文名只存在于 meta.json
        from app.rag.vector_store import VectorKnowledgeBase

        collection_dir = store_root / VectorKnowledgeBase(store_root)._slugify("测试集合")
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
        monkeypatch.setenv("VECTOR_STORE_DIR", str(store_root))
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
