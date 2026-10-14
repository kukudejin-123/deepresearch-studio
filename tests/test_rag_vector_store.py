"""
app.rag 本地向量知识库测试

使用 langchain_community 的 FakeEmbeddings（确定性伪向量）替代真实
Embedding 服务，完整走通：文档加载 -> 分片 -> 建库 -> 落盘 -> 集合发现 ->
检索的全链路，不依赖任何外部 API。
"""

import pytest
from langchain_community.embeddings import FakeEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

from app.rag import vector_store as vector_store_module
from app.rag.ingest import load_single_file, split_documents
from app.rag.vector_store import VectorKnowledgeBase


@pytest.fixture
def fake_embeddings():
    """确定性伪 Embedding，维度 32"""
    return FakeEmbeddings(size=32)


@pytest.fixture
def knowledge_base(tmp_path):
    """指向临时目录的向量知识库"""
    return VectorKnowledgeBase(tmp_path / "vector_store")


@pytest.fixture
def built_store(knowledge_base, fake_embeddings, monkeypatch):
    """构建一个包含两个文档分片的测试集合"""
    monkeypatch.setattr(vector_store_module, "get_embeddings", lambda: fake_embeddings)

    documents = [
        Document(
            page_content="电商直播行业 2026 年保持高速增长，AI 数字人渗透率持续提升。",
            metadata={"source": "电商报告.pdf"},
        ),
        Document(
            page_content="货币政策执行报告指出，四季度社会融资规模稳中有升。",
            metadata={"source": "金融报告.pdf"},
        ),
    ]
    store = FAISS.from_documents(documents, fake_embeddings)
    knowledge_base.save_collection(
        name="测试集合",
        store=store,
        chunk_count=len(documents),
        file_names=["电商报告.pdf", "金融报告.pdf"],
        embedding_model="fake-embeddings",
    )
    return knowledge_base


class TestVectorKnowledgeBase:
    """VectorKnowledgeBase 行为验证"""

    def test_list_collections_reads_meta(self, built_store):
        """集合发现只依赖 meta.json，不需要 Embedding 服务"""
        collections = built_store.list_collections()

        assert len(collections) == 1
        info = collections[0]
        assert info.name == "测试集合"
        assert info.chunk_count == 2
        assert info.file_names == ["电商报告.pdf", "金融报告.pdf"]

    def test_empty_store_returns_empty_list(self, knowledge_base):
        """空目录（甚至目录不存在）时返回空列表"""
        assert knowledge_base.list_collections() == []

    def test_get_collection_by_name(self, built_store):
        """按名称查询集合元数据"""
        info = built_store.get_collection("测试集合")

        assert info is not None
        assert info.name == "测试集合"

    def test_load_missing_collection_raises(self, built_store):
        """加载不存在的集合抛出 FileNotFoundError，且错误信息包含可用集合"""
        with pytest.raises(FileNotFoundError, match="测试集合"):
            built_store.load_store("不存在的集合")

    def test_search_returns_scored_documents(self, built_store):
        """检索返回带来源元数据和距离分数的分片"""
        results = built_store.search("测试集合", "电商直播增长趋势", top_k=1)

        assert len(results) == 1
        doc, score = results[0]
        assert doc.metadata["source"] in {"电商报告.pdf", "金融报告.pdf"}
        assert isinstance(score, float)

    def test_save_and_reload_roundtrip(self, knowledge_base, fake_embeddings, monkeypatch):
        """保存后重新加载的索引可以正常检索（落盘链路完整）"""
        monkeypatch.setattr(vector_store_module, "get_embeddings", lambda: fake_embeddings)
        documents = [Document(page_content="向量检索测试文档内容。", metadata={"source": "t.md"})]
        store = FAISS.from_documents(documents, fake_embeddings)
        knowledge_base.save_collection(
            name="落盘集合",
            store=store,
            chunk_count=1,
            file_names=["t.md"],
            embedding_model="fake-embeddings",
        )

        reloaded = knowledge_base.load_store("落盘集合")
        results = reloaded.similarity_search("向量检索", k=1)

        assert len(results) == 1
        assert "向量检索测试文档内容" in results[0].page_content

    def test_collection_name_traversal_is_sanitized(self, knowledge_base):
        """集合名称中的路径穿越字符应被清洗，不能跳出向量库根目录"""
        with pytest.raises(FileNotFoundError):
            knowledge_base.load_store("../../etc/passwd")


class TestIngest:
    """文档加载与分片测试"""

    def test_load_markdown_file(self, tmp_path):
        """Markdown 文件加载为 Document，元数据记录来源"""
        md_file = tmp_path / "报告.md"
        md_file.write_text("# 标题\n\n正文内容", encoding="utf-8")

        documents = load_single_file(md_file)

        assert len(documents) == 1
        assert documents[0].metadata["source"] == "报告.md"
        assert "正文内容" in documents[0].page_content

    def test_load_unsupported_suffix_skipped(self, tmp_path):
        """不支持的文件类型返回空列表"""
        binary = tmp_path / "data.bin"
        binary.write_bytes(b"\x00\x01\x02")

        assert load_single_file(binary) == []

    def test_split_documents_produces_chunks(self):
        """长文档分片后产生多个块，且保留来源元数据"""
        long_text = "这是测试内容。" * 200
        documents = [Document(page_content=long_text, metadata={"source": "长文档.md"})]

        chunks = split_documents(documents)

        assert len(chunks) > 1
        assert all(chunk.metadata["source"] == "长文档.md" for chunk in chunks)

    def test_split_preserves_short_document(self):
        """短文档不分片，原样返回"""
        documents = [Document(page_content="很短的内容", metadata={"source": "s.md"})]

        chunks = split_documents(documents)

        assert len(chunks) == 1
        assert chunks[0].page_content == "很短的内容"
