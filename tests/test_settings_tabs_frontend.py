"""Check the Settings sub-tabs cover every section and switch exclusively."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.javascript_helpers import APP_JS, NODE, extract_javascript_function
from tests.test_telegram_frontend import UI_SETUP, run_javascript


INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"


def _settings_markup() -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    return html[html.index('<div id="settings-tab"') : html.index("<!-- Help Tab -->")]


def test_every_settings_section_sits_in_exactly_one_panel_with_a_tab():
    markup = _settings_markup()
    tabs = re.findall(r'data-settings-tab="([a-z]+)"', markup)
    panels = re.findall(r'id="settings-panel-([a-z]+)"', markup)
    assert tabs == panels == ["general", "mining", "games", "points", "notifications", "account"]
    chunks = re.split(r'class="settings-panel[" ]', markup)
    assert "settings-section" not in chunks[0], "section outside any panel"
    assert all("settings-section" in chunk for chunk in chunks[1:])
    # Mining benefits used to live inside the Telegram section.
    telegram = re.search(r'<section[^>]*>(?:(?!</section>).)*telegram-bot-token.*?</section>', markup, re.S)
    assert telegram and "mining-benefit-item" not in telegram.group(0)
    groups = dict(zip(panels, chunks[1:], strict=True))
    assert 'id="idle-channels-section"' in groups['points']
    assert 'id="reward-priority-section"' in groups['mining']
    assert 'id="priority-list-only"' in groups['games']
    assert 'id="select-linked-btn"' in groups['games']
    assert 'reload-campaigns-btn' in groups['games']
    assert 'id="dashboard-access-slot"' in groups['account']
    assert 'id="inventory-list-view"' not in markup and 'id="reload-btn"' not in markup


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
def test_switch_settings_tab_shows_only_the_selected_panel():
    source = APP_JS.read_text(encoding="utf-8")
    script = extract_javascript_function(source, "switchSubTab") + r"""
const assert = require('node:assert/strict');
const names = ['general', 'mining', 'games'];
const buttons = names.map(name => ({
    dataset: { settingsTab: name }, attrs: {}, classes: new Set(),
    classList: { toggle(c, on) { on ? this.owner.classes.add(c) : this.owner.classes.delete(c); } },
    setAttribute(k, v) { this.attrs[k] = v; }
}));
buttons.forEach(b => { b.classList.owner = b; });
const panels = names.map(name => ({ id: `settings-panel-${name}`, hidden: name !== 'general' }));
const document = {
    querySelectorAll: sel => sel === '[data-settings-tab]' ? buttons : panels
};
switchSubTab('settings', 'mining');
assert.deepEqual(panels.map(p => p.hidden), [true, false, true]);
assert.deepEqual(buttons.map(b => b.attrs['aria-selected']), ['false', 'true', 'false']);
assert.deepEqual(buttons.map(b => b.classes.has('active')), [false, true, false]);
"""
    result = subprocess.run([NODE, "-"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
@pytest.mark.parametrize('action', ['hours', 'telegram_save', 'telegram_test', 'discord', 'proxy', 'unlinked', 'reward', 'notification_events'])
def test_failed_save_after_leaving_its_panel_remains_visible(action):
    names = ['switchSubTab', 'toggleMiningHoursRange', 'onMiningHoursChange',
             'handleSaveTelegramClick', 'testTelegramConnection', 'saveTelegramSettings',
             'saveDiscordSettings', 'updateDiscordSettings', 'saveProxy', 'onUnlinkedMiningChange',
             'saveBooleanSetting', 'saveRewardPriority', 'onNotificationEventChange']
    source = APP_JS.read_text(encoding='utf-8')
    if 'function reportSettingsError(' in source:
        names.append('reportSettingsError')
    script = UI_SETUP + r"""
const panelNames = ['general','mining','games','points','notifications','account'];
const panels = panelNames.map(name=>({id:'settings-panel-'+name,hidden:name!=='general'}));
const buttons = panelNames.map(name=>({dataset:{settingsTab:name},classList:{toggle(){}},setAttribute(){}}));
document.querySelectorAll=selector=>selector==='[data-settings-tab]'?buttons:panels;
document.activeElement=null;
const panel = name=>panels.find(p=>p.id==='settings-panel-'+name);
const toastCalls=[];
function showToast(message,type){toastCalls.push({message,type});}
function renderRewardPriority(){}
let rewardPrioritySaving=false;
state.settings={telegram_configured:true,discord_configured:true,reward_priority:[],proxy:'',
 mining_hours:{mode:'always',start:'08:00',end:'22:00'}};
Object.assign(state.translations.gui.settings,{mining_hours_save_error:'<b>Hours failed',
 proxy_save_error:'Proxy failed',allow_unlinked_campaigns_save_error:'Unlinked failed',
 reward_priority_error:'Reward failed',discord:{save_error:'Discord failed',error:'Discord test failed'}});
for(const id of ['mining-hours-error','allow-unlinked-error','reward-priority-error'])
 document.getElementById(id).closest=()=>panel('mining');
