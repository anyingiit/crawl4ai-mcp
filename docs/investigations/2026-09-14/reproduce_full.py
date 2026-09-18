import os
if os.environ.get('CRAWL4AI_MCP_LIVE_TESTS') != '1':
 raise SystemExit('Set CRAWL4AI_MCP_LIVE_TESTS=1; this audit calls configured paid providers.')
import asyncio,json,tempfile,time
from pathlib import Path
from dotenv import dotenv_values
from crawl4ai_mcp.config import load_config
from crawl4ai_mcp.service import CrawlService
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'docs/investigations/2026-09-14'
async def main():
 with tempfile.TemporaryDirectory() as td:
  cfg=load_config(ROOT/'config.toml',env=dotenv_values(ROOT/'.env')).model_copy(update={'database_path':Path(td)/'p.db'})
  svc=CrawlService(cfg);await svc.start();rows=[]
  try:
   targets=[('cf_upload','https://developers.cloudflare.com/pages/get-started/direct-upload/','Direct Upload enables'),('cf_waiting','https://developers.cloudflare.com/waiting-room/plans/','Waiting Room'),('redis','https://redis.io/docs/latest/develop/use/patterns/distributed-locks/','Distributed Locks'),('laravel','https://laravel.com/docs/12.x/queries','Pessimistic Locking'),('unesco_verified','https://whc.unesco.org/en/list/440/','Mogao Caves')]
   for name,url,marker in targets:
    await svc.policy.clear();t=time.monotonic()
    try:
     r=await asyncio.wait_for(svc.scrape(url),180)
     content=r.pop('content','');r['content_chars']=len(content);r['expected_marker_found']=marker.lower() in content.lower()
     row={'name':name,'url':url,'wall_seconds':round(time.monotonic()-t,2),'response':r}
    except Exception as e:row={'name':name,'url':url,'exception':type(e).__name__+': '+str(e)}
    rows.append(row);(OUT/'live-full-rechecks.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2));print(json.dumps(row),flush=True)
  finally:await svc.close()
asyncio.run(main())
