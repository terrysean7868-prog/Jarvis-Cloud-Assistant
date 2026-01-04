"""
Web Scraper Module for JARVIS
Enables real-time internet access for data fetching, web scraping, and search functionality
"""

import asyncio
import aiohttp
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import quote, urljoin, urlparse, parse_qs, unquote
import json

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class WebScraper:
    """
    Web scraper for fetching and parsing web content
    Supports Google searches, webpage fetching, and data extraction
    """
    
    def __init__(self):
        self.session = None
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        self.timeout = aiohttp.ClientTimeout(total=10)
        
    async def initialize(self):
        """Initialize async session"""
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self
    
    async def close(self):
        """Close async session"""
        if self.session:
            await self.session.close()
    
    async def fetch_url(self, url: str, timeout: int = 10) -> Optional[str]:
        """
        Fetch raw HTML content from a URL
        
        Args:
            url: URL to fetch
            timeout: Request timeout in seconds
            
        Returns:
            HTML content or None if fetch fails
        """
        try:
            await self.initialize()
            
            async with self.session.get(
                url,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=False
            ) as response:
                if response.status == 200:
                    content = await response.text()
                    logger.info(f"✅ Successfully fetched: {url}")
                    return content
                else:
                    logger.warning(f"⚠️ Failed to fetch {url}: Status {response.status}")
                    return None
                    
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ Timeout fetching {url}")
            return None
        except Exception as e:
            logger.error(f"❌ Error fetching {url}: {str(e)}")
            return None
    
    async def google_search(self, query: str, num_results: int = 5) -> List[Dict[str, str]]:
        """
        Perform Google search using ddg (DuckDuckGo) as fallback
        
        Args:
            query: Search query
            num_results: Number of results to return
            
        Returns:
            List of search results with title, url, and snippet
        """
        try:
            # DuckDuckGo HTML results page (no key needed)
            # NOTE: https://html.duckduckgo.com/?q=... often returns the homepage, not results.
            # Encode fully; quote() defaults to safe='/' which can break queries containing slashes.
            search_url = f"https://duckduckgo.com/html/?q={quote(query, safe='')}"
            lite_url = f"https://lite.duckduckgo.com/lite/?q={quote(query, safe='')}"
            
            await self.initialize()
            html = await self.fetch_url(search_url, timeout=8)
            used_lite = False

            if not html:
                # /html sometimes responds with 202 (bot protection). Fall back to /lite.
                html = await self.fetch_url(lite_url, timeout=8)
                used_lite = bool(html)

            if not html:
                logger.warning(f"Could not perform search: {query}")
                return []
            
            soup = BeautifulSoup(html, 'html.parser')
            results: List[Dict[str, str]] = []

            def _normalize_ddg_href(href: str) -> str:
                if not href:
                    return ""
                href = href.strip()
                if href.startswith("//"):
                    href = "https:" + href
                # DuckDuckGo often returns redirect URLs like /l/?uddg=<encoded>
                try:
                    if href.startswith("/"):
                        href_full = urljoin("https://duckduckgo.com", href)
                    else:
                        href_full = href

                    u = urlparse(href_full)
                    if "duckduckgo.com" in (u.netloc or "") and u.path.startswith("/l/"):
                        uddg = parse_qs(u.query).get("uddg", [""])[0]
                        if uddg:
                            return unquote(uddg)
                    return href_full
                except Exception:
                    return href

            if used_lite:
                # DuckDuckGo lite parsing
                for a in soup.select("a.result-link"):
                    try:
                        title = a.get_text(" ", strip=True)
                        href = a.get("href", "")
                        url = _normalize_ddg_href(href)
                        if not title or not url:
                            continue

                        snippet = ""
                        tr = a.find_parent("tr")
                        if tr:
                            snippet_td = tr.find_next("td", class_="result-snippet")
                            if snippet_td:
                                snippet = snippet_td.get_text(" ", strip=True)

                        results.append({"title": title, "url": url, "snippet": snippet, "source": "DuckDuckGo"})
                        if len(results) >= num_results:
                            break
                    except Exception as e:
                        logger.debug(f"Error parsing search result: {e}")
                        continue
            else:
                # DuckDuckGo /html parsing
                for a in soup.select("a.result__a"):
                    try:
                        title = a.get_text(strip=True)
                        href = a.get("href", "")
                        url = _normalize_ddg_href(href)
                        if not title or not url:
                            continue

                        container = a.find_parent("div", class_=lambda c: isinstance(c, str) and "result" in c.split())
                        if container:
                            classes = container.get("class") or []
                            if isinstance(classes, list) and any("result--ad" in str(x) for x in classes):
                                continue

                        snippet = ""
                        if container:
                            snippet_elem = (
                                container.select_one("a.result__snippet")
                                or container.select_one("div.result__snippet")
                                or container.select_one("span.result__snippet")
                                or container.select_one("div.result__content")
                            )
                            if snippet_elem:
                                snippet = snippet_elem.get_text(" ", strip=True)

                        results.append(
                            {
                                "title": title,
                                "url": url,
                                "snippet": snippet,
                                "source": "DuckDuckGo",
                            }
                        )
                        if len(results) >= num_results:
                            break
                    except Exception as e:
                        logger.debug(f"Error parsing search result: {e}")
                        continue
            
            if results:
                logger.info(f"🔍 Found {len(results)} results for: {query}")
            
            return results
            
        except Exception as e:
            logger.error(f"❌ Google search failed for '{query}': {str(e)}")
            return []
    
    async def extract_text(self, html: str, max_length: int = 2000) -> str:
        """
        Extract clean text from HTML content
        
        Args:
            html: HTML content to parse
            max_length: Maximum length of extracted text
            
        Returns:
            Clean text content
        """
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # Remove script and style elements
            for script in soup(['script', 'style', 'nav', 'footer']):
                script.decompose()
            
            # Get text
            text = soup.get_text(separator=' ', strip=True)
            
            # Clean up text
            text = ' '.join(text.split())  # Remove extra whitespace
            text = text[:max_length]  # Limit length
            
            logger.info(f"📄 Extracted {len(text)} characters of text")
            return text
            
        except Exception as e:
            logger.error(f"❌ Error extracting text: {str(e)}")
            return ""
    
    async def get_webpage_summary(self, url: str, max_chars: int = 1500) -> Optional[Dict[str, str]]:
        """
        Fetch and summarize a webpage
        
        Args:
            url: URL to summarize
            max_chars: Maximum character length of summary
            
        Returns:
            Dictionary with title, url, and summary text
        """
        try:
            html = await self.fetch_url(url)
            if not html:
                return None
            
            soup = BeautifulSoup(html, 'html.parser')
            
            # Extract title
            title = 'No title'
            if soup.title:
                title = soup.title.string
            
            # Extract main content
            text = await self.extract_text(html, max_length=max_chars)
            
            return {
                'title': title,
                'url': url,
                'summary': text,
                'fetched_at': datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"❌ Error summarizing {url}: {str(e)}")
            return None
    
    async def search_and_summarize(self, query: str, num_results: int = 3) -> List[Dict[str, str]]:
        """
        Search Google and fetch summaries of top results
        
        Args:
            query: Search query
            num_results: Number of results to fetch and summarize
            
        Returns:
            List of search results with summaries
        """
        try:
            # Perform search
            search_results = await self.google_search(query, num_results=num_results)
            
            if not search_results:
                logger.warning(f"No search results for: {query}")
                return []
            
            # Fetch and summarize top results
            summaries = []
            for result in search_results[:num_results]:
                try:
                    summary = await self.get_webpage_summary(result['url'])
                    if summary:
                        summaries.append({
                            **result,
                            'content_summary': summary['summary']
                        })
                except Exception as e:
                    logger.debug(f"Could not summarize {result['url']}: {e}")
                    # Still include search result even if summary fails
                    summaries.append(result)
            
            return summaries
            
        except Exception as e:
            logger.error(f"❌ Search and summarize failed: {str(e)}")
            return []
    
    async def get_latest_news(self, topic: str = "technology", num_results: int = 5) -> List[Dict[str, str]]:
        """
        Fetch latest news on a topic
        
        Args:
            topic: News topic to search
            num_results: Number of news articles to fetch
            
        Returns:
            List of news articles
        """
        try:
            query = f"{topic} news latest"
            results = await self.google_search(query, num_results=num_results)
            
            logger.info(f"📰 Fetched {len(results)} news articles about: {topic}")
            return results
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch news: {str(e)}")
            return []
    
    async def get_stock_info(self, symbol: str) -> Optional[Dict[str, str]]:
        """
        Fetch stock information for a symbol
        
        Args:
            symbol: Stock symbol (e.g., 'AAPL')
            
        Returns:
            Stock information dictionary
        """
        try:
            # Using Yahoo Finance alternative
            url = f"https://finance.yahoo.com/quote/{symbol}"
            html = await self.fetch_url(url)
            
            if not html:
                return None
            
            soup = BeautifulSoup(html, 'html.parser')
            
            # Try to extract price (simplified)
            # Note: Yahoo blocks web scraping, so this may not work
            # For production, use yfinance library instead
            
            return {
                'symbol': symbol,
                'url': url,
                'note': 'Use yfinance library for reliable stock data'
            }
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch stock info for {symbol}: {str(e)}")
            return None
    
    async def extract_data(self, url: str, selectors: Dict[str, str]) -> Dict[str, Any]:
        """
        Extract specific data from webpage using CSS selectors
        
        Args:
            url: Webpage URL
            selectors: Dictionary mapping field names to CSS selectors
            
        Returns:
            Dictionary of extracted data
        """
        try:
            html = await self.fetch_url(url)
            if not html:
                return {}
            
            soup = BeautifulSoup(html, 'html.parser')
            extracted = {}
            
            for field, selector in selectors.items():
                try:
                    element = soup.select_one(selector)
                    if element:
                        extracted[field] = element.get_text(strip=True)
                except Exception as e:
                    logger.debug(f"Could not extract {field}: {e}")
            
            logger.info(f"📊 Extracted {len(extracted)} fields from {url}")
            return extracted
            
        except Exception as e:
            logger.error(f"❌ Error extracting data: {str(e)}")
            return {}


