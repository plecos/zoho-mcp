from zoho_mcp.config import PROJECT_ROOT


def test_project_root_points_to_repo_root():
    assert (PROJECT_ROOT / "pyproject.toml").exists()
