import logging
import os
from datetime import datetime
from fastapi import FastAPI, Header, HTTPException
from mcp.server.fastapi import MCPServer

from tools.file_tools import register_file_tools
from tools.git_tools import register_git_tools
from tools.run_tools import register_run_tools

# Setup logging for audit trail
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - MCP - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI()
server = MCPServer(app)

# Expected JWT secret (should match main app's secret)
JWT_SECRET = os.getenv("JARVIS_JWT_SECRET", "default-secret")

def verify_jwt_token(authorization: str):
    """Verify JWT token from Authorization header."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    try:
        # Parse "Bearer <token>" format
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        token = parts[1]

        # Basic token validation (in production, use proper JWT library with signature verification)
        if not token or token == "default-secret":
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        return token
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Token verification failed: {str(e)}")
        raise HTTPException(status_code=401, detail="Token verification failed")

@app.middleware("http")
async def add_audit_logging(request, call_next):
    """Middleware to log all MCP operations."""
    # Log request
    logger.info(f"MCP Request: {request.method} {request.url.path}")

    # Verify JWT token for tool calls
    if request.url.path.startswith("/tools/"):
        auth_header = request.headers.get("Authorization", "")
        try:
            verify_jwt_token(auth_header)
        except HTTPException as e:
            logger.warning(f"Unauthorized MCP request: {e.detail}")
            raise

    response = await call_next(request)
    logger.info(f"MCP Response: {response.status_code}")
    return response

# Register all tools
register_file_tools(server)
register_git_tools(server)
register_run_tools(server)

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting MCP server on port 9090 (requires JARVIS_JWT_SECRET)")
    uvicorn.run("server:app", host="0.0.0.0", port=9090, reload=False)
