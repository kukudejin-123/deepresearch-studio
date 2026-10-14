"""
本地 FAISS 向量知识库管理模块

负责知识库集合（collection）的元数据管理、FAISS 索引读写和相似度检索。
每个集合对应磁盘上一个独立目录，内含：

- index.faiss      向量索引文件（FAISS 二进制格式）
- index.pkl        文档存储（langchain docstore，记录分片原文与元数据）
- meta.json        集合元数据（来源目录、文件名、分片数、模型、构建时间）

目录结构示例：
    app/data/vector_store/
    ├── 电商行业/
    │   ├── index.faiss
    │   ├── index.pkl
    │   └── meta.json
    └── 金融行业/
        └── ...
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

from app.rag.embeddings import get_embeddings
from app.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class CollectionInfo:
    """知识库集合元数据，list_knowledge_collections 工具直接面向模型暴露这些字段"""

    name: str
    chunk_count: int = 0
    file_names: list[str] = field(default_factory=list)
    embedding_model: str = ""
    created_at: str = ""

    def to_prompt_text(self) -> str:
        """格式化为模型容易阅读的多行文本"""
        files = "、".join(self.file_names) if self.file_names else "无"
        return (
            f"集合名称: {self.name}\n"
            f"  分片数量: {self.chunk_count}\n"
            f"  包含文件: {files}\n"
            f"  嵌入模型: {self.embedding_model or '未知'}\n"
            f"  构建时间: {self.created_at or '未知'}"
        )


class VectorKnowledgeBase:
    """
    本地 FAISS 向量知识库

    与外部 RAG 服务的关键差异：
    1. 索引文件保存在项目内，检索不依赖任何外部服务；
    2. 集合发现只需要读 meta.json，不需要 Embedding 密钥；
    3. 检索结果携带来源文件名，便于主智能体在报告中引用出处。
    """

    def __init__(self, root: str | Path):
        """
        :param root: 向量库根目录，例如 app/data/vector_store
        """
        self.root = Path(root)

    def _collection_dir(self, name: str) -> Path:
        """集合目录；对名称做基础清洗，避免路径穿越"""
        safe_name = Path(name).name
        return self.root / safe_name

    def list_collections(self) -> list[CollectionInfo]:
        """
        列出所有可用知识库集合

        只读取 meta.json，不需要 Embedding 客户端，也不会触发任何网络请求。
        :return: 按名称排序的集合元数据列表；无集合时返回空列表
        """
        if not self.root.exists():
            return []

        collections: list[CollectionInfo] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            meta_file = child / "meta.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("集合元数据读取失败，已跳过: %s (%s)", child.name, e)
                continue
            collections.append(
                CollectionInfo(
                    name=meta.get("name", child.name),
                    chunk_count=int(meta.get("chunk_count", 0)),
                    file_names=list(meta.get("file_names", [])),
                    embedding_model=meta.get("embedding_model", ""),
                    created_at=meta.get("created_at", ""),
                )
            )
        return collections

    def get_collection(self, name: str) -> Optional[CollectionInfo]:
        """
        按名称查询单个集合元数据

        :param name: 集合名称
        :return: 集合元数据；不存在时返回 None
        """
        for info in self.list_collections():
            if info.name == name:
                return info
        return None

    def load_store(self, name: str) -> FAISS:
        """
        加载指定集合的 FAISS 索引

        :param name: 集合名称
        :return: 可检索的 FAISS 对象
        :raises FileNotFoundError: 集合不存在或索引文件缺失
        :raises RuntimeError: 未配置 Embedding 服务（加载时仅校验存在性，不发起请求）
        """
        collection_dir = self._collection_dir(name)
        index_file = collection_dir / "index.faiss"
        if not index_file.exists():
            raise FileNotFoundError(
                f"知识库集合 '{name}' 不存在（可用集合: {[c.name for c in self.list_collections()]}）"
            )

        # allow_dangerous_deserialization 是加载本地 docstore 的官方要求；
        # 这里的 pkl 由本项目 ingest 流程生成，不属于不可信输入
        return FAISS.load_local(
            folder_path=str(collection_dir),
            embeddings=get_embeddings(),
            index_name="index",
            allow_dangerous_deserialization=True,
        )

    def search(
        self,
        name: str,
        question: str,
        top_k: int = 4,
    ) -> list[tuple[Document, float]]:
        """
        在指定集合中检索与问题最相关的文档分片

        :param name: 集合名称
        :param question: 用户问题或检索语句
        :param top_k: 返回的最大分片数
        :return: (文档分片, 相似度分数) 列表，按相关度降序
        """
        store = self.load_store(name)
        # FAISS 返回的是 L2 距离，越小越相关
        return store.similarity_search_with_score(question, k=top_k)

    def save_collection(
        self,
        name: str,
        store: FAISS,
        chunk_count: int,
        file_names: list[str],
        embedding_model: str,
    ) -> CollectionInfo:
        """
        将构建好的 FAISS 索引和元数据落盘

        :param name: 集合名称
        :param store: 已构建完成的 FAISS 对象
        :param chunk_count: 分片总数
        :param file_names: 来源文件列表
        :param embedding_model: 构建时使用的嵌入模型名
        :return: 写入的集合元数据
        """
        import datetime

        collection_dir = self._collection_dir(name)
        collection_dir.mkdir(parents=True, exist_ok=True)
        store.save_local(folder_path=str(collection_dir), index_name="index")

        info = CollectionInfo(
            name=name,
            chunk_count=chunk_count,
            file_names=file_names,
            embedding_model=embedding_model,
            created_at=datetime.datetime.now().isoformat(timespec="seconds"),
        )
        (collection_dir / "meta.json").write_text(
            json.dumps(asdict(info), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "知识库集合已保存: %s（分片 %d 个，文件 %d 个）",
            name,
            chunk_count,
            len(file_names),
        )
        return info
