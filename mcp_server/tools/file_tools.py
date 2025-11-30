import os
import shutil
from pathlib import Path
from mcp.server.types import ToolResponse

def register_file_tools(server):

    @server.tool(
        name="read_file",
        description="Reads a file inside the Jarvis project directory"
    )
    async def read_file(path: str) -> ToolResponse:
        """Read and return file contents"""
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
        try:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            return ToolResponse(content=f"OK: Copied {source} to {destination}")
        except Exception as e:
            return ToolResponse(content=f"ERROR copying {source} to {destination}: {e}", is_error=True)

    @server.tool(
        name="cleanup_project",
        description="Remove unnecessary files and directories from the project"
    )
    async def cleanup_project(base_path: str = ".") -> ToolResponse:
        """Clean up unnecessary project files"""
        try:
            cleanup_items = [
                "__pycache__",
                ".pytest_cache",
                "*.pyc",
                ".egg-info",
                "node_modules",
                ".DS_Store",
                "*.log",
            ]
            
            removed = []
            base = Path(base_path)
            
            # Remove common unnecessary directories
            for pattern in ["__pycache__", ".pytest_cache", ".egg-info"]:
                for item in base.rglob(pattern):
                    if item.is_dir():
                        try:
                            shutil.rmtree(item)
                            removed.append(f"DIR: {item}")
                        except:
                            pass
            
            # Remove .pyc files
            for item in base.rglob("*.pyc"):
                try:
                    item.unlink()
                    removed.append(f"FILE: {item}")
                except:
                    pass
            
            # Remove log files
            for item in base.rglob("*.log"):
                try:
                    item.unlink()
                    removed.append(f"FILE: {item}")
                except:
                    pass
            
            result = f"OK: Cleanup complete\nRemoved {len(removed)} items:\n" + "\n".join(removed[:20])
            if len(removed) > 20:
                result += f"\n... and {len(removed) - 20} more items"
            
            return ToolResponse(content=result)
        except Exception as e:
            return ToolResponse(content=f"ERROR during cleanup: {e}", is_error=True)
