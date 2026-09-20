import asyncio,json,tempfile,time,sys
from pathlib import Path
from dotenv import dotenv_values
from bs4 import BeautifulSoup
from fastmcp import Client
from crawl4ai_mcp.config import load_config
from crawl4ai_mcp.service import CrawlService
from crawl4ai_mcp.models import Tier
from crawl4ai_mcp.detect import classify,CLOUDFLARE_MARKERS
ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'docs/investigations/2026-09-14'
TARGETS=[
('example','https://example.com/','Example Domain'),
('python','https://docs.python.org/3/library/asyncio.html','asyncio'),
('quotes_js','https://quotes.toscrape.com/js/','The world as we have created it'),
('cf_upload','https://developers.cloudflare.com/pages/get-started/direct-upload/','Direct Upload enables'),
('cf_waiting','https://developers.cloudflare.com/waiting-room/plans/','Waiting Room'),
('redis','https://redis.io/docs/latest/develop/use/patterns/distributed-locks/','Distributed Locks'),
('laravel','https://laravel.com/docs/12.x/queries','Pessimistic Locking'),
('nature','https://www.nature.com/articles/s41467-023-35886-6','Assessing and advancing'),
('unesco','https://whc.unesco.org/en/list/440/','Mogao Caves'),
('kubernetes','https://kubernetes.io/docs/concepts/workloads/pods/disruptions/','Disruptions')]
async def main():
 async with Client('http://127.0.0.1:11236/mcp') as c:
  r=await c.call_tool('diagnose',{})
  d=json.loads(r.content[0].text)
  # `recent_failures` comes from the live daemon, so it holds whatever
  # unrelated requests happened to fail -- full paths and query strings.
  # This file is tracked, so the URL is dropped and the parts that
  # actually diagnose anything are kept.
  failures=[{**f,'url':'REDACTED'} for f in d['recent_failures']]
  selected={'providers':d['providers'],'recent_failures':failures,'policy_domain_count':len(d['domain_policies'])}
  (OUT/'diagnose.json').write_text(json.dumps(selected,indent=2,ensure_ascii=False))
 with tempfile.TemporaryDirectory() as td:
  cfg=load_config(ROOT/'config.toml',env=dotenv_values(ROOT/'.env')).model_copy(update={'database_path':Path(td)/'policy.db'})
  svc=CrawlService(cfg); await svc.start()
  records=[]
  try:
   for name,url,marker in TARGETS:
    await svc.policy.clear()
    t=time.monotonic(); fetches=[]
    originals={}
    for tier,p in svc.providers.items():
     originals[tier]=p.fetch
     async def wrapped(u,original=p.fetch):
      f=await original(u)
      soup=BeautifulSoup(f.html,'lxml'); text=soup.get_text(' ',strip=True)
      hay=(f.html+str(f.headers)).lower()
      matches=[m for m in CLOUDFLARE_MARKERS if m in hay]
      fetches.append({'tier':f.tier.name.lower(),'status':f.target_status_code,'decision':str(classify(f)), 'html_chars':len(f.html),'text_chars':len(text),'title':soup.title.get_text() if soup.title else None,'expected_marker_found':marker.lower() in (text+' '+(f.markdown or '')).lower(),'cf_matches':matches,'text_prefix':text[:160],'error':f.error})
      return f
     p.fetch=wrapped
    try:
     r=await asyncio.wait_for(svc.scrape(url,max_tier='undetected'),150)
     content=r.pop('content',''); r['content_chars']=len(content); r['expected_marker_found']=marker.lower() in content.lower()
     row={'name':name,'url':url,'wall_seconds':round(time.monotonic()-t,2),'response':r,'fetches':fetches}
    except Exception as e:
     row={'name':name,'url':url,'exception':type(e).__name__+': '+str(e),'fetches':fetches}
    finally:
     for tier,p in svc.providers.items(): p.fetch=originals[tier]
    records.append(row); (OUT/'live-free.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
    print(json.dumps(row,ensure_ascii=False),flush=True)
  finally: await svc.close()
asyncio.run(main())
