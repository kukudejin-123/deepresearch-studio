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

import hashlib
import json
import pickle
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import faiss
import numpy as np
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.rag.embeddings import get_embeddings
from app.utils.logging_config import get_logger

logger = get_logger(__name__)

INDEX_FILE_NAME = "index.faiss"
DOCSTORE_FILE_NAME = "index.pkl"


def _save_faiss_index(store: FAISS, collection_dir: Path) -> None:
    """
    将 FAISS 索引与文档存储写入集合目录

    为什么不直接用 FAISS.save_local：其内部调用 faiss.write_index，
    走 C++ 窄字符文件 IO，Windows 上路径含任何非 ASCII 字符
    （例如中文项目目录、中文 Windows 用户名）都会写盘失败。
    这里改用 faiss.serialize_index 得到字节数组后用 Python 写文件，
    路径安全性由 Python 层保证，跨平台行为一致。
    """
    index_bytes = faiss.serialize_index(store.index)
    (collection_dir / INDEX_FILE_NAME).write_bytes(index_bytes.tobytes())

    with open(collection_dir / DOCSTORE_FILE_NAME, "wb") as f:
        pickle.dump((store.docstore, store.index_to_docstore_id), f)


def _load_faiss_index(collection_dir: Path, embeddings: Embeddings) -> FAISS:
    """
    从集合目录还原 FAISS 对象

    :param collection_dir: 集合目录
    :param embeddings: 检索时使用的 Embedding 客户端
    :return: 可检索的 FAISS 对象
    """
    raw = (collection_dir / INDEX_FILE_NAME).read_bytes()
    index = faiss.deserialize_index(np.frombuffer(raw, dtype=np.uint8))

    with open(collection_dir / DOCSTORE_FILE_NAME, "rb") as f:
        docstore, index_to_docstore_id = pickle.load(f)

    return FAISS(embeddings, index, docstore, index_to_docstore_id)


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

    def _slugify(self, name: str) -> str:
        """
        集合名称转 ASCII 安全的目录名

        FAISS 的 C++ 写入层（faiss.write_index）在 Windows 上无法打开包含
        非 ASCII 字符的路径，直接以中文名建目录会写盘失败。因此磁盘目录统一
        使用“ASCII 片段 + 名称哈希”，中文展示名保留在 meta.json 的 name 字段，
        对上层工具和调用方完全透明。
        """
        ascii_part = re.sub(r"[^A-Za-z0-9_-]+", "", name)[:20]
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        prefix = ascii_part or "collection"
        return f"{prefix}_{digest}"

    def _collection_dir(self, name: str) -> Path:
        """集合目录；名称经 slug 化处理，天然免疫路径穿越"""
        return self.root / self._slugify(name)

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
        """
        collection_dir = self._collection_dir(name)
        index_file = collection_dir / INDEX_FILE_NAME
        if not index_file.exists():
            available = [c.name for c in self.list_collections()]
            raise FileNotFoundError(f"知识库集合 '{name}' 不存在（可用集合: {available}）")

        return _load_faiss_index(collection_dir, get_embeddings())

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
        # FAISS 返回的是 L2 距离（numpy.float32），越小越相关；
        # 统一转成 Python float，避免调用方处理 numpy 类型
        scored = store.similarity_search_with_score(question, k=top_k)
        return [(doc, float(score)) for doc, score in scored]

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
        _save_faiss_index(store, collection_dir)

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
