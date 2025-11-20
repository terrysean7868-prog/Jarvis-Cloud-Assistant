from fastapi import FastAPI
from mcp.server.fastapi import MCPServer

from tools.file_tools import register_file_tools
from tools.git_tools import register_git_tools
from tools.run_tools import register_run_tools

app = FastAPI()
server = MCPServer(app)

# Register all tools
register_file_tools(server)
register_git_tools(server)
register_run_tools(server)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=9090, reload=False)
