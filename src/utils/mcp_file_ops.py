"""
MCP Server File Operations Integration
Exposes MCP server file operation tools as FastAPI endpoints
"""

import os
import logging
from typing import Optional, Dict, Any, List
from pathlib import Path
import asyncio
import subprocess

logger = logging.getLogger(__name__)

# MCP Server configuration
MCP_SERVER_HOST = "localhost"
MCP_SERVER_PORT = 9090
MCP_SERVER_URL = f"http://{MCP_SERVER_HOST}:{MCP_SERVER_PORT}"


# Local file operations root (safety sandbox)
# Defaults to repo root (two levels up from src/utils).
LOCAL_FILE_OPS_ROOT = Path(
    str(Path(__file__).resolve().parents[2])
).resolve()


def _resolve_local_safe_path(user_path: str) -> Path:
    if not isinstance(user_path, str) or not user_path.strip():
        raise ValueError("path is required")

    p = Path(user_path.strip())
    if not p.is_absolute():
        p = (LOCAL_FILE_OPS_ROOT / p).resolve()
    else:
        p = p.resolve()

    try:
        p.relative_to(LOCAL_FILE_OPS_ROOT)
    except Exception:
        raise ValueError(f"Path is outside sandbox root: {LOCAL_FILE_OPS_ROOT}")
    return p


class MCPFileOperations:
    """Interface to MCP server file operations"""
    
    @staticmethod
    async def read_file(file_path: str) -> Dict[str, Any]:
        """Read file content via MCP"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MCP_SERVER_URL}/tools/read_file",
                    json={"path": file_path}
                ) as resp:
                    return await resp.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    async def write_file(file_path: str, content: str) -> Dict[str, Any]:
        """Write content to file via MCP"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MCP_SERVER_URL}/tools/write_file",
                    json={"path": file_path, "content": content}
                ) as resp:
                    return await resp.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    async def list_files(directory: str) -> Dict[str, Any]:
        """List files in directory via MCP"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MCP_SERVER_URL}/tools/list_files",
                    json={"directory": directory}
                ) as resp:
                    return await resp.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    async def delete_file(file_path: str) -> Dict[str, Any]:
        """Delete file via MCP"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MCP_SERVER_URL}/tools/delete_file",
                    json={"path": file_path}
                ) as resp:
                    return await resp.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    async def create_directory(dir_path: str) -> Dict[str, Any]:
        """Create directory via MCP"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MCP_SERVER_URL}/tools/create_directory",
                    json={"path": dir_path}
                ) as resp:
                    return await resp.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    async def copy_file(source: str, destination: str) -> Dict[str, Any]:
        """Copy file via MCP"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MCP_SERVER_URL}/tools/copy_file",
                    json={"source": source, "destination": destination}
                ) as resp:
                    return await resp.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    async def cleanup_project() -> Dict[str, Any]:
        """Clean up project cache files via MCP"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MCP_SERVER_URL}/tools/cleanup_project",
                    json={}
                ) as resp:
                    return await resp.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}


# Local file operations (fallback if MCP not available)
class LocalFileOperations:
    """Direct local file operations"""
    
    @staticmethod
    def read_file(file_path: str) -> Dict[str, Any]:
        """Read file content locally"""
        try:
            path = _resolve_local_safe_path(file_path)
            if not path.exists():
                return {"status": "error", "message": f"File not found: {str(path)}"}
            if not path.is_file():
                return {"status": "error", "message": f"Not a file: {str(path)}"}

            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            return {
                "status": "success",
                "path": str(path),
                "content": content,
                "size": len(content)
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def write_file(file_path: str, content: str) -> Dict[str, Any]:
        """Write content to file locally"""
        try:
            path = _resolve_local_safe_path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            return {
                "status": "success",
                "message": f"File written: {str(path)}",
                "path": str(path),
                "size": len(content)
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def list_files(directory: str) -> Dict[str, Any]:
        """List files in directory locally"""
        try:
            path = _resolve_local_safe_path(directory)
            if not path.exists():
                return {"status": "error", "message": f"Directory not found: {str(path)}"}
            if not path.is_dir():
                return {"status": "error", "message": f"Not a directory: {str(path)}"}
            
            files = []
            for item in path.iterdir():
                files.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0
                })
            
            return {
                "status": "success",
                "path": str(path),
                "files": files,
                "count": len(files)
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def delete_file(file_path: str) -> Dict[str, Any]:
        """Delete file locally"""
        try:
            path = _resolve_local_safe_path(file_path)
            if not path.exists():
                return {"status": "error", "message": f"File not found: {str(path)}"}
            if not path.is_file():
                return {"status": "error", "message": f"Not a file: {str(path)}"}

            path.unlink()
            return {
                "status": "success",
                "message": f"File deleted: {str(path)}"
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def create_directory(dir_path: str) -> Dict[str, Any]:
        """Create directory locally"""
        try:
            path = _resolve_local_safe_path(dir_path)
            path.mkdir(parents=True, exist_ok=True)
            return {
                "status": "success",
                "message": f"Directory created: {str(path)}",
                "path": str(path)
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def copy_file(source: str, destination: str) -> Dict[str, Any]:
        """Copy file locally"""
        try:
            import shutil
            src_path = _resolve_local_safe_path(source)
            dst_path = _resolve_local_safe_path(destination)
            
            if not src_path.exists():
                return {"status": "error", "message": f"Source file not found: {source}"}
            if not src_path.is_file():
                return {"status": "error", "message": f"Not a file: {str(src_path)}"}
            
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            
            return {
                "status": "success",
                "message": f"File copied: {str(src_path)} -> {str(dst_path)}",
                "source": str(src_path),
                "destination": str(dst_path)
            }
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def cleanup_project() -> Dict[str, Any]:
        """Clean up project cache files locally"""
        try:
            import shutil
            patterns = [
                '__pycache__',
                '*.pyc',
                '.pytest_cache',
                '.coverage',
                '*.egg-info',
                '.DS_Store',
                'node_modules/.cache'
            ]
            
            deleted_count = 0
            for pattern in patterns:
                # Implementation of recursive pattern cleanup
                for root, dirs, files in os.walk('.'):
                    if pattern in dirs:
                        shutil.rmtree(os.path.join(root, pattern))
                        deleted_count += 1
                    for file in files:
                        if file.endswith('.pyc'):
                            os.remove(os.path.join(root, file))
                            deleted_count += 1
            
            return {
                "status": "success",
                "message": f"Cleaned up project",
                "items_deleted": deleted_count
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


# Use local operations as primary (more reliable)
file_ops = LocalFileOperations
