import subprocess
from mcp.server.types import ToolResponse

def register_git_tools(server):

    @server.tool(name="git_commit", description="Git commit all changes")
    async def git_commit(msg: str) -> ToolResponse:
        try:
            subprocess.run(["git", "add", "."], check=True, capture_output=True, text=True)
            result = subprocess.run(["git", "commit", "-m", msg], capture_output=True, text=True)
            return ToolResponse(content=result.stdout + result.stderr)
        except subprocess.CalledProcessError as e:
            return ToolResponse(content=f"ERROR: {e.stderr}", is_error=True)
        except Exception as e:
            return ToolResponse(content=f"ERROR: {str(e)}", is_error=True)

    @server.tool(name="git_push", description="Git push changes to origin main")
    async def git_push() -> ToolResponse:
        try:
            result = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True)
            if result.returncode != 0:
                return ToolResponse(content=f"ERROR: {result.stderr}", is_error=True)
            return ToolResponse(content=result.stdout)
        except Exception as e:
            return ToolResponse(content=f"ERROR: {str(e)}", is_error=True)
