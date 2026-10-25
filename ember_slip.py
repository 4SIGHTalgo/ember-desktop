"""Execution-layer exact-leg readers; no navigation, strategy, or wager clicks.

The caller supplies visibleUI and slipDecimal. Unknown tracking/layouts are
unsupported: labels alone never establish a contract's identity.
"""
from datetime import datetime
import math


def plan_slip_config(event, market, plan, outcome=None):
    """Build the identity/deadline supplied to every protocol-2 DOM operation."""
    if any(not isinstance(value, dict) for value in (event, market, plan)):
        raise ValueError("plan slip contract must be an object")
    if outcome is None:
        outcome = {"selectionId": plan.get("selectionId"), "label": plan.get("selection")}
    if not isinstance(outcome, dict) or not isinstance(plan.get("specification"), dict):
        raise ValueError("plan slip selection and specification must be objects")
    identifiers = (event.get("eventId"), market.get("marketId"), outcome.get("selectionId"))
    if any(not isinstance(value, str) or not value.strip() for value in identifiers):
        raise ValueError("plan slip identity is incomplete")
    if (market.get("eventId") != event["eventId"] or plan.get("selectionId") != outcome["selectionId"]
            or plan.get("selection") != outcome.get("label")
            or any(not isinstance(value, str) or not value.strip() for value in (market.get("name"), outcome.get("label")))):
        raise ValueError("plan slip identity disagrees with the selected contract")
    deadline = datetime.fromisoformat(plan["deadline"])
    if deadline.tzinfo is None:
        raise ValueError("plan deadline must be timezone-aware")
    line = (plan.get("specification") or {}).get("line")
    if line is not None and (type(line) not in (int, float) or not math.isfinite(line)):
        raise ValueError("plan line is invalid")
    return {"eventId": event["eventId"], "marketId": market["marketId"],
            "selectionId": outcome["selectionId"], "selectionLabel": outcome["label"],
            "marketName": market["name"], "selectionLine": line,
            "validUntil": deadline.timestamp() * 1000}


