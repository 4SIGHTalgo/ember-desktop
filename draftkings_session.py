"""Use DraftKings' own activity-driven heartbeat; observe renewal without secrets."""

from datetime import datetime, timezone
import json
import base64
import time
from urllib.parse import urlparse
import re

from selenium.common.exceptions import (ElementClickInterceptedException, ElementNotInteractableException,
                                        StaleElementReferenceException, WebDriverException)
from driver_lifecycle import connection_lost
from draftkings_live import retry_after_seconds


SESSION_ACTIVITY_SCRIPT = r"""
const visible = el => {
    if (!el?.getClientRects().length) return false;
    for (let node=el;node;node=node.parentElement) {
        const style=getComputedStyle(node);
        if (style.display==='none'||style.visibility==='hidden'||Number(style.opacity)===0) return false;
    }
    return true;
};
if (location.hostname!=='sportsbook.draftkings.com'||/^\/(?:auth|login)(?:\/|$)/.test(location.pathname))
    return {state:'authentication_required'};
// While wagering waits (location check, loading), only DK's idle logout is prevented: a signed-in
// session clock is clicked whatever the grant. Live 2026-10-28 DK logged out 29 min into a pause.
const waiting=arguments[0]?.waiting===true&&window.__isLoggedIn===true;
const span=window.__playSpan?.playSpan;
if (span?.grantType==='Restricted'&&!waiting) return {state:'restricted',nativeSessionPresent:true};
const authenticated=window.__isLoggedIn===true&&window.__canBet===true&&span?.clientKey&&span.grantType==='PlaySportsbook';
if (!visible(document.getElementById('standardized-header-account-menu'))&&!authenticated&&!waiting)
    return {state:'authentication_required'};
const nativeSessionPresent=!!span?.clientKey;
const canBet=window.__canBet===true,viewingOdds=!!window.__viewOdds;
if (!waiting&&(!nativeSessionPresent||!canBet||!viewingOdds))
    return {state:'native_session_not_ready',nativeSessionPresent,canBet,viewingOdds};
const clocks=[...document.querySelectorAll('span#sb-session-time')].filter(visible)
    .filter(el=>!el.closest('a,button,input,label,[role="button"],[role="tab"],[contenteditable="true"]'));
if (clocks.length!==1) return {state:'session_indicator_unavailable',nativeSessionPresent,canBet,viewingOdds};
// Return a non-wager element for a trusted WebDriver click. The site's own
// document listener records activity; its normal heartbeat performs renewal.
return {state:'ready',nativeSessionPresent,canBet,viewingOdds,element:clocks[0]};
"""


def utc():
    return datetime.now(timezone.utc).isoformat()


CLICK_HANG_BACKOFF = 300  # seconds; cleared at once when the activity click returns
CLICK_REFUSED = (ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException)


