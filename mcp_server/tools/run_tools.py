import subprocess
from mcp.server.types import ToolResponse

def register_run_tools(server):

    @server.tool(
        name="run_command",
        description="Runs a shell command (sandboxed)"
    )
    async def run_command(cmd: str) -> ToolResponse:
        out = subprocess.getoutput(cmd)
        return ToolResponse(content=out)
