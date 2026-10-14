"""
上传安全与限流测试

- upload_guard：文件名清洗、扩展名白名单、大小上限读取；
- rate_limit：滑动窗口的放行/阻断/窗口滑过/手动重置。
"""

import time

import pytest

from app.api.rate_limit import RateLimiter
from app.api.upload_guard import (
    get_allowed_extensions,
    get_max_upload_bytes,
    sanitize_filename,
    validate_upload,
)


class TestUploadGuard:
    """上传文件校验"""

    def test_allows_document_types(self):
        """文档类型全部放行"""
        for filename in ["报告.pdf", "数据.xlsx", "笔记.md", "表格.csv", "文档.docx"]:
            assert validate_upload(filename) is None, filename

    @pytest.mark.parametrize(
        "filename",
        ["evil.exe", "script.sh", "virus.bat", "hack.php", "x"],
    )
    def test_blocks_disallowed_types(self, filename):
        """白名单外类型拦截"""
        reason = validate_upload(filename)
        assert reason is not None
        assert "不支持" in reason or "扩展名" in reason

    def test_blocks_missing_extension(self):
        """无扩展名文件拦截"""
        assert validate_upload("README") is not None

    def test_sanitize_strips_directory_components(self):
        """路径成分被剥离，只保留 basename"""
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("C:\\Users\\x\\evil.md") == "evil.md"

    def test_sanitize_removes_control_and_reserved_chars(self):
        """控制字符和 Windows 保留字符被移除"""
        assert sanitize_filename("a\x00b?.md") == "ab.md"

    def test_sanitize_rejects_empty_and_hidden(self):
        """空名和隐藏文件拒绝"""
        assert sanitize_filename("") is None
        assert sanitize_filename("   ") is None
        assert sanitize_filename(".env") is None

    def test_sanitize_truncates_long_names(self):
        """超长文件名截断但保留扩展名"""
        long_name = "a" * 200 + ".md"
        safe = sanitize_filename(long_name)

        assert safe is not None
        assert len(safe) <= 100
        assert safe.endswith(".md")

    def test_max_upload_bytes_from_env(self, monkeypatch):
        """大小上限读取环境变量"""
        monkeypatch.setenv("UPLOAD_MAX_MB", "5")

        assert get_max_upload_bytes() == 5 * 1024 * 1024

    def test_allowed_extensions_from_env(self, monkeypatch):
        """扩展名白名单读取环境变量"""
        monkeypatch.setenv("UPLOAD_ALLOWED_EXTENSIONS", ".md,.txt")

        assert get_allowed_extensions() == {".md", ".txt"}


class TestRateLimiter:
    """滑动窗口限流器"""

    def test_allows_up_to_limit(self):
        """窗口内允许 max_requests 次"""
        limiter = RateLimiter(max_requests=3, window_seconds=60)

        results = [limiter.check("key")[0] for _ in range(3)]

        assert results == [True, True, True]

    def test_blocks_over_limit(self):
        """超过上限被阻断并返回等待时间"""
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        limiter.check("key")
        limiter.check("key")

        allowed, retry_after = limiter.check("key")

        assert allowed is False
        assert retry_after > 0

    def test_window_slides(self):
        """窗口滑过后恢复放行"""
        limiter = RateLimiter(max_requests=1, window_seconds=0.2)
        assert limiter.check("key")[0] is True
        assert limiter.check("key")[0] is False

        time.sleep(0.25)

        assert limiter.check("key")[0] is True

    def test_keys_are_independent(self):
        """不同限流键互不影响"""
        limiter = RateLimiter(max_requests=1, window_seconds=60)

        assert limiter.check("user:1")[0] is True
        assert limiter.check("user:2")[0] is True
        assert limiter.check("user:1")[0] is False

    def test_reset_clears_key(self):
        """手动重置后重新放行"""
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        limiter.check("key")

        limiter.reset("key")

        assert limiter.check("key")[0] is True
