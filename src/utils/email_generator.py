# src/utils/email_generator.py
"""
Email generation and management for Jarvis
Generates emails based on voice commands and context
"""
import os
import re
from typing import Dict, Optional, List
from datetime import datetime
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY") or os.getenv("PRIMARY_API_KEY"))


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
            
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=500
            )
            
            content = response.choices[0].message.content.strip()
            
            # Parse JSON response
            import json
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