for(const id of ['telegram-test-result','discord-test-result','notification-events-error'])
 document.getElementById(id).closest=()=>panel('notifications');
document.getElementById('proxy-verify-result').closest=()=>panel('general');
document.getElementById('mining-hours-range').checked=true;
for(const id of ['start','end'])Object.assign(document.getElementById('mining-hours-'+id),
 {value:id==='start'?'10:00':'18:00',reportValidity:()=>true});
document.getElementById('telegram-chat-id').value='42';
document.getElementById('proxy-url').value='http://sample-proxy.invalid:8080';
let release;
global.fetch=()=>new Promise(resolve=>{release=()=>resolve({ok:false,status:503,json:async()=>({success:false})});});
const actions={
 hours:{panel:'mining',result:'mining-hours-error',run:onMiningHoursChange},
 telegram_save:{panel:'notifications',result:'telegram-test-result',run:handleSaveTelegramClick},
 telegram_test:{panel:'notifications',result:'telegram-test-result',run:testTelegramConnection},
 discord:{panel:'notifications',result:'discord-test-result',run:()=>updateDiscordSettings('save')},
 proxy:{panel:'general',result:'proxy-verify-result',run:saveProxy},
 unlinked:{panel:'mining',result:'allow-unlinked-error',run:onUnlinkedMiningChange},
 reward:{panel:'mining',result:'reward-priority-error',run:()=>saveRewardPriority(['BADGE','EMOTE','DIRECT_ENTITLEMENT','UNKNOWN'])},
 notification_events:{panel:'notifications',result:'notification-events-error',
  run:()=>onNotificationEventChange({target:document.getElementById('notify-new_release')})}
};
""" + f"const action=actions[{action!r}];\n" + r"""
(async()=>{
 switchSubTab('settings',action.panel);
 const pending=action.run();
 assert.equal(typeof release,'function','the actual handler reached its asynchronous request');
 switchSubTab('settings',action.panel==='general'?'games':'general');
 release();await pending;
 const result=document.getElementById(action.result);
 assert.ok(panel(action.panel).hidden);
 assert.ok(result.textContent);
 assert.deepEqual(toastCalls,[{message:result.textContent,type:'error'}]);
})().catch(error=>{console.error(error);process.exit(1)});
"""
    run_javascript(names, script)


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
@pytest.mark.parametrize('group,names', [
    ('settings', ['general', 'mining', 'games', 'points', 'notifications', 'account']),
    ('history', ['drops', 'points'])])
def test_tab_click_and_arrow_home_end_keys_keep_one_tab_stop_and_panel(group, names):
    run_javascript(['switchSubTab', 'handleSubTabKeydown', 'initializeSubTabs'],
                   f"const group={group!r};const names={json.dumps(names)};" + r"""
const assert=require('node:assert/strict');
const buttons=names.map(name=>({dataset:{[group+'Tab']:name},attrs:{},events:{},classList:{toggle(){}},
 setAttribute(k,v){this.attrs[k]=v},addEventListener(k,fn){this.events[k]=fn},focus(){document.activeElement=this}}));
const panels=names.map(name=>({id:group+'-panel-'+name,hidden:name!==names[0]}));
// a panel of the other group must never be touched
const foreign=[{id:'settings-panel-x',hidden:false},{id:'history-panel-x',hidden:false}].filter(p=>!p.id.startsWith(group));
const document={activeElement:null,
 querySelectorAll:s=>s===`[data-${group}-tab]`?buttons:s.startsWith('[data-')?[]:[...panels,...foreign]};
initializeSubTabs(group);
const last=names.length-1;
const check=index=>{
 assert.deepEqual(panels.filter(p=>!p.hidden).map(p=>p.id),[group+'-panel-'+names[index]]);
 assert.deepEqual(buttons.filter(b=>b.attrs.tabindex==='0').map(b=>b.dataset[group+'Tab']),[names[index]]);
 assert.deepEqual(buttons.filter(b=>b.attrs['aria-selected']==='true').map(b=>b.dataset[group+'Tab']),[names[index]]);
 assert.ok(foreign.every(p=>!p.hidden));
};
buttons[last].events.click();check(last);
const key=(index,key)=>{let prevented=false;buttons[index].events.keydown({currentTarget:buttons[index],key,
 preventDefault(){prevented=true}});return prevented};
assert.ok(key(last,'ArrowRight'));check(0);assert.equal(document.activeElement,buttons[0]);
assert.ok(key(0,'ArrowLeft'));check(last);
assert.ok(key(last,'Home'));check(0);
assert.ok(key(0,'End'));check(last);
assert.equal(key(last,'Tab'),false);check(last);
switchSubTab(group,'unknown');check(last);
""")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
@pytest.mark.parametrize('locale', ['English', '简体中文', 'العربية'])
def test_tab_labels_use_translations_as_text(locale):
    translations = json.loads((INDEX_HTML.parents[1] / 'lang' / f'{locale}.json').read_text(encoding='utf-8'))
    settings = translations['gui']['settings']
    expected = [settings['general']['name'], settings['tab_mining'], settings['tab_games'],
                translations['gui']['tabs']['points'], settings['tab_notifications'], settings['tab_account']]
    run_javascript(['applyTranslations'], r"""
