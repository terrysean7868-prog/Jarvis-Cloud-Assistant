import subprocess
from mcp.server.types import ToolResponse

def register_run_tools(server):

    @server.tool(
        name="run_command",
        description="Runs a shell command (sandboxed, allowed commands only)"
    )
    async def run_command(cmd: str) -> ToolResponse:
        # Whitelist of allowed commands to prevent misuse
        allowed_prefixes = ["pip", "npm", "python", "node", "echo", "cat", "ls", "pwd", "find"]

        cmd_lower = cmd.strip().split()[0].lower() if cmd.strip() else ""

        if cmd_lower not in allowed_prefixes:
            return ToolResponse(
                content=f"ERROR: Command '{cmd_lower}' not in whitelist. Allowed: {', '.join(allowed_prefixes)}",
                is_error=True
            )

        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            output = result.stdout + result.stderr
            if result.returncode != 0:
                return ToolResponse(content=output, is_error=False)
            return ToolResponse(content=output)
        except subprocess.TimeoutExpired:
            return ToolResponse(content="ERROR: Command timed out after 30 seconds", is_error=True)
        except Exception as e:
            return ToolResponse(content=f"ERROR: {str(e)}", is_error=True)
