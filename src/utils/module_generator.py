import os
from openai import OpenAI

# the newest OpenAI model is "gpt-5" which was released August 7, 2025.
# do not change this unless explicitly requested by the user

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)


def generate_module(module_name, description=None):
    """
    Generate a new Jarvis module using ChatGPT.
    
    Args:
        module_name: Name of the module to generate
        description: Optional description of what the module should do
    
    Returns:
        tuple: (success: bool, module_code: str, error_message: str)
    """
    try:
        if not OPENAI_API_KEY:
            return False, None, "OPENAI_API_KEY not configured"
        
        # Build the prompt for module generation
        prompt = f"""You are an expert Python developer creating a module for the Jarvis Telegram bot.

Generate a complete Python module file for a feature called "{module_name}".
{f'The module should: {description}' if description else ''}

Requirements:
1. The module MUST have a DESCRIPTION string at the top
2. The module MUST have a register(dp, services, scheduler) function
3. Use telegram.ext imports (CommandHandler, MessageHandler, Filters, etc.)
4. The register function should add handlers to the dispatcher (dp)
5. Available services: mongodb_uri, openweather, github_token, github_repo, default_lang
6. scheduler is an APScheduler BackgroundScheduler instance
7. Handle errors gracefully with try/except blocks
8. Reply to user with helpful messages
9. Follow the existing module patterns (note.py, reminder.py, search.py, weather.py, voice.py)

Example structure:
```python
DESCRIPTION = 'Brief description of what this module does'

def register(dp, services, scheduler):
    from telegram.ext import CommandHandler
    
    def command_handler(update, context):
        # Implementation here
        update.message.reply_text('Response')
    
    dp.add_handler(CommandHandler('commandname', command_handler))
```

Generate ONLY the Python code for the module, no explanations or markdown. Make it functional and production-ready."""

        response = openai_client.chat.completions.create(
            model="gpt-5",
            messages=[
                {"role": "system", "content": "You are an expert Python developer. Generate only clean, working Python code without any markdown formatting or explanations."},
                {"role": "user", "content": prompt}
            ],
            max_completion_tokens=2048
        )
        
        module_code = response.choices[0].message.content.strip()
        
        # Clean up any markdown formatting if present
        if module_code.startswith('```python'):
            module_code = module_code.split('```python')[1]
            module_code = module_code.rsplit('```', 1)[0]
        elif module_code.startswith('```'):
            module_code = module_code.split('```')[1]
            module_code = module_code.rsplit('```', 1)[0]
        
        module_code = module_code.strip()
        
        # Validate the module has required components
        if 'DESCRIPTION' not in module_code:
            return False, None, "Generated module missing DESCRIPTION variable"
        if 'def register(' not in module_code:
            return False, None, "Generated module missing register() function"
        
        return True, module_code, None
        
    except Exception as e:
        return False, None, f"Error generating module: {str(e)}"


def save_module(module_name, module_code):
    """
    Save generated module code to the modules directory.
    
    Args:
        module_name: Name of the module (without .py extension)
        module_code: Python code for the module
    
    Returns:
        tuple: (success: bool, file_path: str, error_message: str)
    """
    try:
        # Ensure module name is safe
        safe_name = module_name.replace(' ', '_').replace('-', '_')
        safe_name = ''.join(c for c in safe_name if c.isalnum() or c == '_')
        
        if not safe_name:
            return False, None, "Invalid module name"
        
        modules_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'modules')
        file_path = os.path.join(modules_dir, f"{safe_name}.py")
        
        # Check if module already exists
        if os.path.exists(file_path):
            return False, None, f"Module {safe_name} already exists"
        
        # Write the module file
        with open(file_path, 'w') as f:
            f.write(module_code)
        
        return True, file_path, None
        
    except Exception as e:
        return False, None, f"Error saving module: {str(e)}"
