# src/utils/error_handler.py
"""
Automatic Error Handling and Bot Self-Healing
Detects errors, analyzes logs, and fixes issues automatically
"""
import os
import re
import subprocess
import logging
from typing import Dict, List, Optional
from pathlib import Path
from datetime import datetime
import requests

from src.config.secrets import render_secrets

logger = logging.getLogger("jarvis.error_handler")


class ErrorHandler:
    """Automatic error detection and fixing"""
    
    def __init__(self):
        self.error_logs = []
        self.fix_history = []
        s = render_secrets()
        self.render_api_key = s.api_key
        self.render_service_id = s.service_id
    
    def check_render_logs(self) -> Dict:
        """Fetch and analyze Render logs"""
        if not self.render_api_key or not self.render_service_id:
            return {
                "status": "error",
                "message": "Render API credentials not configured"
            }
        
        try:
            headers = {
                "Authorization": f"Bearer {self.render_api_key}",
                "Accept": "application/json"
            }
            
            # Fetch recent logs
            url = f"https://api.render.com/v1/services/{self.render_service_id}/logs"
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                logs = response.json()
                errors = self._analyze_logs(logs)
                return {
                    "status": "success",
                    "logs": logs,
                    "errors": errors,
                    "fixes": self._suggest_fixes(errors)
                }
            else:
                return {
                    "status": "error",
                    "message": f"Failed to fetch logs: {response.status_code}"
                }
        except Exception as e:
            logger.error(f"Error fetching Render logs: {e}")
            return {
                "status": "error",
                "message": str(e)
            }
    
    def _analyze_logs(self, logs: List[str]) -> List[Dict]:
        """Analyze logs for errors"""
        errors = []
        error_patterns = [
            (r"Error|ERROR", "general_error"),
            (r"Exception|Traceback", "exception"),
            (r"Failed|FAILED", "failure"),
            (r"Timeout|TIMEOUT", "timeout"),
            (r"Connection.*refused|Connection.*error", "connection_error"),
            (r"ModuleNotFoundError|ImportError", "import_error"),
            (r"AttributeError", "attribute_error"),
            (r"KeyError|IndexError", "key_error"),
            (r"Permission denied|PermissionDenied", "permission_error"),
            (r"FileNotFoundError|File not found", "file_not_found"),
        ]
        
        if isinstance(logs, dict) and "logs" in logs:
            log_text = "\n".join(logs["logs"])
        elif isinstance(logs, list):
            log_text = "\n".join(logs)
        else:
            log_text = str(logs)
        
        for pattern, error_type in error_patterns:
            matches = re.finditer(pattern, log_text, re.IGNORECASE)
            for match in matches:
                # Extract context around error
                start = max(0, match.start() - 200)
                end = min(len(log_text), match.end() + 200)
                context = log_text[start:end]
                
                errors.append({
                    "type": error_type,
                    "message": match.group(),
                    "context": context,
                    "position": match.start()
                })
        
        return errors
    
    def _suggest_fixes(self, errors: List[Dict]) -> List[Dict]:
        """Suggest fixes for errors"""
        fixes = []
        
        for error in errors:
            error_type = error["type"]
            fix = {
                "error_type": error_type,
                "suggested_fix": "",
                "commands": []
            }
            
            if error_type == "import_error":
                # Extract module name
                module_match = re.search(r"'(.*?)'", error["context"])
                if module_match:
                    module_name = module_match.group(1)
                    fix["suggested_fix"] = f"Install missing module: {module_name}"
                    fix["commands"] = [f"pip install {module_name}"]
            
            elif error_type == "connection_error":
                fix["suggested_fix"] = "Check network connection and service availability"
                fix["commands"] = ["ping -c 4 8.8.8.8"]
            
            elif error_type == "permission_error":
                fix["suggested_fix"] = "Fix file permissions"
                fix["commands"] = ["chmod +x script.py"]  # Platform-specific
            
            elif error_type == "file_not_found":
                # Extract filename
                file_match = re.search(r"'(.*?)'", error["context"])
                if file_match:
                    filename = file_match.group(1)
                    fix["suggested_fix"] = f"Create or locate file: {filename}"
                    fix["commands"] = [f"touch {filename}"]  # Platform-specific
            
            elif error_type == "timeout":
                fix["suggested_fix"] = "Increase timeout or check service response"
                fix["commands"] = []
            
            if fix["suggested_fix"]:
                fixes.append(fix)
        
        return fixes
    
    def auto_fix_error(self, error: Dict) -> Dict:
        """Automatically fix an error"""
        fixes = self._suggest_fixes([error])
        
        if not fixes:
            return {
                "status": "error",
                "message": "No automatic fix available"
            }
        
        fix = fixes[0]
        results = []
        
        for command in fix["commands"]:
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                results.append({
                    "command": command,
                    "success": result.returncode == 0,
                    "output": result.stdout,
                    "error": result.stderr
                })
            except Exception as e:
                results.append({
                    "command": command,
                    "success": False,
                    "error": str(e)
                })
        
        self.fix_history.append({
            "error": error,
            "fix": fix,
            "results": results,
            "timestamp": datetime.now().isoformat()
        })
        
        return {
            "status": "success",
            "fix": fix,
            "results": results
        }
    
    def check_local_logs(self, log_file: Optional[str] = None) -> Dict:
        """Check local log files for errors"""
        if not log_file:
            # Check common log locations
            log_files = [
                "logs/jarvis.log",
                "jarvis.log",
                "error.log",
                "app.log"
            ]
        else:
            log_files = [log_file]
        
        errors = []
        for log_path in log_files:
            log_file_path = Path(log_path)
            if log_file_path.exists():
                try:
                    with open(log_file_path, 'r') as f:
                        log_content = f.read()
                        errors.extend(self._analyze_logs([log_content]))
                except Exception as e:
                    logger.error(f"Error reading log file {log_path}: {e}")
        
        return {
            "status": "success",
            "errors": errors,
            "fixes": self._suggest_fixes(errors)
        }
    
    def monitor_and_fix(self) -> Dict:
        """Monitor for errors and automatically fix them"""
        # Check Render logs
        render_result = self.check_render_logs()
        
        # Check local logs
        local_result = self.check_local_logs()
        
        all_errors = []
        if render_result.get("status") == "success":
            all_errors.extend(render_result.get("errors", []))
        if local_result.get("status") == "success":
            all_errors.extend(local_result.get("errors", []))
        
        fixes_applied = []
        for error in all_errors:
            fix_result = self.auto_fix_error(error)
            if fix_result.get("status") == "success":
                fixes_applied.append(fix_result)
        
        return {
            "status": "success",
            "errors_found": len(all_errors),
            "fixes_applied": len(fixes_applied),
            "details": fixes_applied
        }


# Global instance
error_handler = ErrorHandler()

