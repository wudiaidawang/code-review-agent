"""pytest 共享 fixtures。"""

import pytest
from tests.helpers import patch_git_tool


@pytest.fixture
def fixed_git_diff(monkeypatch):
    """用固定 change_set 替换 GitTool.execute，确保测试输入不受仓库提交历史影响。"""
    patch_git_tool(monkeypatch)
