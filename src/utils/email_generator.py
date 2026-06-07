# src/utils/email_generator.py
"""
Email generation and management for Jarvis
Generates emails based on voice commands and context
"""
import re
import json
from typing import Dict, Optional, List
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import logging

from ..config.secrets import llm_secrets
from ..config.settings import settings as jarvis_settings

def _self_hosted_model_config() -> tuple[str, str, Optional[str]]:
    endpoint = str(getattr(jarvis_settings, "self_hosted_llm_endpoint", "") or "").strip()
    if not endpoint:
        endpoint = "http://127.0.0.1:8010/v1/chat/completions"
    model = str(getattr(jarvis_settings, "self_hosted_llm_model", "") or "").strip()
    if not model:
        model = "Qwen/Qwen2.5-7B-Instruct"
    api_key = llm_secrets().self_hosted_api_key
    return endpoint, model, api_key


def _call_self_hosted_chat_completion(*, system_prompt: str, user_prompt: str) -> str:
    endpoint, model, api_key = _self_hosted_model_config()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=180) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        raise RuntimeError(f"Self-hosted model service error: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Self-hosted model service unavailable: {exc}") from exc

    try:
        data = json.loads(body)
    except Exception as exc:
        raise RuntimeError(f"Invalid self-hosted model response: {body[:500]}") from exc

    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except Exception as exc:
        raise RuntimeError(f"Unexpected self-hosted model response shape: {body[:500]}") from exc



class EmailGenerator:
    """Generate and manage emails"""
    
    def __init__(self):
        self.drafts = []
    
    def generate_email(self, 
                      recipient: str,
                      subject: Optional[str] = None,
                      body_prompt: str = "",
                      tone: str = "professional",
                      context: str = "") -> Dict:
        """
        Generate email based on voice command
        
        Args:
            recipient: Email recipient
            subject: Email subject (auto-generated if None)
            body_prompt: Description of what email should contain
            tone: Email tone (professional, casual, formal, friendly)
            context: Additional context for email generation
        """
        try:
            # Generate email content using AI
            system_prompt = f"""You are an expert email writer. Generate professional, clear, and concise emails.
Use correct grammar and spelling. Do NOT include typos.
Tone: {tone}
Format: Return JSON with 'subject' and 'body' fields.
Keep emails concise but complete."""
            
            user_prompt = f"""Generate an email with the following requirements:
Recipient: {recipient}
Subject: {subject if subject else 'Auto-generate based on content'}
Body content: {body_prompt}
Context: {context}
Tone: {tone}

Return JSON format:
{{
  "subject": "email subject here",
  "body": "email body here"
}}"""
            
            content = _call_self_hosted_chat_completion(system_prompt=system_prompt, user_prompt=user_prompt).strip()
            
            # Parse JSON response
            try:
                email_data = json.loads(content)
            except:
                # Extract JSON from markdown if present
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    email_data = json.loads(json_match.group())
                else:
                    # Fallback: create simple email
                    email_data = {
                        "subject": subject or "Email from Jarvis",
                        "body": content
                    }
            
            email = {
                "to": recipient,
                "subject": email_data.get("subject", subject or "Email from Jarvis"),
                "body": email_data.get("body", body_prompt),
                "tone": tone,
                "created_at": datetime.now().isoformat(),
                "status": "draft"
            }
            
            self.drafts.append(email)
            return {
                "status": "success",
                "email": email,
                "message": "Email generated successfully"
            }
            
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to generate email: {str(e)}"
            }
    
    def parse_email_command(self, command: str) -> Dict:
        """
        Parse voice command to extract email details
        
        Examples:
        - "Generate a mail for john@example.com about the meeting"
        - "Send email to jane@test.com with subject project update"
        - "Create email for boss about the report"
        """
        command_lower = command.lower()
        
        # Extract recipient
        recipient_patterns = [
            r"(?:to|for)\s+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
            r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
            r"(?:to|for)\s+(\w+)(?:\s+about|\s+with|\s+regarding|$)"
        ]
        
        recipient = None
        for pattern in recipient_patterns:
            match = re.search(pattern, command_lower)
            if match:
                recipient = match.group(1)
                if "@" in recipient:
                    break
                # If no email found, it might be a name - would need contact lookup
                break
        
        # Extract subject
        subject_patterns = [
            r"subject[:\s]+([^,]+?)(?:,|about|with|$)",
            r"about[:\s]+([^,]+?)(?:,|with|$)",
            r"regarding[:\s]+([^,]+?)(?:,|with|$)"
        ]
        
        subject = None
        for pattern in subject_patterns:
            match = re.search(pattern, command_lower)
            if match:
                subject = match.group(1).strip()
                break
        
        # Extract body/content
        body_keywords = ["about", "regarding", "saying", "mentioning", "with content"]
        body = ""
        for keyword in body_keywords:
            if keyword in command_lower:
                idx = command_lower.find(keyword)
                body = command_lower[idx + len(keyword):].strip()
                # Remove recipient and subject from body
                if recipient and recipient in body:
                    body = body.replace(recipient, "").strip()
                if subject and subject in body:
                    body = body.replace(subject, "").strip()
                break
        
        # If no body found, use the whole command as context
        if not body:
            body = command
        
        # Determine tone
        tone = "professional"
        if any(word in command_lower for word in ["casual", "informal", "friendly"]):
            tone = "casual"
        elif any(word in command_lower for word in ["formal", "official"]):
            tone = "formal"
        
        return {
            "recipient": recipient or "unknown@example.com",
            "subject": subject,
            "body_prompt": body,
            "tone": tone
        }
    
    def generate_from_command(self, command: str, context: str = "") -> Dict:
        """Generate email from voice command"""
        parsed = self.parse_email_command(command)
        return self.generate_email(
            recipient=parsed["recipient"],
            subject=parsed["subject"],
            body_prompt=parsed["body_prompt"],
            tone=parsed["tone"],
            context=context
        )
    
    def get_drafts(self) -> List[Dict]:
        """Get all email drafts"""
        return self.drafts
    
    def format_email_for_display(self, email: Dict) -> str:
        """Format email for display"""
        return f"""
To: {email['to']}
Subject: {email['subject']}

{email['body']}
"""


# Global instance
email_generator = EmailGenerator()

logger = logging.getLogger("jarvis.email_generator")