const assert=require('node:assert/strict');
const names=['general','mining','games','points','notifications','account'];
const buttons=Object.fromEntries(names.map(name=>['settings-tab-'+name,{textContent:''}]));
for(const button of Object.values(buttons))Object.defineProperty(button,'innerHTML',{set(){throw Error('unsafe')}});
const document={getElementById:id=>id==='settings-tab'?{querySelector:()=>null}:buttons[id]||null,
 querySelector:()=>null,querySelectorAll:()=>[]};
function translateHistory(){}
function renderGamesToWatch(){}
""" + 'applyTranslations(' + json.dumps(translations) + ');\n'
        + 'assert.deepEqual(names.map(name=>buttons["settings-tab-"+name].textContent),'
        + json.dumps(expected) + ');')


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
def test_visible_inline_failure_does_not_duplicate_a_global_notification():
    run_javascript(['reportSettingsError'], r"""
const assert=require('node:assert/strict');const calls=[];
const panel={hidden:false},tab={classList:{contains:()=>true}};
const element={textContent:'',closest:selector=>selector==='.settings-panel'?panel:tab};
function showToast(...args){calls.push(args)}
reportSettingsError(element,'Translated failure');
assert.equal(element.textContent,'Translated failure');assert.deepEqual(calls,[]);
panel.hidden=true;reportSettingsError(element,'Translated failure');
assert.deepEqual(calls,[['Translated failure','error']]);
""")


def test_history_views_are_a_tab_group_with_the_drop_and_points_content():
    html = INDEX_HTML.read_text(encoding="utf-8")
    markup = html[html.index('<div id="history-tab"') : html.index("<!-- Footer -->")]
    assert re.findall(r'data-history-tab="([a-z]+)"', markup) == ["drops", "points"]
    assert re.findall(r'id="history-panel-([a-z]+)"', markup) == ["drops", "points"]
    drops, points = markup.split('id="history-panel-points"')
    assert 'id="history-table"' in drops and 'id="history-filter-game"' in drops
    assert 'id="points-history-section"' in points and 'id="points-history-chart"' in points
    assert 'points-history' not in drops
    assert 'hidden>' in points.split("\n")[0]
    assert 'aria-selected="true" tabindex="0"' in drops.split('id="history-panel-drops"')[0]


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
def test_shortcut_buttons_open_the_settings_and_history_views_and_labels_join_existing_strings():
    run_javascript(['openSubTab', 'switchSubTab', 'switchTab', 'applyTranslations'], r"""
const assert=require('node:assert/strict');
const calls=[];
function loadHistory(){calls.push('loadHistory')}
function translateHistory(){}
function renderGamesToWatch(){}
function translatePoints(){}
const mk=(group,name)=>({dataset:{[group+'Tab']:name},attrs:{},classList:{toggle(){}},setAttribute(k,v){this.attrs[k]=v}});
const buttons={settings:[mk('settings','general'),mk('settings','points')],history:[mk('history','drops'),mk('history','points')]};
const panels=[{id:'settings-panel-general',hidden:false},{id:'settings-panel-points',hidden:true},
 {id:'history-panel-drops',hidden:false},{id:'history-panel-points',hidden:true}];
const tabs=['history','settings'].map(n=>({dataset:{tab:n},classList:{add(){},remove(){}},textContent:''}));
const content=['history','settings'].map(n=>({id:n+'-tab',classList:{add(){calls.push('show '+n)},remove(){}},querySelector:()=>null}));
const shortcuts={'points-open-settings':{textContent:''},'points-open-history':{textContent:''}};
const document={
 querySelectorAll:s=>{const m=/^\[data-(\w+)-tab\]$/.exec(s);return m?buttons[m[1]]:s==='.settings-panel'?panels:[]},
 querySelector:s=>s==='[data-tab="history"]'?tabs[0]:s==='[data-tab="settings"]'?tabs[1]:null,
 getElementById:id=>content.find(c=>c.id===id)||shortcuts[id]||(id==='settings-tab'?{querySelector:()=>null}:null)};
openSubTab('history','history','points');
assert.deepEqual(panels.map(p=>p.hidden),[false,true,true,false]);
assert.ok(calls.includes('loadHistory'));
openSubTab('settings','settings','points');
assert.deepEqual(panels.map(p=>p.hidden),[true,false,true,false]);
applyTranslations({gui:{tabs:{settings:'S',points:'P',history:'H',main:'',inventory:'',help:''},points:{title:'T'},settings:{general:{name:'G'}}}});
assert.equal(shortcuts['points-open-settings'].textContent,'S > P');
assert.equal(shortcuts['points-open-history'].textContent,'H > T');
""")
