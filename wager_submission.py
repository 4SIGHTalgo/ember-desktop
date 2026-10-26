"""Bounded continuation of one wager through explicit provider confirmations."""
import time


SUBMISSION_UI_JS = r"""
const submissionText=e=>(e?.innerText||'').replace(/\s+/g,' ').trim();
const submissionKind=button=>/accept\s+(?:odds|price)\s+changes?/i.test(submissionText(button))?'accept-price-change':
    /^place\s*bet\b/i.test(submissionText(button))?'place-bet':null;
const watchSubmissionNetwork=()=>{
    if(window.__ttSubmitNetwork)return window.__ttSubmitNetwork;
    const state={sequence:0,requests:[]};window.__ttSubmitNetwork=state;
    const begin=(address,method)=>{
        try{const url=new URL(address,location.href);
            if(!/^(POST|PUT|PATCH)$/i.test(method)||
                !(url.hostname===location.hostname||url.hostname.endsWith('.draftkings.com'))||
                !/place.?bets?|submit.?bets?|wagers?|\/bets(?:\/|$)|betplacement/i.test(url.pathname))return null;
            const entry={id:++state.sequence,pending:true,status:null};state.requests.push(entry);
            if(state.requests.length>200)state.requests.shift();return entry;
        }catch(_){return null;}
    };
    const finish=(entry,status)=>{if(entry){entry.pending=false;entry.status=status;}};
    const fetchOriginal=window.fetch;
    window.fetch=function(input,options){
        const entry=begin(typeof input==='string'||input instanceof URL?input:input?.url,options?.method||input?.method||'GET');
        try{return fetchOriginal.apply(this,arguments).then(response=>{finish(entry,response.status);return response;},
            error=>{finish(entry,0);throw error;});}catch(error){finish(entry,0);throw error;}
    };
    const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send,requests=new WeakMap();
    XMLHttpRequest.prototype.open=function(method,url){requests.set(this,{method,url});return open.apply(this,arguments);};
    XMLHttpRequest.prototype.send=function(){const info=requests.get(this),entry=info?begin(info.url,info.method):null;
        if(entry)this.addEventListener('loadend',()=>finish(entry,this.status),{once:true});
        try{return send.apply(this,arguments);}catch(error){finish(entry,0);throw error;}
    };
    return state;
};
const loadNotice=()=>{
    const pattern=/(?:oops[\s-]*)?something\s+didn.t\s+load\s+right/i;
    const matches=e=>{const t=(e.textContent||'').replace(/\s+/g,' ').trim();
        return t.length<350&&pattern.test(t)&&visibleUI(e)&&pattern.test(submissionText(e));};
    for(const e of document.querySelectorAll('[role="alert"],[data-testid*="toast"]'))if(matches(e))return e;
    if(!document.body)return null;
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    for(let text=walker.nextNode();text;text=walker.nextNode()){
        if(!/oops|something|load\s+right/i.test(text.nodeValue||''))continue;
        if(text.parentElement?.closest('script,style,noscript,template'))continue;
        if(!visibleUI(text.parentElement))continue;
        for(let e=text.parentElement;e;e=e.parentElement){
            if((e.textContent||'').length>700)break;
            if(matches(e))return e;
        }
    }
    return null;
};
const stakeLimitPrompt=()=>{
    const roots=new Set();
    const headings=[...document.querySelectorAll('h1,h2,h3,h4,strong,p,span,div')].filter(visibleUI)
        .filter(e=>/^stake\s+amount\s+too\s+high[.!]?$/i.test(submissionText(e)));
    for(const heading of headings){
        for(let root=heading,n=0;root&&root!==document.body&&n<12;root=root.parentElement,n++){
            const text=submissionText(root);
            if(!/(?:can.t|cannot|could not)\s+be\s+placed/i.test(text))continue;
            const buttons=[...root.querySelectorAll('button,[role="button"]')].filter(visibleUI)
                .filter(button=>submissionKind(button)==='place-bet');
            const inputs=[...root.querySelectorAll('input')].filter(visibleUI)
                .filter(input=>input.matches('[data-testid="betslip-wager-box-input"],input[name="stake"],input[inputmode="decimal"]'));
            if(buttons.length===1&&inputs.length===1){roots.add(root);break;}
        }
    }
    const candidates=[...roots].filter(root=>![...roots].some(other=>other!==root&&root.contains(other)));
    if(candidates.length!==1)return null;
    const root=candidates[0],text=submissionText(root);
    const maximum=text.match(/max(?:imum)?\s+stake\s+for\s+(?:the|this)\s+bet\s+is\s*:\s*(?:your\s+stake\s+amount\s+has\s+been\s+adjusted\s+accordingly[.!]?\s*)?\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})(?!\d)/i);
    const amount=maximum?Number(maximum[1].replace(/,/g,'')):null;
    const button=[...root.querySelectorAll('button,[role="button"]')].filter(visibleUI).find(e=>submissionKind(e)==='place-bet');
    return {root,button,maximum:Number.isFinite(amount)&&amount>0?amount:null};
};
const submissionFingerprint=(surface,button,maximum=null)=>JSON.stringify([
    submissionKind(button),submissionText(button),maximum,
    [...surface.querySelectorAll('input[data-testid="betslip-wager-box-input"],input[name="stake"],input[inputmode="decimal"]')]
        .filter(visibleUI).map(e=>e.value),
    [...surface.querySelectorAll('[data-testid="betslip-odds-standard"]')].filter(visibleUI).map(submissionText)
]);
const freshSubmissionReceipt=slip=>{
    const baseline=window.__ttSubmitReceiptBaseline;
    const text=submissionText(slip);
    const receipts=slip?[...slip.querySelectorAll('[data-testid="dk-betslip-receipt"]')].filter(visibleUI):[];
    const fresh=receipts.filter(element=>!baseline||!baseline.receipts.some(old=>old.element===element&&old.text===submissionText(element)));
    const freshText=fresh.map(submissionText).join(' ');
    return {placed:receipts.length?/bet placed/i.test(freshText):
        (!baseline||baseline.slip!==slip||baseline.text!==text)&&/bet placed/i.test(text),
        staleReceiptIgnored:receipts.length>fresh.length,text:receipts.length&&fresh.length?freshText:text};
};
const submissionContinuation=(slip)=>{
    const flow=window.__ttSubmitFlow;
    if(!flow||freshSubmissionReceipt(slip).placed||loadNotice())return null;
    const limit=stakeLimitPrompt();
    const requests=(window.__ttSubmitNetwork?.requests||[]).filter(row=>row.id>flow.networkStart);
    if(requests.some(row=>row.pending))return null;
    const surface=limit?.root||slip;
    const buttons=limit?[limit.button]:[...slip.querySelectorAll('button[data-testid="betslip-place-bet-button"]')].filter(visibleUI);
    if(buttons.length!==1||buttons[0].disabled||buttons[0].getAttribute('aria-disabled')==='true'||
        surface.getAttribute('aria-busy')==='true'||surface.querySelector('[aria-busy="true"]'))return null;
    const button=buttons[0],kind=submissionKind(button);
    const fingerprint=submissionFingerprint(surface,button,limit?.maximum);
    if(fingerprint===flow.fingerprint)return null;
    let action=null;
    if(limit?.maximum!==null&&limit?.maximum!==undefined)action='stake-limit';
    // Some controls accept AND place in one request. A changed label alone must
    // never replay a bet-placement request whose receipt is still delayed.
    else if(!limit&&flow.kind==='accept-price-change'&&kind==='place-bet'&&!requests.length)action='accepted-price';
    else if(!limit&&kind==='accept-price-change')action='price-change';
    return action?{kind:action,token:flow.token,fingerprint,maximum:limit?.maximum??null}:null;
};
const checkSubmissionContinuation=(cfg,slip)=>{
    if(!cfg.continuation)return null;
    if(freshSubmissionReceipt(slip).placed)return {status:'already-placed'};
    const current=submissionContinuation(slip),expected=cfg.continuation;
    if(!current||current.token!==expected.token||current.fingerprint!==expected.fingerprint||current.kind!==expected.kind)
        return {status:'aborted',reason:'provider confirmation changed before validation'};
    return null;
};
const recordSubmissionClick=(cfg,slip,surface,button,kind,maximum=null)=>{
    const network=watchSubmissionNetwork();
    if(!cfg.continuation){
        window.__ttSubmitReceiptBaseline={slip,text:submissionText(slip),
            receipts:[...slip.querySelectorAll('[data-testid="dk-betslip-receipt"]')].filter(visibleUI)
                .map(element=>({element,text:submissionText(element)}))};
    }
    window.__ttSubmitFlow={kind,token:String(Date.now())+'-'+String((window.__ttSubmitSequence||0)+1),
        fingerprint:submissionFingerprint(surface,button,maximum),networkStart:network.sequence};
    window.__ttSubmitSequence=(window.__ttSubmitSequence||0)+1;
};
"""