class SessionMaintenance:
    def __init__(self, interval=300):
        self.interval=min(float(interval),45) if interval>0 else 0
        self.due=self.network_due=0
        self.backoff_until=0
        self.requests={}
        self.report={'method':'native playspan heartbeat via trusted non-wager activity',
                     'activityIntervalSeconds':self.interval,'activityCount':0,'renewalCount':0,
                     'state':'waiting_for_authentication','lastRenewalAt':None,
                     'lastVerificationAt':None,'lastActivityAt':None,'sessionRejectionStatus':None,
                     'heartbeatCount':0}

    def observe_network(self, driver, now):
        if now<self.network_due: return
        self.network_due=now+15
        try:
            entries=driver.get_log('performance')
        except WebDriverException as error:
            if connection_lost(error):
                raise
            self.report['networkObservation']='unavailable'
            return
        if not isinstance(entries,list): return
        self.report['networkObservation']='enabled'
        for entry in entries:
            try:
                message=json.loads(entry['message'])['message']
                params=message['params']; identifier=params.get('requestId')
                if message['method']=='Network.requestWillBeSent':
                    request=params['request']; address=urlparse(request['url'])
                    if ((address.hostname=='draftkings.com' or (address.hostname or '').endswith('.draftkings.com'))
                            and re.fullmatch(r'/sites/[^/]+/playspans/v1/playspans/[^/]+',address.path)
                            and request['method'] in ('GET','PUT')):
                        # Never retain the URL, client key, headers, cookies or body.
                        self.requests[identifier]={'method':request['method']}
                        while len(self.requests)>64: self.requests.pop(next(iter(self.requests)))
                elif message['method']=='Network.responseReceived' and identifier in self.requests:
                    tracked=self.requests[identifier];method=tracked['method'];status=params['response']['status']
                    tracked['status']=status
                    self.report['lastHeartbeatAt']=utc()
                    self.report['heartbeatCount']+=1
                    self.report['lastHeartbeatStatus']=status
                    self.report['lastHeartbeatMethod']=method
                    if status==200 and method=='GET':
                        self.report['lastVerificationAt']=utc()
                    elif status in (401,403):
                        self.report['sessionRejectionStatus']=status
                    elif status in (429,503):
                        headers=params['response'].get('headers') or {}
                        retry=next((value for key,value in headers.items() if key.lower()=='retry-after'),None)
                        delay=retry_after_seconds({'Retry-After':retry},60)
                        self.backoff_until=now+delay
                        self.report['retryAfterSeconds']=delay
                elif message['method']=='Network.loadingFinished' and identifier in self.requests:
                    tracked=self.requests.pop(identifier)
                    if tracked.get('method')=='PUT' and tracked.get('status')==200:
                        try:
                            payload=driver.execute_cdp_cmd('Network.getResponseBody',{'requestId':identifier})
                            text=payload['body']
                            if payload.get('base64Encoded'): text=base64.b64decode(text).decode('utf-8')
                            data=json.loads(text) if len(text)<100000 else {}
                            span=data.get('playSpan') if isinstance(data,dict) else None
                            grant=span.get('grantType') if isinstance(span,dict) else None
                            if grant=='PlaySportsbook':
                                self.report.update(lastRenewalAt=utc(),renewalCount=self.report['renewalCount']+1,
                                                   sessionRejectionStatus=None,sessionRestricted=False,
                                                   renewalEvidence='native PUT 200 with PlaySportsbook grant')
                            elif grant=='Restricted':
                                self.report['sessionRestricted']=True
                            else:
                                self.report['renewalEvidence']='response did not confirm a sportsbook grant'
                        except (WebDriverException,ValueError,KeyError,TypeError) as error:
                            if connection_lost(error):
                                raise
                            self.report['renewalEvidence']='response body unavailable; renewal unverified'
                elif message['method']=='Network.loadingFailed' and identifier in self.requests:
                    self.requests.pop(identifier)
                    self.report['lastHeartbeatStatus']='network_error'
            except (ValueError,KeyError,TypeError):
                continue

    def activity(self, driver, now, auth_state, busy=False, paused=False, blocked=False, waiting=False):
        if not self.interval or now<max(self.due,self.backoff_until) or busy or blocked or auth_state!='signed-in':
            return False
        self.due=now+self.interval
        result=driver.execute_script(SESSION_ACTIVITY_SCRIPT,{'waiting':waiting is True})
        if not isinstance(result,dict):
            self.report['state']='native_session_not_ready'
            return False
        self.report.update({key:result[key] for key in ('state','nativeSessionPresent','canBet','viewingOdds') if key in result})
        element=result.get('element')
        if paused or result.get('state')!='ready' or element is None: return False
        # Live 2026-10-29: on a loaded page this click hung to the driver timeout four times in six
        # minutes, each forcing a recovery; a click that never returns holds the next one off 5 min.
        held,self.backoff_until=self.backoff_until,max(self.backoff_until,now+CLICK_HANG_BACKOFF)
        try:
            element.click()
        except CLICK_REFUSED:
            # A DK overlay over the clock is refused at once, never clicked through: retry next interval.
            self.backoff_until=held
            self.report['state']='session_indicator_covered'
            return False
        self.backoff_until=held
        self.report.update(state='activity_sent_waiting_for_native_renewal',lastActivityAt=utc(),
                           activityCount=self.report['activityCount']+1)
        return True


class AuthenticationAlerts:
    def __init__(self, repeat=300):
        self.repeat=repeat
        self.due=0
        self.alerting=False
        self.report={'reauthenticationRequired':False,'detectedAt':None,'alertCount':0}

    def update(self, required, now, notify, recovered):
        if not required:
            if self.alerting:
                recovered()
                self.report.update(reauthenticationRequired=False,recoveredAt=utc())
            self.alerting=False
            return False
        if not self.alerting:
            self.alerting=True
            self.due=0
            self.report.update(reauthenticationRequired=True,detectedAt=utc())
        if now<self.due: return False
        self.due=now+self.repeat
        notify()
        self.report['alertCount']+=1
        return True
