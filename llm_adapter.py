# llm_adapter.py
import os, re, json
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Provider selection: "openai", "gemini", or "auto" (try OpenAI first, fallback to Gemini)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto").lower()

# Lazy imports
_openai_client = None
_gemini_client = None

if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except ImportError:
        try:
            # Fallback for older openai versions
            import openai as _openai_old
            _openai_old.api_key = OPENAI_API_KEY
            _openai_client = _openai_old
        except Exception:
            _openai_client = None
    except Exception:
        _openai_client = None

if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        _gemini_client = genai.GenerativeModel(model_name)
    except ImportError:
        print("Warning: google-generativeai not installed. Install with: pip install google-generativeai")
        _gemini_client = None
    except Exception as e:
        print(f"Warning: Failed to initialize Gemini: {e}")
        _gemini_client = None

class LLMAdapter:
    def __init__(self):
        self.openai_client = _openai_client
        self.gemini_client = _gemini_client
        self.provider = LLM_PROVIDER

    async def generate(self, prompt: str, system: str = None, max_tokens: int = 2048):
        """
        Returns dict: {'text': str}
        Models should be prompted to append a JSON object with 'actions' if any file edits are proposed.
        Supports both OpenAI and Gemini APIs.
        """
        provider_to_use = self.provider
        
        # Auto mode: try OpenAI first, then Gemini
        if provider_to_use == "auto":
            if self.openai_client:
                provider_to_use = "openai"
            elif self.gemini_client:
                provider_to_use = "gemini"
            else:
                return {"text": "Error: No LLM provider configured. Set OPENAI_API_KEY or GEMINI_API_KEY."}

        # Try OpenAI
        if provider_to_use == "openai" and self.openai_client:
            try:
                # Modern OpenAI SDK (v1.0+)
                if hasattr(self.openai_client, 'chat') and hasattr(self.openai_client.chat, 'completions'):
                    response = self.openai_client.chat.completions.create(
                        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                        messages=[
                            {"role": "system", "content": system or "You are Jarvis, an AI assistant that can propose file actions in JSON."},
                            {"role": "user", "content": prompt}
                        ],
                        max_tokens=max_tokens,
                        temperature=float(os.getenv("OPENAI_TEMP", "0.7"))
                    )
                    text = response.choices[0].message.content
                else:
                    # Legacy OpenAI SDK (<1.0)
                    response = self.openai_client.ChatCompletion.create(
                        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                        messages=[
                            {"role": "system", "content": system or "You are Jarvis, an AI assistant that can propose file actions in JSON."},
                            {"role": "user", "content": prompt}
                        ],
                        max_tokens=max_tokens,
                        temperature=float(os.getenv("OPENAI_TEMP", "0.7"))
                    )
                    text = response["choices"][0]["message"]["content"]
                return {"text": text}
            except Exception as e:
                # Fallback to Gemini if OpenAI fails and auto mode
                if self.provider == "auto" and self.gemini_client:
                    provider_to_use = "gemini"
                else:
                    return {"text": f"OpenAI error: {e}"}

        # Try Gemini
        if provider_to_use == "gemini" and self.gemini_client:
            try:
                # Build messages for Gemini
                full_prompt = f"{system or 'You are Jarvis, an AI assistant that can propose file actions in JSON.'}\n\nUser: {prompt}"
                response = self.gemini_client.generate_content(full_prompt)
                # Gemini response.text might need to be accessed differently
                if hasattr(response, 'text'):
                    text = response.text
                elif hasattr(response, 'candidates') and response.candidates:
                    text = response.candidates[0].content.parts[0].text
                else:
                    text = str(response)
                return {"text": text}
            except Exception as e:
                return {"text": f"Gemini error: {e}"}

        # Fallback
        return {"text": f"Error: No available LLM provider. OpenAI: {self.openai_client is not None}, Gemini: {self.gemini_client is not None}"}

    def parse_actions_from_text(self, text: str):
        """
        Extract JSON object from model text and return actions array if present.
        Supports both inline JSON and code blocks.
        """
        try:
            # Try to find JSON in code blocks first
            code_block_pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
            code_match = re.search(code_block_pattern, text, re.DOTALL)
            if code_match:
                obj = json.loads(code_match.group(1))
                return obj.get("actions", [])
            
            # Try to find JSON object in text
            json_pattern = r"\{[^{}]*\"actions\"[^{}]*\[.*?\]\s*\}"
            # More robust: find balanced braces
            brace_count = 0
            start_idx = -1
            for i, char in enumerate(text):
                if char == '{':
                    if brace_count == 0:
                        start_idx = i
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0 and start_idx != -1:
                        try:
                            obj = json.loads(text[start_idx:i+1])
                            if "actions" in obj:
                                return obj.get("actions", [])
                        except:
                            pass
            
            # Last resort: try simple regex
            m = re.search(r"(\{[^{}]*\"actions\"[^{}]*\})", text, re.S)
            if m:
                obj = json.loads(m.group(1))
                return obj.get("actions", [])
            
            return []
        except Exception as e:
            # Silently fail and return empty actions
            return []
