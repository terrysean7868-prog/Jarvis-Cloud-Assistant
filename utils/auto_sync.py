import os
import subprocess
import logging

logger = logging.getLogger('jarvis.autosync')


def git_commit_and_push(file_path, commit_message):
    """
    Commit and push a file to the GitHub repository.
    
    Args:
        file_path: Path to the file to commit
        commit_message: Commit message
    
    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        # Get the repository root
        repo_root = os.path.dirname(os.path.dirname(__file__))
        
        # Configure git user if not already set
        try:
            subprocess.run(
                ['git', 'config', 'user.email'],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True
            )
        except subprocess.CalledProcessError:
            # Set default git user config
            subprocess.run(
                ['git', 'config', 'user.email', 'jarvis-bot@replit.app'],
                cwd=repo_root,
                check=True
            )
            subprocess.run(
                ['git', 'config', 'user.name', 'Jarvis Bot'],
                cwd=repo_root,
                check=True
            )
        
        # Add the file
        result = subprocess.run(
            ['git', 'add', file_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            logger.error(f"Git add failed: {result.stderr}")
            return False, f"Failed to add file: {result.stderr}"
        
        # Commit the file
        result = subprocess.run(
            ['git', 'commit', '-m', commit_message],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
                return True, "No changes to commit"
            logger.error(f"Git commit failed: {result.stderr}")
            return False, f"Failed to commit: {result.stderr}"
        
        # Push to remote
        result = subprocess.run(
            ['git', 'push'],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode != 0:
            logger.error(f"Git push failed: {result.stderr}")
            return False, f"Failed to push: {result.stderr}"
        
        logger.info(f"Successfully committed and pushed: {commit_message}")
        return True, "Successfully committed and pushed to GitHub"
        
    except subprocess.TimeoutExpired:
        return False, "Git operation timed out"
    except Exception as e:
        logger.exception(f"Auto-sync error: {e}")
        return False, f"Error: {str(e)}"


def get_git_status():
    """
    Get the current git status.
    
    Returns:
        tuple: (success: bool, status: str, error_message: str)
    """
    try:
        repo_root = os.path.dirname(os.path.dirname(__file__))
        
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            return False, None, f"Git status failed: {result.stderr}"
        
        return True, result.stdout, None
        
    except Exception as e:
        return False, None, f"Error getting git status: {str(e)}"
