"""
SQL 安全防护测试

验证只读校验器对各类语句的判定：
- 放行正常 SELECT / SHOW / WITH / EXPLAIN；
- 拦截 DDL/DML、多语句、注释绕过、空语句和超长语句；
- 表名白名单拦截拼接注入。
"""

import pytest

from app.tools.sql_guard import validate_readonly_sql, validate_table_name


class TestReadonlySqlValidation:
    """只读 SQL 校验"""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM drugs",
            "select id, name from drugs where stock > 100",
            "SELECT COUNT(*) FROM sales_records GROUP BY drug_id",
            "SELECT d.name, s.quantity FROM drugs d JOIN sales_records s ON d.drug_id = s.drug_id",
            "SHOW TABLES",
            "WITH recent AS (SELECT * FROM sales_records) SELECT * FROM recent",
            "EXPLAIN SELECT * FROM drugs",
            "SELECT * FROM drugs WHERE name LIKE '%感冒%'",
            # created_at / updated_at 这类标识符不能误伤
            "SELECT created_at, updated_at FROM drugs ORDER BY updated_at DESC",
            "SELECT * FROM 药品表",
        ],
    )
    def test_allows_readonly_queries(self, sql):
        """正常只读查询全部放行"""
        assert validate_readonly_sql(sql) is None

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE drugs",
            "DELETE FROM drugs",
            "UPDATE drugs SET stock = 0",
            "INSERT INTO drugs VALUES (1, 'x')",
            "TRUNCATE TABLE drugs",
            "ALTER TABLE drugs ADD COLUMN x INT",
            "CREATE TABLE evil (id INT)",
            "GRANT ALL ON *.* TO 'hacker'@'%'",
            "SELECT * INTO OUTFILE '/tmp/x' FROM drugs",
        ],
    )
    def test_blocks_ddl_and_dml(self, sql):
        """DDL/DML 语句全部拦截"""
        reason = validate_readonly_sql(sql)
        assert reason is not None
        assert "拒绝" in reason

    def test_blocks_multi_statement(self):
        """一次执行多条语句按注入拦截"""
        reason = validate_readonly_sql("SELECT 1; DELETE FROM drugs")

        assert reason is not None
        assert "多语句" in reason

    def test_blocks_multi_statement_hidden_by_comment(self):
        """注释后紧跟第二条语句同样拦截"""
        reason = validate_readonly_sql("SELECT 1; -- 注释\nDROP TABLE drugs")

        assert reason is not None
        assert "多语句" in reason

    def test_comment_only_drops_statements(self):
        """整条被注释包裹时按空语句拒绝"""
        reason = validate_readonly_sql("-- SELECT 1")

        assert reason is not None

    def test_comment_cannot_smuggle_keyword(self):
        """注释中隐藏的关键字不会被执行（剥离后语句本身安全则放行）"""
        # “SELECT 1 -- ; DROP TABLE drugs” 实际只会执行 SELECT 1
        assert validate_readonly_sql("SELECT 1 -- ; DROP TABLE drugs") is None

    def test_rejects_empty(self):
        """空语句拒绝"""
        assert validate_readonly_sql("") is not None
        assert validate_readonly_sql("   ") is not None

    def test_rejects_overlong(self):
        """超长语句拒绝"""
        assert validate_readonly_sql("SELECT * FROM t WHERE x = '" + "a" * 3000) is not None

    def test_keyword_inside_identifier_not_blocked(self):
        """标识符中的关键字片段（created_at）不误伤"""
        assert validate_readonly_sql("SELECT created_at FROM drugs") is None


class TestTableNameValidation:
    """表名白名单校验"""

    @pytest.mark.parametrize(
        "name",
        ["drugs", "sales_records", "药品表", "`drugs`", "t_2026"],
    )
    def test_allows_safe_names(self, name):
        """合法表名放行"""
        assert validate_table_name(name) is None

    @pytest.mark.parametrize(
        "name",
        [
            "drugs; DROP TABLE users",
            "drugs--",
            "drugs/**/OR/**/1=1",
            "",
            "   ",
            "a" * 65,
            "drugs UNION SELECT",
        ],
    )
    def test_blocks_unsafe_names(self, name):
        """含注入字符的表名拦截"""
        assert validate_table_name(name) is not None
