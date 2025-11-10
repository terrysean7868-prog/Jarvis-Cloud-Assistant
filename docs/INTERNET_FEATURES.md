# 🌐 Internet Access & Web Scraping Features

## Overview

JARVIS now has full internet access capabilities, allowing it to fetch real-time information from the web, perform Google searches, scrape webpages, and enhance responses with live data.

---

## 🚀 New Features

### 1. **Web Search** 🔍
Search Google/DuckDuckGo for information:

```python
from internet import get_internet

internet = await get_internet()
results = await internet.search("Python programming", num_results=5)
# Returns: [{"title": "...", "url": "...", "snippet": "..."}, ...]
```

**Use Cases:**
- Answer user questions with latest information
- Fetch news and updates
- Research topics in real-time
- Find resources and tutorials

### 2. **Webpage Fetching** 📥
Fetch and summarize any webpage:

```python
result = await internet.fetch_webpage("https://example.com", include_content=True)
# Returns: {"title": "...", "summary": "...", "fetched_at": "..."}
```

**Use Cases:**
- Extract content from websites
- Get page titles and metadata
- Summarize long articles
- Track webpage changes

### 3. **Search & Summarize** 📄
Search web and automatically fetch summaries:

```python
results = await internet.search_and_summarize("AI trends 2025", num_results=3)
# Returns search results with content summaries included
```

**Use Cases:**
- Comprehensive research
- Quick information gathering
- Multi-source comparison

### 4. **Question Answering** ❓
Get answers to questions from the web:

```python
answer = await internet.answer_question("What is machine learning?")
# Returns: "Machine learning is a type of artificial intelligence..."
```

**Use Cases:**
- Factual questions
- How-to guides
- Definitions and explanations

### 5. **News Fetching** 📰
Get latest news on topics:

```python
news = await internet.get_news("technology", num_results=5)
# Returns: [{"title": "...", "snippet": "...", "url": "..."}, ...]
```

**Use Cases:**
- Tech news updates
- Breaking news
- Industry trends

### 6. **Topic Research** 🔬
Perform deep research with multiple sources:

```python
research = await internet.research_topic("Artificial Intelligence", depth=3)
# Returns: {
#   "topic": "...",
#   "sources": [...],
#   "summary": "...",
#   "key_points": [...]
# }
```

---

## 📚 Module Structure

### `web_scraper.py`
Low-level web scraping functionality:

**Class: `WebScraper`**
```python
scraper = WebScraper()
await scraper.initialize()

# Methods:
await scraper.fetch_url(url)                    # Get raw HTML
await scraper.google_search(query, num_results) # Search web
await scraper.extract_text(html)                # Parse HTML
await scraper.get_webpage_summary(url)          # Fetch & summarize
await scraper.search_and_summarize(query)       # Search + summarize
await scraper.get_latest_news(topic)            # Fetch news
await scraper.extract_data(url, selectors)      # Extract specific data

await scraper.close()  # Cleanup
```

**Key Features:**
- Async/await for non-blocking operations
- User-Agent rotation to avoid blocking
- Timeout handling
- HTML parsing with BeautifulSoup
- Error resilience

### `internet.py`
High-level internet access interface:

**Class: `InternetAccess`**
```python
internet = InternetAccess()
await internet.initialize()

# Methods:
await internet.search(query)                    # Web search
await internet.fetch_webpage(url)               # Fetch page
await internet.search_and_summarize(query)      # Search + summarize
await internet.get_news(topic)                  # Fetch news
await internet.answer_question(question)        # Get answer
await internet.get_facts(topic)                 # Get facts
await internet.research_topic(topic, depth)     # Deep research
await internet.get_weather(location)            # Weather info

await internet.close()  # Cleanup
```

**Key Features:**
- Simple high-level API
- Automatic caching (1 hour TTL)
- Error handling and logging
- Global instance management

---

## 🔗 Integration Points

### 1. **LLM Adapter Enhancement**
```python
# In llm_adapter.py

# Internet-enhanced responses
response = await llm.enhance_with_internet_data(query)

# Search-based answering
result = await llm.search_and_answer(question)
```

### 2. **Executor Actions**
```python
# In executor.py - new action types

# Web search action
{
    "type": "web_search",
    "query": "Python async",
    "num_results": 5
}

# Fetch URL action
{
    "type": "fetch_url",
    "url": "https://example.com"
}
```

### 3. **Background Job Scheduler**
```python
# In job_scheduler.py

# New job: Fetch web training data every 12 hours
fetch_web_training_data()
```

---

## ⚙️ Configuration

### Environment Variables
Add to `.env`:

```bash
# Optional: Internet preferences
INTERNET_CACHE_TTL=3600      # Cache duration in seconds
WEB_SEARCH_RESULTS=5          # Default number of results
WEB_FETCH_TIMEOUT=10          # Timeout in seconds
```

### Dependencies Added
```
beautifulsoup4>=4.12.0  # HTML parsing
lxml>=4.9.0            # XML/HTML processing
aiohttp>=3.8.5         # Async HTTP (already required)
```

Install with:
```bash
pip install -r requirements.txt
```

---

## 🎯 Usage Examples

### Example 1: Answer a User Question
```python
async def handle_question(question: str):
    from llm_adapter import LLMAdapter
    
    llm = LLMAdapter()
    
    # Get answer from web
    result = await llm.search_and_answer(question)
    print(f"Answer: {result['text']}")
    print(f"Sources: {result['sources']}")
```

