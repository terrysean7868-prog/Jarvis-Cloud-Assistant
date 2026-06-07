"""
Internet Access Module for JARVIS
High-level API for web searches, data fetching, and real-time information
"""

import asyncio
import logging
from typing import Optional, List, Dict, Any
from .web_scraper import WebScraper, get_scraper, close_scraper
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class InternetAccess:
    """
    High-level interface for internet data access
    Provides methods for searching, fetching, and processing web data
    """
    
    def __init__(self):
        self.scraper = None
        self.cache = {}  # Simple in-memory cache
        self.cache_ttl = 3600  # 1 hour cache
        
    async def initialize(self):
        """Initialize internet access"""
        self.scraper = await get_scraper()
        logger.info("🌐 Internet Access initialized")
        return self
    
    async def close(self):
        """Close internet access"""
        await close_scraper()
        self.cache.clear()
    
    def _get_cache_key(self, action: str, query: str) -> str:
        """Generate cache key"""
        return f"{action}:{query}".lower()
    
    def _is_cache_valid(self, key: str) -> bool:
        """Check if cache entry is valid"""
        if key not in self.cache:
            return False
        
        entry = self.cache[key]
        age = (datetime.now() - entry['timestamp']).total_seconds()
        
        if age > self.cache_ttl:
            del self.cache[key]
            return False
        
        return True
    
    async def search(self, query: str, num_results: int = 5) -> List[Dict[str, str]]:
        """
        Search the web for information
        
        Args:
            query: Search query
            num_results: Number of results to return
            
        Returns:
            List of search results
        """
        try:
            cache_key = self._get_cache_key('search', query)
            
            # Check cache
            if self._is_cache_valid(cache_key):
                logger.info(f"📦 Using cached search results for: {query}")
                return self.cache[cache_key]['data']
            
            # Perform search
            logger.info(f"🔍 Searching web for: {query}")
            await self.initialize()
            results = await self.scraper.google_search(query, num_results=num_results)
            
            # Cache results
            self.cache[cache_key] = {
                'data': results,
                'timestamp': datetime.now()
            }
            
            return results
            
        except Exception as e:
            logger.error(f"❌ Search failed: {str(e)}")
            return []
    
    async def fetch_webpage(self, url: str, include_content: bool = True) -> Optional[Dict[str, Any]]:
        """
        Fetch and parse a webpage
        
        Args:
            url: URL to fetch
            include_content: Whether to include page content
            
        Returns:
            Dictionary with page info and optionally content
        """
        try:
            logger.info(f"📥 Fetching webpage: {url}")
            await self.initialize()
            
            if include_content:
                result = await self.scraper.get_webpage_summary(url)
                return result
            else:
                # Just fetch headers and basic info
                html = await self.scraper.fetch_url(url)
                if html:
                    return {'url': url, 'status': 'fetched', 'size': len(html)}
                return None
                
        except Exception as e:
            logger.error(f"❌ Failed to fetch webpage: {str(e)}")
            return None
    
    async def search_and_summarize(self, query: str, num_results: int = 3) -> List[Dict[str, Any]]:
        """
        Search web and get summaries of top results
        
        Args:
            query: Search query
            num_results: Number of results to summarize
            
        Returns:
            List of results with summaries
        """
        try:
            logger.info(f"🔍 Searching and summarizing: {query}")
            await self.initialize()
            results = await self.scraper.search_and_summarize(query, num_results=num_results)
            return results
            
        except Exception as e:
            logger.error(f"❌ Search and summarize failed: {str(e)}")
            return []
    
    async def get_news(self, topic: str = "latest", num_results: int = 5) -> List[Dict[str, str]]:
        """
        Get latest news on a topic
        
        Args:
            topic: News topic
            num_results: Number of news articles
            
        Returns:
            List of news articles
        """
        try:
            cache_key = self._get_cache_key('news', topic)
            
            if self._is_cache_valid(cache_key):
                logger.info(f"📦 Using cached news for: {topic}")
                return self.cache[cache_key]['data']
            
            logger.info(f"📰 Fetching news about: {topic}")
            await self.initialize()
            news = await self.scraper.get_latest_news(topic, num_results=num_results)
            
            # Cache news
            self.cache[cache_key] = {
                'data': news,
                'timestamp': datetime.now()
            }
            
            return news
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch news: {str(e)}")
            return []
    
    async def get_weather(self, location: str) -> Optional[Dict[str, str]]:
        """
        Get weather information for a location
        
        Args:
            location: Location (city, country)
            
        Returns:
            Weather information
        """
        try:
            logger.info(f"🌤️ Fetching weather for: {location}")
            query = f"weather {location}"
            results = await self.search(query, num_results=1)
            
            if results:
                return {
                    'location': location,
                    'query': query,
                    'source': results[0].get('title', 'Weather Search'),
                    'snippet': results[0].get('snippet', ''),
                    'url': results[0].get('url', '')
                }
            return None
            
        except Exception as e:
            logger.error(f"❌ Weather fetch failed: {str(e)}")
            return None
    
    async def answer_question(self, question: str) -> Optional[str]:
        """
        Search web to answer a question
        
        Args:
            question: Question to answer
            
        Returns:
            Answer summary or None
        """
        try:
            logger.info(f"❓ Answering question: {question}")
            results = await self.search_and_summarize(question, num_results=1)
            
            if results and len(results) > 0:
                result = results[0]
                # Return the snippet or content summary
                answer = result.get('content_summary') or result.get('snippet', '')
                
                if answer:
                    return answer[:500]  # Limit answer length
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Failed to answer question: {str(e)}")
            return None
    
    async def get_facts(self, topic: str, num_facts: int = 3) -> List[str]:
        """
        Get interesting facts about a topic
        
        Args:
            topic: Topic to get facts about
            num_facts: Number of facts to retrieve
            
        Returns:
            List of facts
        """
        try:
            logger.info(f"📚 Fetching facts about: {topic}")
            query = f"{topic} facts interesting information"
            results = await self.search(query, num_results=num_facts)
            
            facts = []
            for result in results:
                snippet = result.get('snippet', '')
                if snippet:
                    facts.append(snippet[:200])
            
            return facts
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch facts: {str(e)}")
            return []
    
    async def research_topic(self, topic: str, depth: int = 3) -> Dict[str, Any]:
        """
        Perform deep research on a topic
        
        Args:
            topic: Topic to research
            depth: Number of search results to process
            
        Returns:
            Comprehensive research data
        """
        try:
            logger.info(f"🔬 Researching topic: {topic} (depth: {depth})")
            
            # Get multiple sources
            results = await self.search_and_summarize(topic, num_results=depth)
            
            # Compile research
            research_data = {
                'topic': topic,
                'timestamp': datetime.now().isoformat(),
                'sources': results,
                'summary': '',
                'key_points': []
            }
            
            # Build summary from results
            summaries = []
            for result in results:
                if 'content_summary' in result:
                    summaries.append(result['content_summary'][:300])
            
            research_data['summary'] = ' '.join(summaries)[:1000]
            
            # Extract key points from snippets
            research_data['key_points'] = [
                r.get('snippet', '')[:150] 
                for r in results 
                if r.get('snippet')
            ][:depth]
            
            return research_data
            
        except Exception as e:
            logger.error(f"❌ Research failed: {str(e)}")
            return {}


