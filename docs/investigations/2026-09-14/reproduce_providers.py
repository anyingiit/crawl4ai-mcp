import os
if os.environ.get('CRAWL4AI_MCP_LIVE_TESTS') != '1':
 raise SystemExit('Set CRAWL4AI_MCP_LIVE_TESTS=1; this audit calls configured paid providers.')
import asyncio,json,tempfile,time
from pathlib import Path
from dotenv import dotenv_values
from bs4 import BeautifulSoup
from crawl4ai_mcp.config import load_config
from crawl4ai_mcp.service import CrawlService
from crawl4ai_mcp.models import Tier
from crawl4ai_mcp.detect import classify,CLOUDFLARE_MARKERS
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'docs/investigations/2026-09-14'
async def main():
 with tempfile.TemporaryDirectory() as td:
  cfg=load_config(ROOT/'config.toml',env=dotenv_values(ROOT/'.env')).model_copy(update={'database_path':Path(td)/'p.db'})
  svc=CrawlService(cfg);await svc.start();rows=[]
  try:
   for tier,url in [(Tier.CAMOUFOX,'https://example.com/'),(Tier.PROXY,'https://example.com/'),(Tier.RAYOBYTE,'https://example.com/'),(Tier.FIRECRAWL,'https://example.com/'),(Tier.FIRECRAWL,'https://developers.cloudflare.com/pages/get-started/direct-upload/')]:
    p=svc.providers[tier];t=time.monotonic()
    try:
     f=await asyncio.wait_for(p.fetch(url),90)
     d=f.model_dump(mode='json');html=d.pop('html','');md=d.pop('markdown',None) or '';soup=BeautifulSoup(html,'lxml');txt=soup.get_text(' ',strip=True)
     d.pop('headers',None)
     d.update(decision=str(classify(f)),title=soup.title.get_text() if soup.title else None,text_chars=len(txt),markdown_chars=len(md),cf_matches=[m for m in CLOUDFLARE_MARKERS if m in (html+str(f.headers)).lower()],has_example='Example Domain' in txt+md,has_upload='Direct Upload enables' in txt+md)
     row={'tier':tier.name.lower(),'availability':p.availability().model_dump(mode='json'),'fetch':d,'wall_seconds':round(time.monotonic()-t,2)}
    except Exception as e:row={'tier':tier.name.lower(),'exception':type(e).__name__+': '+str(e)}
    rows.append(row);(OUT/'providers-live.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2));print(json.dumps(row),flush=True)
  finally:await svc.close()
asyncio.run(main())
