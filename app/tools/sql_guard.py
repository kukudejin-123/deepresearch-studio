"""
SQL 安全防护模块

第一期安全改造：数据库查询助手直接执行模型生成的 SQL，原版仅靠提示词
约束“生成只读查询”，这显然不可靠——模型一旦生成 DROP/DELETE 就会直接
作用到业务库。本模块在工具层增加硬约束：

1. 只允许 SELECT / SHOW / WITH...SELECT 开头的语句；
2. 用词边界匹配拦截 DDL/DML 关键字（DROP、DELETE、UPDATE、INSERT 等）；
3. 拒绝多语句执行（分号后还有内容视为注入尝试）；
4. 先剥离注释再校验，防止 `SELECT 1; -- DROP TABLE` 这类绕过；
5. 表名做白名单字符校验，杜绝 get_table_data 的拼接注入。
"""

import re
from typing import Optional

# 允许的语句开头关键字（大写比较）
_ALLOWED_PREFIXES = ("SELECT", "SHOW", "WITH", "EXPLAIN")

# 一旦出现即拒绝的关键字（词边界匹配，避免误伤 like、updated_at 这类标识符）
_FORBIDDEN_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "REPLACE",
    "MERGE",
    "CALL",
    "LOCK",
    "UNLOCK",
    "LOAD",
    "COPY",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "SET",
    "USE",
    "INTO",
)

# 语句长度上限，防止异常超长输入
_MAX_SQL_LENGTH = 2000

_FORBIDDEN_PATTERN = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# 表名白名单：字母、数字、下划线，可选中文字符（业务表偶有中文名）
_TABLE_NAME_PATTERN = re.compile(r"^[\w一-龥]+$")

_LINE_COMMENT_PATTERN = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_sql_comments(sql: str) -> str:
    """
    剥离 SQL 中的行注释和块注释

    注释可能被用来隐藏真实语句（例如 `SELECT 1 -- ; DROP TABLE x`），
    校验必须在剥离注释后的文本上进行。
    """
    without_block = _BLOCK_COMMENT_PATTERN.sub(" ", sql)
    return _LINE_COMMENT_PATTERN.sub(" ", without_block)


def validate_readonly_sql(query: str) -> Optional[str]:
    """
    校验一条 SQL 是否为安全的只读查询

    :param query: 待校验的 SQL 语句
    :return: 拒绝原因；通过时返回 None
    """
    if not query or not query.strip():
        return "SQL 语句为空"

    if len(query) > _MAX_SQL_LENGTH:
        return f"SQL 语句过长（超过 {_MAX_SQL_LENGTH} 字符）"

    # 先剥离注释再校验，防止注释绕过关键字检查
    normalized = strip_sql_comments(query).strip().rstrip(";").strip()
    if not normalized:
        return "SQL 语句在剥离注释后为空"

    first_word = normalized.split(None, 1)[0].upper()
    if first_word not in _ALLOWED_PREFIXES:
        return (
            f"仅支持只读查询（{' / '.join(_ALLOWED_PREFIXES)} 开头），"
            f"当前语句以 {first_word} 开头，已被安全策略拒绝"
        )

    # 多语句检测：去掉末尾分号后，中间出现分号说明尝试一次执行多条语句
    body = normalized.rstrip(";")
    if ";" in body:
        return "检测到多语句执行尝试，已被安全策略拒绝"

    matched = _FORBIDDEN_PATTERN.search(normalized)
    if matched:
        return (
            f"SQL 中包含被禁止的关键字 {matched.group(1).upper()}，仅允许只读查询，已被安全策略拒绝"
        )

    return None


def validate_table_name(table_name: str) -> Optional[str]:
    """
    校验表名是否安全（防止拼接注入）

    :param table_name: 待校验的表名
    :return: 拒绝原因；通过时返回 None
    """
    if not table_name or not table_name.strip():
        return "表名为空"

    name = table_name.strip().strip("`")
    if not _TABLE_NAME_PATTERN.match(name):
        return f"表名 '{table_name}' 包含非法字符，仅允许字母、数字、下划线和中文"
    if len(name) > 64:
        return "表名过长"
    return None
