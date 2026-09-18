import asyncio,json,tempfile
from pathlib import Path
from crawl4ai_mcp.detect import classify
from crawl4ai_mcp.models import *
from crawl4ai_mcp.cascade import CascadeEngine
from crawl4ai_mcp.policy import PolicyStore
from crawl4ai_mcp.providers.http import HttpProvider
from crawl4ai_mcp.egress import UrlPolicy

def result(html='',tier=Tier.HTTP,**kw):
 return FetchResult(url='https://example.com/a',tier=tier,cost_kind=CostKind.FREE,elapsed_ms=1,html=html,**kw)
class Provider:
 def __init__(self,tier,value): self.tier=tier; self.cost_kind=CostKind.FREE; self.value=value; self.calls=0
 def availability(self): return ProviderAvailability(enabled=True,ready=True)
 async def fetch(self,url): self.calls+=1; return self.value
async def main():
 rows=[]
 for name,html in [('normal_doc_mentions_turnstile','<main>'+('Normal useful documentation. '*20)+'Turnstile</main>'),('empty_200',''),('bot_interstitial_200','<h1>Access Denied</h1><p>Please enable cookies to continue.</p>'),('short_rendered_page_with_script','<main>Useful short answer</main><script src="analytics.js"></script>')]:
  rows.append({'case':name,'decision':str(classify(result(html,target_status_code=200)))})
 with tempfile.TemporaryDirectory() as td:
  p=await PolicyStore.open(Path(td)/'p.db')
  http=Provider(Tier.HTTP,result(network_error='request_failed'))
  browser=Provider(Tier.STEALTH,result('<main>'+'Useful content '*30+'</main>',tier=Tier.STEALTH,target_status_code=200,markdown='Useful content'))
  e=CascadeEngine({Tier.HTTP:http,Tier.STEALTH:browser},p,clock=lambda:10000)
  first=await e.scrape('https://example.com/a',maximum=Tier.STEALTH)
  second=await e.scrape('https://example.com/b',maximum=Tier.STEALTH)
  rows.append({'case':'http_error_then_healthy_browser_and_sibling_url','first_status':first.response.status,'http_calls':http.calls,'healthy_browser_calls':browser.calls,'second_status':second.response.status,'cooldown_seconds':second.response.cooldown_until-10000})
  await p.clear()
  await p.record_success('https://example.com/a',Tier.FIRECRAWL,10000)
  fallback=Provider(Tier.FIRECRAWL,result(tier=Tier.FIRECRAWL,provider_error_kind=ProviderErrorKind.QUOTA))
  good=Provider(Tier.HTTP,result('<main>'+'Useful content '*30+'</main>',target_status_code=200,markdown='Useful'))
  e=CascadeEngine({Tier.HTTP:good,Tier.FIRECRAWL:fallback},p,clock=lambda:10000)
  out=await e.scrape('https://example.com/b')
  rows.append({'case':'remembered_paid_tier_broken_lower_http_healthy','status':out.response.status,'healthy_http_calls':good.calls,'paid_calls':fallback.calls})
  await p.close()
 print(json.dumps(rows,ensure_ascii=False,indent=2))
asyncio.run(main())