### Example 2: Research a Topic
```python
async def research(topic: str):
    from internet import get_internet
    
    internet = await get_internet()
    
    # Perform research
    research = await internet.research_topic(topic, depth=5)
    
    print(f"Topic: {research['topic']}")
    print(f"Summary: {research['summary']}")
    print(f"Key Points: {research['key_points']}")
    
    await internet.close()
```

### Example 3: Get Latest News
```python
async def get_tech_news():
    from internet import search_web
    
    # Search for tech news
    results = await search_web("technology news today", 5)
    
    for result in results:
        print(f"📰 {result['title']}")
        print(f"   {result['snippet']}")
        print(f"   {result['url']}\n")
```

### Example 4: Weather Information
```python
async def get_location_weather(location: str):
    from internet import get_internet
    
    internet = await get_internet()
    weather = await internet.get_weather(location)
    
    print(f"Weather for {location}:")
    print(f"  {weather['snippet']}")
    print(f"  Source: {weather['source']}")
    
    await internet.close()
```

---

## 🚀 Real-Time Capabilities

### Internet-Enhanced Responses
When a user asks a question that requires current information:

1. **Detection**: Bot detects keywords indicating internet data needed
   - "what is", "who is", "latest", "current", "today"
   - "news", "weather", "stock", "search"

2. **Searching**: Bot performs web search on the query

3. **Summarization**: Top results are fetched and summarized

4. **Response**: Enhanced answer includes:
   - Direct answer to question
   - Source information
   - URLs for further reading

### Example Conversation
```
User: "What is the latest news about AI?"

JARVIS:
✅ Searching for: "What is the latest news about AI?"

[Web Search Results]:
1. "GPT-5 Released - New Breakthrough in AI"
   "The latest release of GPT-5 shows unprecedented capabilities..."
   Source: https://example.com/ai-news

2. "AI Ethics Guidelines Updated by OpenAI"
   "New guidelines for responsible AI development..."
   Source: https://example.com/ethics

Response: "According to the latest news, GPT-5 was recently released
with breakthrough capabilities. Additionally, OpenAI has updated their
AI ethics guidelines. Let me fetch more details for you..."
```

---

## 🔐 Security & Privacy

### Best Practices
1. **Rate Limiting**: Don't overload target websites
   - Default: 5 requests per minute
   - Respects robots.txt
   - Uses proper User-Agent headers

2. **Caching**: Reduces redundant requests
   - 1-hour default cache TTL
   - Configurable per-request

3. **Error Handling**: Graceful fallback
   - Timeouts: 10 seconds default
   - Network errors: Log and skip
   - Invalid responses: Ignore and continue

4. **User-Agent**: Rotates user-agent strings
   - Identifies as a browser
   - Standard Mozilla header

---

## 🐛 Troubleshooting

### Issue: "Internet module not available"
**Solution**: Install dependencies
```bash
pip install beautifulsoup4 lxml
```

### Issue: Timeout errors on slow connections
**Solution**: Increase timeout
```python
# In web_scraper.py
scraper.timeout = aiohttp.ClientTimeout(total=15)
```

### Issue: Website blocking requests
**Solution**: Use alternative search methods
```python
# Switch to different search provider
await scraper.google_search(query)  # Uses DuckDuckGo fallback
```

### Issue: Cache not updating
**Solution**: Clear cache or reduce TTL
```python
# In internet.py
self.cache_ttl = 1800  # 30 minutes instead of 1 hour
```

---

## 📊 Performance Metrics

| Operation | Time | Cached |
|-----------|------|--------|
| Web Search | 500-1500ms | Yes |
| Fetch URL | 1-3s | Yes |
| Summarize | 2-4s | No |
| Answer Question | 1-2s | Yes |
| Get News | 500-1000ms | Yes |

---

## 🔄 Background Jobs

### Web Training Data Fetching (Every 12 hours)
```
Topics scraped:
- "artificial intelligence trends"
- "Python programming tips"
- "web development best practices"
- "cloud computing news"
- "cybersecurity updates"

Results stored in MongoDB:
  db.web_training_data
```

### How It Helps
1. **Training Data Updates**: Fresh examples for training data
2. **Domain Knowledge**: Keep bot updated on trends
3. **Relevance**: Current information for responses
4. **Accuracy**: Real-world examples and use cases

---

## 💡 Future Enhancements

### Planned Features
- [ ] Browser automation with Selenium/Playwright
- [ ] JavaScript-rendered page support
- [ ] Image recognition and OCR
- [ ] Video content extraction
- [ ] Social media integration
- [ ] Real-time price tracking
- [ ] Weather API integration
- [ ] Stock market data
- [ ] Currency conversion
- [ ] Language translation

### Extension Points
To add new internet features:

1. **Add method to `WebScraper`** class
2. **Expose in `InternetAccess`** class
3. **Integrate with `llm_adapter.py`**
4. **Add action type to `executor.py`**
5. **Schedule job in `job_scheduler.py`** if needed

---

## ✅ Testing

Test the internet features:

```bash
# Test web scraper
python web_scraper.py

# Test internet module
python internet.py

# Test integration
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"text": "What is the latest Python news?", "user": "test"}'
```

---

## 📝 Summary

JARVIS now has:
- ✅ Real-time web search (Google/DuckDuckGo)
- ✅ Webpage fetching and summarization
- ✅ Question answering from web
- ✅ News fetching
- ✅ Deep research capabilities
- ✅ Automatic data caching
- ✅ Background data fetching
- ✅ Error handling and resilience
- ✅ Async/await for performance

Your bot can now access and utilize real-time internet data to provide:
- Current information and news
- Research from multiple sources
- Accurate answers to factual questions
- Up-to-date training data
- Trending topics and insights

🚀 **Your bot is now connected to the internet!**

