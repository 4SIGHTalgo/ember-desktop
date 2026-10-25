"""Fixed desktop execution viewport and non-wager control-layout diagnostics."""

from selenium.common.exceptions import WebDriverException, InvalidSessionIdException, NoSuchWindowException
from driver_lifecycle import DriverConnectionLost

WIDTH, HEIGHT = 1920, 1080
MIN_WIDTH, MIN_HEIGHT = 1200, 600

LAYOUT_SCRIPT = r"""
const visible=e=>{
    if(!e.getClientRects().length)return false;
    for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);
        if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;}
    return true;
};
const selectors={selection:'button[data-testid^="component-builder-market-button-"]',
    slipHeader:'[data-testid="betslip-header"][role="button"][aria-controls]',
    slipPanel:'[data-testid="betslip-content-wrapper"]',
    stake:'input[data-testid="betslip-wager-box-input"]',
    submit:'button[data-testid="betslip-place-bet-button"]'};
const controls={};
for(const [name,selector] of Object.entries(selectors)){
    const matches=[...document.querySelectorAll(selector)].filter(visible);
    controls[name]={selector,visibleCount:matches.length,positions:matches.slice(0,3).map(e=>{
        const r=e.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),
            width:Math.round(r.width),height:Math.round(r.height)};
    })};
}
return {headless:/HeadlessChrome/.test(navigator.userAgent),viewport:{width:innerWidth,height:innerHeight},
    controls,clickMethod:'native exact element; hit-tested element-relative fallback'};
"""


def configure_fixed_viewport(driver):
    # deviceScaleFactor=0 leaves the browser's actual scale factor unchanged.
    # This fixes layout dimensions, not location, visibility or site CSS.
    driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
        'width': WIDTH, 'height': HEIGHT, 'deviceScaleFactor': 0, 'mobile': False,
    })
    driver._tt_fixed_viewport = True


def fit_headed_window(driver):
    """Use the actual desktop bounds for visible login/location interaction."""
    driver.maximize_window()
    dimensions = driver.execute_script('return {width:innerWidth,height:innerHeight};')
    if isinstance(dimensions, dict) and (dimensions['width'] < MIN_WIDTH or dimensions['height'] < MIN_HEIGHT):
        driver.set_window_size(WIDTH, HEIGHT)
    driver._tt_native_viewport = True


def inspect_execution_layout(driver):
    try:
        report = driver.execute_script(LAYOUT_SCRIPT)
    except (DriverConnectionLost, InvalidSessionIdException, NoSuchWindowException):
        raise
    except WebDriverException:
        # Optional diagnostics must not turn a transient renderer delay into a
        # stopped follower. Live identity/auth/location checks remain separate.
        return {}
    if isinstance(report, dict):
        report['fixedViewport'] = driver.__dict__.get('_tt_fixed_viewport') is True
        report['viewportProtected'] = report['fixedViewport'] or driver.__dict__.get('_tt_native_viewport') is True
        driver._tt_browser_execution = report
    return report
