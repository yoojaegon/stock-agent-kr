# Tavily 뉴스 검색 tool

import os
from tavily import TavilyClient

from tools.domains import KR_FINANCE_DOMAINS

from dotenv import load_dotenv
load_dotenv() # 테스트용 나중에 삭제해야됨

client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))



def search_news(query: str, max_result:int = 5):
    response = client.search(query=query, max_results=max_result, topic="news", time_range="month", search_depth="advanced", include_domains=KR_FINANCE_DOMAINS)
    
    seen = set()
    result = []
    for r in response["results"]:
        if r["url"] not in seen:
            seen.add(r["url"])
            result.append({
                "title": r["title"],
                "url": r["url"],
                "content": r["content"],
                "score": r["score"],
                "published_date": r.get("published_date"),
            })
    
    return result