LOAD_RECOVERY_JS = SUBMISSION_UI_JS + r"""
const notice=loadNotice();
if(!notice)return {present:false};
if(arguments[0]?.dismiss){
    for(let root=notice,n=0;root&&root!==document.body&&n<4;root=root.parentElement,n++){
        if(submissionText(root).length>350)break;
        const closes=[...root.querySelectorAll('button,[role="button"]')].filter(visibleUI).filter(e=>
            /^(?:close|dismiss|close notification|dismiss notification)$/i.test(e.getAttribute('aria-label')||e.getAttribute('title')||'')||/^[×✕x]$/i.test(submissionText(e)));
        if(closes.length===1){closes[0].click();return {present:true,dismissed:true};}
    }
}
return {present:true,dismissed:false};
"""


def recover_load_notice(driver, script, timeout=2):
    """Before dispatch only: dismiss a specific provider toast and recheck readiness."""
    deadline = time.monotonic()+timeout
    state = driver.execute_script(script, {"dismiss": True}) or {}
    while state.get("present") and time.monotonic() < deadline:
        time.sleep(.1)
        state = driver.execute_script(script, {}) or {}
    return not state.get("present")


def settle_submission(driver, confirmation, read_script, continue_submit=None, timeout=12, max_actions=4):
    """Continue only explicit changed prompts. An unanswered Place Bet is never replayed."""
    actions = [confirmation]
    deadline = time.monotonic()+timeout
    candidate, stable_since = None, None
    receipt = {}
    blocked = None
    while time.monotonic() < deadline:
        receipt = driver.execute_script(read_script) or {}
        if receipt.get("placed") or receipt.get("failed") or receipt.get("locationBlocked"):
            break
        step = receipt.get("continuation")
        if step and continue_submit and len(actions) < max_actions:
            identity = (step["token"], step["fingerprint"])
            if identity != candidate:
                candidate, stable_since = identity, time.monotonic()
            elif time.monotonic()-stable_since >= .2:
                response = continue_submit(step) or {}
                if response.get("status") == "already-placed":
                    continue
                if response.get("status") != "submitted":
                    blocked = response.get("reason", "provider confirmation was not authorized")
                    break
                actions.append(response)
                candidate, stable_since = None, None
        else:
            candidate, stable_since = None, None
        time.sleep(.05)
    return {**receipt, "actions": actions, "lastConfirmation": actions[-1],
            "confirmationBlocked": blocked, "actionLimitReached": len(actions) >= max_actions}
