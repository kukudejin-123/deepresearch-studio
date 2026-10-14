"""
app.utils.path_utils 路径解析测试

覆盖三类关键场景：
1. 普通相对路径落到会话目录；
2. 模型常见虚拟前缀（/workspace、/mnt/data）被正确剥离；
3. 越界场景（session 名重复嵌套、output 前缀）被收敛回会话目录；
4. resolve_write_path 对 '../' 和目录外绝对路径的写入收敛。
"""

from app.utils.path_utils import resolve_path, resolve_write_path


class TestResolvePath:
    """resolve_path 行为验证"""

    def test_relative_path_resolves_into_session_dir(self, tmp_path):
        """相对路径应拼接在会话目录下"""
        session_dir = tmp_path / "session_1"
        session_dir.mkdir()

        result = resolve_path("报告.md", str(session_dir))

        assert result == str(session_dir / "报告.md")

    def test_relative_subdir_path(self, tmp_path):
        """带子目录的相对路径应保留目录层级"""
        session_dir = tmp_path / "session_1"
        session_dir.mkdir()

        result = resolve_path("sub_dir/报告.md", str(session_dir))

        assert result == str(session_dir / "sub_dir" / "报告.md")

    def test_strips_virtual_sandbox_prefix(self, tmp_path):
        """模型返回的 /workspace 等沙箱前缀应被剥离，改落到会话目录"""
        session_dir = tmp_path / "session_1"
        session_dir.mkdir()

        result = resolve_path("/workspace/报告.md", str(session_dir))

        assert result == str(session_dir / "报告.md")

    def test_session_name_duplication_collapses(self, tmp_path):
        """session_xxx/session_xxx/file.md 重复嵌套应折叠为单层"""
        session_dir = tmp_path / "session_1"
        session_dir.mkdir()

        result = resolve_path("session_1/session_1/报告.md", str(session_dir))

        assert result == str(session_dir / "报告.md")

    def test_output_prefix_collapses(self, tmp_path):
        """output 前缀不应叠加到会话目录之外"""
        session_dir = tmp_path / "session_1"
        session_dir.mkdir()

        result = resolve_path("output/报告.md", str(session_dir))

        assert result == str(session_dir / "报告.md")

    def test_absolute_path_outside_session_dir_kept(self, tmp_path):
        """会话目录外的真实绝对路径应保持原样，避免误改外部资源"""
        session_dir = tmp_path / "session_1"
        session_dir.mkdir()
        outside = tmp_path / "external" / "data.csv"

        result = resolve_path(str(outside), str(session_dir))

        assert result == str(outside)

    def test_without_session_dir_resolves_plain(self, tmp_path, monkeypatch):
        """未提供会话目录时按普通路径解析"""
        monkeypatch.chdir(tmp_path)

        result = resolve_path("plain.md")

        assert result == str((tmp_path / "plain.md").resolve())


class TestResolveWritePath:
    """resolve_write_path 写入边界验证（防路径穿越）"""

    def test_relative_path_kept_inside_session(self, tmp_path):
        """会话目录内的相对路径原样保留"""
        session_dir = tmp_path / "session_1"
        session_dir.mkdir()

        result = resolve_write_path("sub/报告.md", str(session_dir))

        assert result == str(session_dir / "sub" / "报告.md")

    def test_parent_traversal_clamped_into_session(self, tmp_path):
        """'../' 穿越必须被收敛为会话目录内的同名文件"""
        session_dir = tmp_path / "session_1"
        session_dir.mkdir()

        result = resolve_write_path("../逃逸.md", str(session_dir))

        assert result == str(session_dir / "逃逸.md")

    def test_absolute_outside_clamped_into_session(self, tmp_path):
        """会话目录外的绝对路径收敛为会话目录内的同名文件"""
        session_dir = tmp_path / "session_1"
        session_dir.mkdir()
        outside = tmp_path / "evil.md"

        result = resolve_write_path(str(outside), str(session_dir))

        assert result == str(session_dir / "evil.md")

    def test_absolute_inside_session_kept(self, tmp_path):
        """会话目录内的绝对路径原样保留"""
        session_dir = tmp_path / "session_1"
        session_dir.mkdir()
        inside = session_dir / "报告.md"

        result = resolve_write_path(str(inside), str(session_dir))

        assert result == str(inside)
