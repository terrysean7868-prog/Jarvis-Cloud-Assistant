from subprocess import getoutput
from mcp.server.types import ToolResponse

def register_git_tools(server):

    @server.tool(name="git_commit", description="Git commit all changes")
    async def git_commit(msg: str) -> ToolResponse:
        output = getoutput(f"git add . && git commit -m '{msg}'")
        return ToolResponse(content=output)

    @server.tool(name="git_push", description="Git push changes to origin main")
    async def git_push() -> ToolResponse:
        output = getoutput("git push origin main")
        return ToolResponse(content=output)
