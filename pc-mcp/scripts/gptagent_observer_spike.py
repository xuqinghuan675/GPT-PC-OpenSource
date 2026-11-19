"""Explicit, one-shot native GPTAgent observer acceptance. Never auto-resubmit."""
import argparse
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright
from machine_config import load_machine_config,requested_model_policy
from chatgpt_agent_store import AgentStore,digest
from chatgpt_web_observer import PageObserver
from chatgpt_web_controller import ensure_non_pro_effort,submit_native_once

async def main(lease_id,reuse_target=None):
    import server
    c=load_machine_config();store=AgentStore(c)
    existing=store.setting('observer_spike')
    if existing:
        print(json.dumps({'status':'already_registered_no_resubmit','spike':existing},ensure_ascii=False),flush=True)
        return
    check=lambda:server._chatgpt_agent_control_check(lease_id)
    check()
    async with async_playwright() as p:
        b=await p.chromium.connect_over_cdp(c.cdp_endpoint,no_defaults=True,timeout=10000)
        page=None
        if reuse_target:
            for candidate in b.contexts[0].pages:
                cd=await candidate.context.new_cdp_session(candidate)
                info=(await cd.send('Target.getTargetInfo'))['targetInfo'];await cd.detach()
                if info['targetId']==reuse_target:page=candidate;break
            if page is None:raise RuntimeError('Exact prepared target no longer exists')
        else:
            page=await b.contexts[0].new_page()
            await page.goto(store.setting('project_probe')['url'],wait_until='domcontentloaded',timeout=15000)
        await page.locator('#prompt-textarea').wait_for(state='visible',timeout=25000)
        target_session=await page.context.new_cdp_session(page)
        target=(await target_session.send('Target.getTargetInfo'))['targetInfo']['targetId']
        await target_session.detach()
        policy=await ensure_non_pro_effort(page,requested_model_policy(c))
        print(json.dumps({'model_policy':policy,'target_id':target},ensure_ascii=False),flush=True)
        observer=await PageObserver(page,store).start()
        prompt='仅回复 GPTAGENT_OBSERVER_SPIKE_OK。不要搜索，不要使用工具。'
        intent={'state':'PREPARED','target_id':target,'prompt_sha256':digest(prompt),'created_at':store.clock(),'policy':policy}
        store.set_setting('observer_spike',intent)
        def mark():
            intent['state']='SUBMITTING';store.set_setting('observer_spike',intent)
        try:
            initial=await submit_native_once(page,observer,prompt,control_check=check,mark_submitting=mark)
            print(json.dumps({'initial':initial},ensure_ascii=False),flush=True)
            deadline=asyncio.get_running_loop().time()+110
            while asyncio.get_running_loop().time()<deadline:
                check()
                result=await observer.refresh()
                if result['reason']=='trusted_final':break
                await asyncio.sleep(1)
            result={'observer':observer.public(),'requests':observer.requests,'url':page.url,
                'graph_nodes':[{k:v for k,v in node.items() if k!='visible_text'} for node in observer.nodes.values()],
                'visible_reply_matches':any('GPTAGENT_OBSERVER_SPIKE_OK' in n.get('visible_text','') and n.get('role')=='assistant' and not n.get('hidden') for n in observer.nodes.values())}
            intent.update(state='OBSERVED',url=page.url,result=result)
            store.set_setting('observer_spike',intent)
            out=Path(c.agent_data_dir)/'observer-native-acceptance.json'
            out.write_text(json.dumps(intent,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False),flush=True)
            assert result['observer']['generation_post_count']==1, 'Expected exactly one native generation POST'
            assert result['observer']['conversation_id'] and result['observer']['turn_exchange_id'], 'Missing actual turn identity'
            assert result['observer']['reason']=='trusted_final' and result['visible_reply_matches'], 'Trusted public final was not observed'
        finally:
            await observer.stop()

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser();parser.add_argument('--lease-id',required=True);parser.add_argument('--reuse-target')
    args=parser.parse_args()
    asyncio.run(main(args.lease_id,args.reuse_target))