EXACT_SLIP_READER_JS = r"""
const stakeSelector='input[data-testid="betslip-wager-box-input"],input[name="stake"],input[inputmode="decimal"]';
const removeSelector='[data-testid="betslip-selection-card-ex-button"]';
const priceSelector='[data-testid="betslip-odds-standard"]';
const leafSlipPrices=root=>[...root.querySelectorAll(priceSelector)].filter(e=>!e.querySelector(priceSelector));
const slipText=e=>(e?.innerText||'').replace(/\s+/g,' ').trim();
const slipKey=s=>(s||'').toLowerCase().replace(/[łøđ]/g,c=>({ł:'l',ø:'o',đ:'d'})[c])
    .normalize('NFKD').replace(/\p{M}/gu,'').replace(/[^\p{L}\p{N}]+/gu,' ').trim();
// Recalculated cash payouts cannot establish a contract line or identity change.
const slipContractText=root=>{
    const copy=root.cloneNode(true);
    for(const e of copy.querySelectorAll(priceSelector+','+removeSelector+','+stakeSelector))e.remove();
    return copy.textContent.replace(/\$\s*[\d,.]+/g,' ').replace(/\s+/g,' ').trim();
};
const zeroStake=input=>!input.value.trim()||/^\d+(?:\.\d+)?$/.test(input.value.trim())&&Number(input.value)===0;
const writeSlipStake=(input,value)=>{
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);
    input.dispatchEvent(new Event('input',{bubbles:true}));
    input.dispatchEvent(new Event('change',{bubbles:true}));
};
const exactSlipSnapshot=()=>{
    const panels=[...document.querySelectorAll('[data-testid="betslip-content-wrapper"]')];
    const bad=reason=>({status:'unsupported',reason,legs:[]});
    if(panels.length!==1||!visibleUI(panels[0]))return bad('bet slip is hidden or ambiguous');
    const slip=panels[0],removes=[...slip.querySelectorAll(removeSelector)];
    const domIds=[slip,...slip.querySelectorAll('[id]')].map(e=>e.id).filter(Boolean);
    const allIds=[...document.querySelectorAll('[id]')].map(e=>e.id);
    if(domIds.some(id=>allIds.filter(other=>other===id).length!==1))return bad('duplicate DOM ids');
    const inputs=[...slip.querySelectorAll(stakeSelector)];
    if([...slip.querySelectorAll('input')].some(input=>!input.matches(stakeSelector)&&!zeroStake(input)))
        return bad('unrecognized funded slip input');
    if([...document.querySelectorAll(stakeSelector)].some(input=>!slip.contains(input)&&!zeroStake(input)))
        return bad('funded stake control outside the slip');
    const legs=[],ids=new Set();
    for(const remove of removes){
        let tracking;
        try{tracking=JSON.parse(remove.getAttribute('data-tracking'));}catch(_){return bad('malformed leg tracking');}
        if(!tracking||['selectionId','eventId','marketId'].some(k=>
            !['string','number'].includes(typeof tracking[k])||!String(tracking[k]).trim()||
            typeof tracking[k]==='number'&&!Number.isSafeInteger(tracking[k])))return bad('incomplete leg tracking');
        const identity=Object.fromEntries(['selectionId','eventId','marketId'].map(k=>[k,String(tracking[k])]));
        if(ids.has(identity.selectionId))return bad('duplicate selection ids');
        ids.add(identity.selectionId);
        // The minimal common ancestor must isolate exactly one remove, price and stake.
        // No provider card-id guess or player-name selector is used as a fallback.
        let root=remove.parentElement;
        while(root&&root!==slip&&!(root.querySelector(stakeSelector)&&root.querySelector(priceSelector)))root=root.parentElement;
        if(!root||root===slip||root.querySelectorAll(removeSelector).length!==1||
            root.querySelectorAll(stakeSelector).length!==1||leafSlipPrices(root).length!==1)
            return bad('unsupported leg layout');
        const input=root.querySelector(stakeSelector),price=leafSlipPrices(root)[0];
        if(!visibleUI(root)||!visibleUI(remove)||!visibleUI(input)||!visibleUI(price)||
            input.getAttribute('data-testid')!=='betslip-wager-box-input')return bad('hidden or unsupported leg controls');
        const ancestors=[];
        for(let node=root;node&&node!==slip;node=node.parentElement)ancestors.push(node);
        const suspended=[...ancestors,...root.querySelectorAll('[aria-disabled],[disabled]')].some(e=>
            e.getAttribute('aria-disabled')==='true'||e.disabled)||/\bsuspended\b|selection (?:is )?unavailable/i.test(slipText(root));
        const decimalOdds=slipDecimal(root);
        legs.push({...identity,root,input,price,text:slipText(root),decimalOdds,suspended,
            stakeValue:input.value,fingerprint:JSON.stringify([identity,slipContractText(root),price.textContent.trim()])});
    }
    if(leafSlipPrices(slip).some(price=>!legs.some(leg=>leg.price===price)))
        return bad('untracked or ambiguous priced leg');
    if(inputs.some(input=>!legs.some(leg=>leg.input===input)&&!zeroStake(input)))return bad('non-target or parlay stake is not zero');
    if(!removes.length&&!slip.querySelector('[data-testid="betslip-empty-state-title"]'))return bad('unsupported slip without tracked legs');
    return {status:removes.length?'present':'empty',slip,legs,inputs,stagingSafe:inputs.every(zeroStake)};
};
const readExactPlanLeg=(cfg,cleanup=false)=>{
    const bad=reason=>({status:'aborted',uiRejected:true,reason});
    if(['eventId','marketId','selectionId','selectionLabel','marketName'].some(k=>typeof cfg[k]!=='string'||!cfg[k].trim()))
        return bad('exact plan leg identity is required');
    if(!cleanup&&(!Number.isFinite(cfg.validUntil)||Date.now()>=cfg.validUntil))return bad('plan deadline expired');
    const snapshot=exactSlipSnapshot();
    if(!['present','empty'].includes(snapshot.status))return bad(snapshot.reason);
    if(snapshot.status==='empty'&&!cleanup)return {status:'waiting',notReady:true,empty:true,reason:'waiting for the exact selection to reach the slip'};
    const matches=snapshot.legs.filter(leg=>['eventId','marketId','selectionId'].every(k=>leg[k]===cfg[k]));
    if(matches.length!==1)return {...bad('exact target leg is missing or ambiguous'),notReady:matches.length===0};
    const leg=matches[0],{slip}=snapshot;
    if(cleanup)return {...leg,slip,status:'ready'};
    if(snapshot.inputs.some(input=>input!==leg.input&&!zeroStake(input)))return bad('non-target or parlay stake is not zero');
    if(leg.suspended||leg.input.readOnly)return {...bad('target leg is suspended or disabled'),notReady:true};
    const contains=value=>(' '+slipKey(leg.text)+' ').includes(' '+slipKey(value)+' ');
    const markets=cfg.marketName==='Moneyline'?['Moneyline','Match Winner']:[cfg.marketName];
    if(!contains(cfg.selectionLabel)||!markets.some(contains))return bad('target leg label or market changed');
    if(cfg.selectionLine!==null&&cfg.selectionLine!==undefined){
        const numbers=[...slipContractText(leg.root).replace(/\u2212|\u2013/g,'-').matchAll(/(?<![\d.+-])([+\-]?\d+(?:\.\d+)?)(?![\d.])/g)].map(m=>Number(m[1]));
        if(!numbers.length||numbers.some(n=>Math.abs(n-cfg.selectionLine)>=1e-9))return bad('target leg line changed or ambiguous');
    }
    if(leg.decimalOdds===null)return bad('target leg price is unreadable');
    if(cfg.expectedFingerprint&&leg.fingerprint!==cfg.expectedFingerprint)return bad('target leg identity, line or odds changed after claim');
    if(Number.isFinite(cfg.expectedDecimal)&&Math.abs(leg.decimalOdds-cfg.expectedDecimal)>1e-9)return bad('target leg odds changed after claim');
    const buttons=[...slip.querySelectorAll('button[data-testid="betslip-place-bet-button"]')].filter(visibleUI);
    if(buttons.length!==1)return bad('place-bet button is hidden or ambiguous');
    const button=buttons[0],label=slipText(button);
    const login=[...document.querySelectorAll('button,a,[role="button"]')].filter(visibleUI)
        .some(e=>/^(?:sign\s*up\s*(?:or|\/)\s*)?(?:log\s*in|sign\s*in)$/i.test(slipText(e)));
    if(login||window.__isLoggedIn===false||/log\s*in|sign\s*in/i.test(label))
        return {...bad('submit control asks to sign in or the native session is signed out'),signIn:true};
    if(window.__canBet===false)return bad('native account is not permitted to bet');
    const account=document.getElementById('standardized-header-account-menu'),native=window.__playSpan?.playSpan;
    if(!visibleUI(account)&&!(window.__isLoggedIn===true&&window.__canBet===true&&native?.clientKey&&native.grantType==='PlaySportsbook'))
        return bad('authenticated account evidence is missing');
    return {...leg,slip,button,status:'ready',placeText:label,placeDisabled:button.disabled||button.getAttribute('aria-disabled')==='true'};
};
const publicSlipSnapshot=()=>{
    const snapshot=exactSlipSnapshot();
    return {status:snapshot.status,reason:snapshot.reason,stagingSafe:snapshot.stagingSafe===true,
        legs:snapshot.legs.map(({selectionId,eventId,marketId,text,decimalOdds,stakeValue,suspended})=>
            ({selectionId,eventId,marketId,text,decimalOdds,stakeValue,suspended}))};
};
"""
