import os
from mcp.server.types import ToolResponse

def register_file_tools(server):

    @server.tool(
        name="read_file",
        description="Reads a file inside the Jarvis project directory"
    )
    async def read_file(path: str) -> ToolResponse:
        with open(path, "r") as f:
            return ToolResponse(content=f.read())

    @server.tool(
        name="write_file",
        description="Write content to a file (overwrite)"
    )
    async def write_file(path: str, content: str) -> ToolResponse:
        with open(path, "w") as f:
            f.write(content)
        return ToolResponse(content=f"File written: {path}")

    @server.tool(
        name="patch_file",
        description="Search and replace inside a file"
    )
    async def patch_file(path: str, search: str, replace: str) -> ToolResponse:
        with open(path, "r") as f:
            data = f.read()

        updated = data.replace(search, replace)

        with open(path, "w") as f:
            f.write(updated)

        return ToolResponse(content=f"Patched '{search}' → '{replace}' in {path}")
