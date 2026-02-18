"""
Project management utilities for content-posting-lab.
Handles project CRUD operations, path utilities, and filesystem-safe name validation.
"""

import re
from pathlib import Path
from typing import Optional

# Base directory and projects root
BASE_DIR = Path(__file__).parent
PROJECTS_DIR = BASE_DIR / "projects"


def sanitize_project_name(name: str) -> str:
    """
    Sanitize project name for filesystem safety.

    - Converts to lowercase
    - Replaces spaces with hyphens
    - Removes unsafe characters (keeps alphanumeric, hyphens, underscores)
    - Limits to 100 characters
    - Blocks path traversal attempts

    Args:
        name: Raw project name

    Returns:
        Sanitized project name

    Raises:
        ValueError: If name contains path traversal attempts or is empty after sanitization
    """
    if not name or not isinstance(name, str):
        raise ValueError("Project name must be a non-empty string")

    # Block path traversal attempts
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError(
            "Project name cannot contain path traversal characters (.., /, \\)"
        )

    # Convert to lowercase and replace spaces with hyphens
    sanitized = name.lower().replace(" ", "-")

    # Remove unsafe characters (keep alphanumeric, hyphens, underscores)
    sanitized = re.sub(r"[^a-z0-9\-_]", "", sanitized)

    # Limit to 100 characters
    sanitized = sanitized[:100]

    # Ensure not empty after sanitization
    if not sanitized:
        raise ValueError(
            "Project name must contain at least one alphanumeric character"
        )

    return sanitized


def create_project(name: str) -> Path:
    """
    Create a new project with subdirectories.

    Creates:
    - projects/{name}/
    - projects/{name}/videos/
    - projects/{name}/captions/
    - projects/{name}/burned/

    Args:
        name: Project name (will be sanitized)

    Returns:
        Path to the created project directory

    Raises:
        ValueError: If name is invalid
        FileExistsError: If project already exists
    """
    sanitized_name = sanitize_project_name(name)
    project_path = PROJECTS_DIR / sanitized_name

    if project_path.exists():
        raise FileExistsError(f"Project '{sanitized_name}' already exists")

    # Create project root and subdirectories
    project_path.mkdir(parents=True, exist_ok=False)
    (project_path / "videos").mkdir(exist_ok=True)
    (project_path / "captions").mkdir(exist_ok=True)
    (project_path / "burned").mkdir(exist_ok=True)

    return project_path


def list_projects() -> list[dict]:
    """
    List all projects with metadata.

    Returns:
        List of dicts with keys:
        - name: Project name
        - path: Full path to project
        - video_count: Number of videos in videos/
        - caption_count: Number of captions in captions/
        - burned_count: Number of burned videos in burned/
    """
    if not PROJECTS_DIR.exists():
        return []

    projects = []
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        videos_dir = project_dir / "videos"
        captions_dir = project_dir / "captions"
        burned_dir = project_dir / "burned"

        video_count = len(list(videos_dir.glob("*"))) if videos_dir.exists() else 0
        caption_count = (
            len(list(captions_dir.glob("*"))) if captions_dir.exists() else 0
        )
        burned_count = len(list(burned_dir.glob("*"))) if burned_dir.exists() else 0

        projects.append(
            {
                "name": project_dir.name,
                "path": str(project_dir),
                "video_count": video_count,
                "caption_count": caption_count,
                "burned_count": burned_count,
            }
        )

    return projects


def get_project(name: str) -> Optional[dict]:
    """
    Get a single project's information.

    Args:
        name: Project name (will be sanitized)

    Returns:
        Project dict or None if not found
    """
    sanitized_name = sanitize_project_name(name)
    project_path = PROJECTS_DIR / sanitized_name

    if not project_path.exists():
        return None

    videos_dir = project_path / "videos"
    captions_dir = project_path / "captions"
    burned_dir = project_path / "burned"

    video_count = len(list(videos_dir.glob("*"))) if videos_dir.exists() else 0
    caption_count = len(list(captions_dir.glob("*"))) if captions_dir.exists() else 0
    burned_count = len(list(burned_dir.glob("*"))) if burned_dir.exists() else 0

    return {
        "name": project_path.name,
        "path": str(project_path),
        "video_count": video_count,
        "caption_count": caption_count,
        "burned_count": burned_count,
    }


def delete_project(name: str) -> bool:
    """
    Delete a project and all its contents.

    Args:
        name: Project name (will be sanitized)

    Returns:
        True if deleted, False if not found
    """
    sanitized_name = sanitize_project_name(name)
    project_path = PROJECTS_DIR / sanitized_name

    if not project_path.exists():
        return False

    # Safety check: ensure we're deleting a project directory
    if (
        not (project_path / "videos").exists()
        or not (project_path / "captions").exists()
    ):
        raise ValueError(
            f"Directory {project_path} does not appear to be a valid project"
        )

    # Remove directory tree
    import shutil

    shutil.rmtree(project_path)

    return True


def get_project_video_dir(name: str) -> Path:
    """Get the videos subdirectory for a project."""
    sanitized_name = sanitize_project_name(name)
    return PROJECTS_DIR / sanitized_name / "videos"


def get_project_caption_dir(name: str) -> Path:
    """Get the captions subdirectory for a project."""
    sanitized_name = sanitize_project_name(name)
    return PROJECTS_DIR / sanitized_name / "captions"


def get_project_burn_dir(name: str) -> Path:
    """Get the burned subdirectory for a project."""
    sanitized_name = sanitize_project_name(name)
    return PROJECTS_DIR / sanitized_name / "burned"


def ensure_default_project() -> Path:
    """
    Ensure 'quick-test' default project exists.
    Creates it if no projects exist.

    Returns:
        Path to the quick-test project
    """
    projects = list_projects()

    if projects:
        # Projects already exist, return the first one or quick-test if it exists
        for proj in projects:
            if proj["name"] == "quick-test":
                return PROJECTS_DIR / "quick-test"
        return PROJECTS_DIR / projects[0]["name"]

    # No projects exist, create quick-test
    return create_project("quick-test")


if __name__ == "__main__":
    # Quick test
    print("Testing project_manager.py...")

    # Test sanitization
    assert sanitize_project_name("Drake Release!!!") == "drake-release"
    assert sanitize_project_name("My Project 123") == "my-project-123"
    assert sanitize_project_name("test_project") == "test_project"
    print("✓ Sanitization tests passed")

    # Test path traversal blocking
    try:
        sanitize_project_name("../../etc")
        assert False, "Should have raised ValueError"
    except ValueError:
        print("✓ Path traversal blocking works")

    # Test project creation
    try:
        test_proj = create_project("test-project-xyz")
        assert test_proj.exists()
        assert (test_proj / "videos").exists()
        assert (test_proj / "captions").exists()
        assert (test_proj / "burned").exists()
        print(f"✓ Project creation works: {test_proj}")

        # Test get_project
        proj_info = get_project("test-project-xyz")
        assert proj_info is not None
        assert proj_info["name"] == "test-project-xyz"
        print(f"✓ get_project works: {proj_info}")

        # Test list_projects
        projects = list_projects()
        assert len(projects) > 0
        print(f"✓ list_projects works: {len(projects)} projects found")

        # Test delete_project
        deleted = delete_project("test-project-xyz")
        assert deleted
        assert not test_proj.exists()
        print("✓ delete_project works")

    except Exception as e:
        print(f"✗ Error during testing: {e}")
        raise

    print("\nAll tests passed!")