# Global instance
_internet_instance = None

async def get_internet() -> InternetAccess:
    """Get or create global internet access instance"""
    global _internet_instance
    if _internet_instance is None:
        _internet_instance = InternetAccess()
        await _internet_instance.initialize()
    return _internet_instance


async def close_internet():
    """Close global internet instance"""
    global _internet_instance
    if _internet_instance:
        await _internet_instance.close()
        _internet_instance = None


# Simple helper functions for common operations
async def search_web(query: str) -> List[Dict[str, str]]:
    """Search the web"""
    internet = await get_internet()
    return await internet.search(query)


async def get_answer(question: str) -> Optional[str]:
    """Get answer to a question"""
    internet = await get_internet()
    return await internet.answer_question(question)


async def research(topic: str) -> Dict[str, Any]:
    """Research a topic"""
    internet = await get_internet()
    return await internet.research_topic(topic)


async def get_facts(topic: str) -> List[str]:
    """Get facts about a topic"""
    internet = await get_internet()
    return await internet.get_facts(topic)


if __name__ == "__main__":
    async def test():
        internet = InternetAccess()
        await internet.initialize()
        
        # Test search
        print("🔍 Test 1: Web Search")
        results = await internet.search("Python async programming", num_results=3)
        for r in results:
            print(f"  • {r['title']}")
        
        # Test answer question
        print("\n❓ Test 2: Answer Question")
        answer = await internet.answer_question("What is machine learning?")
        if answer:
            print(f"  Answer: {answer[:200]}...")
        
        # Test research
        print("\n🔬 Test 3: Research Topic")
        research = await internet.research_topic("Artificial Intelligence", depth=2)
        print(f"  Topic: {research['topic']}")
        print(f"  Sources: {len(research['sources'])}")
        
        await internet.close()
    
    asyncio.run(test())