# Global scraper instance
_scraper_instance = None

async def get_scraper() -> WebScraper:
    """Get or create global scraper instance"""
    global _scraper_instance
    if _scraper_instance is None:
        _scraper_instance = WebScraper()
        await _scraper_instance.initialize()
    return _scraper_instance


async def close_scraper():
    """Close global scraper instance"""
    global _scraper_instance
    if _scraper_instance:
        await _scraper_instance.close()
        _scraper_instance = None


# Example usage functions
async def search_web(query: str, num_results: int = 5) -> List[Dict[str, str]]:
    """Simple interface to search web"""
    scraper = await get_scraper()
    return await scraper.google_search(query, num_results=num_results)


async def get_info(url: str) -> Optional[Dict[str, str]]:
    """Simple interface to get webpage summary"""
    scraper = await get_scraper()
    return await scraper.get_webpage_summary(url)


async def search_and_get_info(query: str, num_results: int = 3) -> List[Dict[str, str]]:
    """Simple interface to search and get summaries"""
    scraper = await get_scraper()
    return await scraper.search_and_summarize(query, num_results=num_results)


if __name__ == "__main__":
    # Test the scraper
    async def test():
        scraper = WebScraper()
        await scraper.initialize()
        
        # Test Google search
        print("🔍 Testing Google search...")
        results = await scraper.google_search("Python programming tutorial", num_results=3)
        for result in results:
            print(f"  • {result['title']}")
            print(f"    {result['snippet'][:100]}...")
        
        # Test fetch and summarize
        print("\n📄 Testing fetch and summarize...")
        if results:
            summary = await scraper.get_webpage_summary(results[0]['url'])
            if summary:
                print(f"  Title: {summary['title']}")
                print(f"  Content preview: {summary['summary'][:200]}...")
        
        await scraper.close()
    
    # Run test
    asyncio.run(test())
