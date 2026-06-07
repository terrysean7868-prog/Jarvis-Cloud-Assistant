import os
import shutil
from pathlib import Path
from mcp.server.types import ToolResponse

# Define the base directory for all file operations (project root)
SANDBOX_BASE = Path(".").resolve()

def validate_path(path: str) -> tuple[bool, str]:
    """
    Validate that the given path is within the sandbox and safe to access.
    Returns (is_valid, error_message)
    """
    try:
        resolved = Path(path).resolve()
        # Check if path is within sandbox
        if not str(resolved).startswith(str(SANDBOX_BASE)):
            return False, f"Path {path} is outside project directory"
        return True, ""
    except Exception as e:
        return False, f"Invalid path: {str(e)}"

def register_file_tools(server):

    @server.tool(
        name="read_file",
        description="Reads a file inside the Jarvis project directory"
    )
    async def read_file(path: str) -> ToolResponse:
        """Read and return file contents"""
        valid, error = validate_path(path)
        if not valid:
            return ToolResponse(content=f"ERROR: {error}", is_error=True)

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return ToolResponse(content=f"File: {path}\n\n{content}")
        except Exception as e:
            return ToolResponse(content=f"ERROR reading {path}: {e}", is_error=True)

    @server.tool(
        name="write_file",
        description="Write content to a file (creates or overwrites)"
    )
    async def write_file(path: str, content: str) -> ToolResponse:
        """Write or overwrite a file with content"""
        valid, error = validate_path(path)
        if not valid:
            return ToolResponse(content=f"ERROR: {error}", is_error=True)

        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return ToolResponse(content=f"OK: File written to {path} ({len(content)} bytes)")
        except Exception as e:
            return ToolResponse(content=f"ERROR writing {path}: {e}", is_error=True)

    @server.tool(
        name="patch_file",
        description="Search and replace inside a file"
    )
    async def patch_file(path: str, search: str, replace: str) -> ToolResponse:
        """Replace text in a file"""
        valid, error = validate_path(path)
        if not valid:
            return ToolResponse(content=f"ERROR: {error}", is_error=True)

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = f.read()
            if search not in data:
                return ToolResponse(content=f"WARNING: Search string not found in {path}", is_error=True)
            updated = data.replace(search, replace)
            with open(path, "w", encoding="utf-8") as f:
                f.write(updated)
            return ToolResponse(content=f"OK: Patched {path}")
        except Exception as e:
            return ToolResponse(content=f"ERROR patching {path}: {e}", is_error=True)

    @server.tool(
        name="list_files",
        description="List files in a directory"
    )
    async def list_files(directory: str) -> ToolResponse:
        """List directory contents"""
        valid, error = validate_path(directory)
        if not valid:
            return ToolResponse(content=f"ERROR: {error}", is_error=True)

        try:
            items = []
            for item in Path(directory).iterdir():
                if item.is_file():
                    size = item.stat().st_size
                    items.append(f"FILE: {item.name} ({size} bytes)")
                else:
                    items.append(f"DIR:  {item.name}/")
            return ToolResponse(content=f"Contents of {directory}:\n" + "\n".join(items))
        except Exception as e:
            return ToolResponse(content=f"ERROR listing {directory}: {e}", is_error=True)

    @server.tool(
        name="delete_file",
        description="Delete a file"
    )
    async def delete_file(path: str) -> ToolResponse:
        """Delete a file"""
        valid, error = validate_path(path)
        if not valid:
            return ToolResponse(content=f"ERROR: {error}", is_error=True)

        try:
            Path(path).unlink()
            return ToolResponse(content=f"OK: File deleted {path}")
        except Exception as e:
            return ToolResponse(content=f"ERROR deleting {path}: {e}", is_error=True)

    @server.tool(
        name="delete_directory",
        description="Recursively delete a directory"
    )
    async def delete_directory(path: str) -> ToolResponse:
        """Delete a directory and all contents"""
        valid, error = validate_path(path)
        if not valid:
            return ToolResponse(content=f"ERROR: {error}", is_error=True)

        try:
            shutil.rmtree(path)
            return ToolResponse(content=f"OK: Directory deleted {path}")
        except Exception as e:
            return ToolResponse(content=f"ERROR deleting directory {path}: {e}", is_error=True)

    @server.tool(
        name="create_directory",
        description="Create a directory and parent directories if needed"
    )
    async def create_directory(path: str) -> ToolResponse:
        """Create directory structure"""
        valid, error = validate_path(path)
        if not valid:
            return ToolResponse(content=f"ERROR: {error}", is_error=True)

        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            return ToolResponse(content=f"OK: Directory created {path}")
        except Exception as e:
            return ToolResponse(content=f"ERROR creating directory {path}: {e}", is_error=True)

    @server.tool(
        name="copy_file",
        description="Copy a file from source to destination"
    )
    async def copy_file(source: str, destination: str) -> ToolResponse:
        """Copy a file"""
        valid_src, error_src = validate_path(source)
        valid_dst, error_dst = validate_path(destination)

        if not valid_src:
            return ToolResponse(content=f"ERROR: {error_src}", is_error=True)
        if not valid_dst:
            return ToolResponse(content=f"ERROR: {error_dst}", is_error=True)

        try:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            return ToolResponse(content=f"OK: Copied {source} to {destination}")
        except Exception as e:
            return ToolResponse(content=f"ERROR copying {source} to {destination}: {e}", is_error=True)
