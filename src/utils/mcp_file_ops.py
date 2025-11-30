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
MCP_SERVER_HOST = os.getenv("MCP_SERVER_HOST", "localhost")
MCP_SERVER_PORT = int(os.getenv("MCP_SERVER_PORT", 9090))
MCP_SERVER_URL = f"http://{MCP_SERVER_HOST}:{MCP_SERVER_PORT}"


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
                    json={"path": directory}
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
            if not Path(file_path).exists():
                return {"status": "error", "message": f"File not found: {file_path}"}
            
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            return {
                "status": "success",
                "path": file_path,
                "content": content,
                "size": len(content)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def write_file(file_path: str, content: str) -> Dict[str, Any]:
        """Write content to file locally"""
        try:
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            return {
                "status": "success",
                "message": f"File written: {file_path}",
                "path": file_path,
                "size": len(content)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def list_files(directory: str) -> Dict[str, Any]:
        """List files in directory locally"""
        try:
            if not Path(directory).exists():
                return {"status": "error", "message": f"Directory not found: {directory}"}
            
            files = []
            for item in Path(directory).iterdir():
                files.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0
                })
            
            return {
                "status": "success",
                "path": directory,
                "files": files,
                "count": len(files)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def delete_file(file_path: str) -> Dict[str, Any]:
        """Delete file locally"""
        try:
            path = Path(file_path)
            if not path.exists():
                return {"status": "error", "message": f"File not found: {file_path}"}
            
            path.unlink()
            return {
                "status": "success",
                "message": f"File deleted: {file_path}"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def create_directory(dir_path: str) -> Dict[str, Any]:
        """Create directory locally"""
        try:
            Path(dir_path).mkdir(parents=True, exist_ok=True)
            return {
                "status": "success",
                "message": f"Directory created: {dir_path}",
                "path": dir_path
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    @staticmethod
    def copy_file(source: str, destination: str) -> Dict[str, Any]:
        """Copy file locally"""
        try:
            import shutil
            src_path = Path(source)
            dst_path = Path(destination)
            
            if not src_path.exists():
                return {"status": "error", "message": f"Source file not found: {source}"}
            
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            
            return {
                "status": "success",
                "message": f"File copied: {source} -> {destination}",
                "source": source,
                "destination": destination
            }
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
