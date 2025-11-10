"""
Training data for JARVIS bot.
Provides intent examples, conversational patterns, and response templates
to make the bot more human-like and contextually aware.
"""

TRAINING_INTENTS = {
    "greeting": {
        "examples": [
            "hello",
            "hi",
            "hey",
            "good morning",
            "good afternoon",
            "good evening",
            "hey jarvis",
            "jarvis are you there",
            "wake up jarvis",
        ],
        "responses": [
            "Good morning, sir. How may I assist you today?",
            "Hello! Ready to help with whatever you need.",
            "At your service. What can I do for you?",
            "Hey there! What's on your mind?",
            "Greetings. How can I be of assistance?",
        ]
    },
    "how_are_you": {
        "examples": [
            "how are you",
            "how are you doing",
            "how's it going",
            "what's up",
            "how you doing",
            "you okay",
            "you alright",
        ],
        "responses": [
            "I'm functioning at optimal levels, thank you for asking. How about yourself?",
            "All systems nominal, sir. Ready to assist whenever needed.",
            "Doing great! How can I help you today?",
            "I'm always ready to help. What do you need?",
            "Everything's working perfectly. What can I do for you?",
        ]
    },
    "thanks": {
        "examples": [
            "thank you",
            "thanks",
            "thanks jarvis",
            "appreciate it",
            "much appreciated",
            "thanks a lot",
            "thank you very much",
            "gracias",
        ],
        "responses": [
            "You're welcome, sir.",
            "Always happy to help.",
            "My pleasure. Anything else?",
            "Glad I could assist.",
            "Happy to be of service.",
        ]
    },
    "open_url": {
        "examples": [
            "open youtube",
            "open linkedin",
            "open google",
            "open github",
            "open facebook",
            "open twitter",
            "open instagram",
            "open reddit",
            "open wikipedia",
            "open gmail",
            "open netflix",
            "open amazon",
            "show me youtube",
            "go to linkedin",
            "take me to reddit",
        ],
        "responses": [
            "Opening {url} for you now.",
            "Let me get that for you.",
            "Pulling up {url}.",
            "One moment, loading {url}.",
            "Opening {url} in your browser.",
        ]
    },
    "search": {
        "examples": [
            "search for",
            "google",
            "find",
            "look up",
            "search",
            "find information about",
            "tell me about",
        ],
        "responses": [
            "Searching for {query} now.",
            "Let me find that for you.",
            "Looking that up.",
            "I'll search for {query}.",
            "Let me fetch some information on {query}.",
        ]
    },
    "help": {
        "examples": [
            "help",
            "what can you do",
            "what are you capable of",
            "what can you help with",
            "show me commands",
            "how do i use you",
            "what can i ask you",
        ],
        "responses": [
            "I can help you with various tasks like opening websites, searching the web, and much more. What would you like me to do?",
            "I'm here to assist with browsing, searching, and executing various commands. How can I help?",
            "I can open URLs, search the web, and perform various actions. What do you need?",
            "There's quite a lot I can do. You can ask me to open websites, search for information, or help with other tasks.",
        ]
    },
    "time": {
        "examples": [
            "what time is it",
            "what's the time",
            "current time",
            "what time",
            "tell me the time",
            "what's the current time",
        ],
        "responses": [
            "It's currently {time}.",
            "The time is {time}.",
            "Right now it's {time}.",
            "Let me check... it's {time}.",
        ]
    },
    "joke": {
        "examples": [
            "tell me a joke",
            "make me laugh",
            "say something funny",
            "tell a joke",
            "got any jokes",
        ],
        "responses": [
            "Why did the AI go to school? To improve its neural network! 😄",
            "I tried to tell a programming joke, but there were no laughs in the output.",
            "Why do programmers prefer dark mode? Because light attracts bugs!",
            "I'd tell you a UDP joke, but you might not get it.",
            "Why did the developer go broke? Because he used up all his cache!",
        ]
    },
    "bye": {
        "examples": [
            "goodbye",
            "bye",
            "see you",
            "see you later",
            "talk to you later",
            "gotta go",
            "signing off",
            "farewell",
        ],
        "responses": [
            "Goodbye, sir. Have a great day!",
            "See you next time. Take care!",
            "Until next time. Goodbye!",
            "Catch you later!",
            "Signing off. Have a wonderful day!",
        ]
    },
    "unknown": {
        "examples": [],
        "responses": [
            "I'm not quite sure what you mean. Could you clarify that?",
            "Interesting. Can you tell me more about that?",
            "I'm still learning. Could you rephrase that?",
            "That's a bit beyond my current capabilities, but I'm always learning.",
            "I didn't quite understand that. Can you try again?",
        ]
    }
}

CONVERSATIONAL_PATTERNS = [
    {
        "pattern": "Can you {action}?",
        "response": "I'll {action} for you right away."
    },
    {
        "pattern": "Please {action}",
        "response": "Certainly. {action} coming up."
    },
    {
        "pattern": "I need to {action}",
        "response": "Let me help you {action}."
    },
    {
        "pattern": "Do you know {topic}?",
        "response": "Let me search for information about {topic}."
    },
]

CONTEXTUAL_RESPONSES = {
    "office_context": [
        "Excellent. Let me update that for you.",
        "Right away, sir.",
        "Updating now.",
        "Noted. Processing your request.",
    ],
    "casual_context": [
        "Sure thing!",
        "You got it!",
        "No problem.",
        "Coming right up!",
    ],
    "technical_context": [
        "Initializing process.",
        "Executing command.",
        "Processing request.",
        "Analyzing parameters.",
    ]
}

PERSONALITY_TRAITS = {
    "formality": 0.7,  # 0 = casual, 1 = very formal
    "helpfulness": 0.95,  # How proactive in offering help
    "humor": 0.4,  # Likelihood to use humor
    "confidence": 0.85,  # Confidence in responses
}

def get_intent_examples(intent: str) -> list:
    """Get example phrases for a given intent."""
    return TRAINING_INTENTS.get(intent, {}).get("examples", [])

def get_intent_responses(intent: str) -> list:
    """Get response templates for a given intent."""
    return TRAINING_INTENTS.get(intent, {}).get("responses", [])

def get_all_intents() -> dict:
    """Get all training intents."""
    return TRAINING_INTENTS

def get_personality() -> dict:
    """Get personality configuration."""
    return PERSONALITY_TRAITS
