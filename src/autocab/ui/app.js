const THEME_STORAGE_KEY = 'wfrec-theme';
const THEME_MEDIA = window.matchMedia('(prefers-color-scheme: dark)');
let THEME_IS_MANUAL = false;

function storedTheme(){
  try{
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    if(stored === 'light' || stored === 'dark') return stored;
  }catch(_error){}
  return '';
}

function applyTheme(theme){
  document.documentElement.dataset.theme = theme;
  const toggle = document.getElementById('theme-toggle');
  if(!toggle) return;
  const dark = theme === 'dark';
  document.getElementById('theme-icon-moon').hidden = dark;
  document.getElementById('theme-icon-sun').hidden = !dark;
  const label = dark ? 'Switch to light mode' : 'Switch to dark mode';
  toggle.setAttribute('aria-label', label);
  toggle.title = label;
}

function toggleTheme(){
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  THEME_IS_MANUAL = true;
  try{ localStorage.setItem(THEME_STORAGE_KEY, next); }catch(_error){}
  applyTheme(next);
}

const savedTheme = storedTheme();
THEME_IS_MANUAL = Boolean(savedTheme);
applyTheme(savedTheme || (THEME_MEDIA.matches ? 'dark' : 'light'));
THEME_MEDIA.addEventListener('change', event => {
  if(!THEME_IS_MANUAL) applyTheme(event.matches ? 'dark' : 'light');
});

const TOKEN = document.querySelector('meta[name="wfrec-token"]').content;
const H = {'Content-Type':'application/json','Authorization':'Bearer '+TOKEN};
const SOURCE_HELP = {
  screen:'Screenshots, OCR, and window titles',
  context:'Notes and pasted context. Clipboard is not watched.',
  shell:'Commands, exit codes, and durations',
  agents:'Agent transcripts',
  files:'Git-verified file changes in declared roots'
};
const SOURCE_DETAILS = {
  agents:'Codex, Claude Code, Copilot, and Cursor transcripts'
};
const SOURCE_NAMES = {
  screen:'Screen', context:'Context', shell:'Shell', agents:'Agents', files:'Files'
};
const EVENT_NAMES = {
  'session.created':'Session created',
  'session.started':'Recording started',
  'session.paused':'Recording paused',
  'session.resumed':'Recording resumed',
  'session.stopped':'Session archived',
  'session.preempted':'Session switched',
  'session.waiting':'Waiting',
  'source.enabled':'Capture source enabled',
  'source.disabled':'Capture source disabled',
  'shell.command.completed':'Command completed',
  'screen.frame':'Screenshot captured',
  'screen.ocr':'Screen text captured',
  'screen.window':'Active window changed',
  'screen.recording.started':'Screen recording started',
  'screen.recording.stopped':'Screen recording stopped',
  'context.note':'Note added',
  'agent.message':'Agent message',
  'agent.adapter.failed':'Agent capture issue',
  'file.changed':'File changed',
  'file.diff':'File diff captured',
  'file.flood':'Large file change detected',
  'git.snapshot':'Git snapshot',
  'job.submitted':'Job submitted',
  'job.completed':'Job completed',
  'marker.user':'Marker added',
  'deid.sealed':'Session sealed',
  'collector.error':'Capture issue'
};
const TOOL_NAMES = {
  codex:'Codex', 'devsql-codex':'Codex', 'claude-code':'Claude Code',
  copilot:'Copilot', cursor:'Cursor'
};
const SESSION_STATUS_LABELS = {
  active:'Recording', paused:'Paused', stopped:'Archived', idle:'No session'
};
const EVENT_PAGE_SIZE = 25;
const DASHBOARD_PAGE_SIZE = 2000;
const EXPANDED_EVENTS = new Set();
const DASHBOARD_CACHE = new Map();
let STATE = null;
let SESSION_LIST = [];
let SESSION_SIGNATURE = '';
let SELECTED_SESSION = '';
let CREATING_SESSION = false;
let RENAMING_SESSION = false;
let SHOW_ALL_ARCHIVED = false;
let ANALYST_INITIALIZED = false;
let CONNECTED = true;
let EVENT_LIMIT = EVENT_PAGE_SIZE;
let EVENT_SESSION = '';
let STATE_RECEIVED_AT = performance.now();
let EVENT_SIGNATURE = '';
let EVENT_REFRESH_IN_PROGRESS = false;
let EVENT_REFRESH_PENDING = false;
let SOURCE_SIGNATURE = '';
let LIVE_UPDATES = true;
let EXPORT_IN_PROGRESS = false;
let PENDING_EXPORT = null;
const ACTIVITY_DASHBOARD = window.WfrecActivityDashboard.create({
  root:document.getElementById('activity-dashboard'),
  eventLabel:type => EVENT_NAMES[type] || readableEventName(type),
  toolLabel:tool => TOOL_NAMES[tool] || cleanText(tool || 'Agent', 30),
  windowLabel:payload => normalizeText(payload.app)
    || normalizeText(payload.window_title)
    || 'Unknown',
  timeLabel:eventTime,
  dateTimeLabel:eventDateTime
});
const PROVENANCE = window.WfrecProvenance.create({
  button:document.getElementById('provenance-button'),
  dialog:document.getElementById('provenance-dialog'),
  content:document.getElementById('provenance-content'),
  load:sessionId => api(`/sessions/${encodeURIComponent(sessionId)}/provenance`)
});
const SETTINGS = window.WfrecSettings.create({
  button:document.getElementById('settings-button'),
  dialog:document.getElementById('settings-dialog'),
  form:document.getElementById('settings-form'),
  input:document.getElementById('default-analyst'),
  message:document.getElementById('settings-message'),
  load:() => api('/settings'),
  save:settings => api('/settings', settings, 'PATCH'),
  onSaved:settings => {
    if(STATE) STATE.default_analyst = settings.default_analyst;
    document.getElementById('analyst').value = settings.default_analyst;
    flash('Settings saved.', 'success');
  }
});
const SESSION_METADATA = window.WfrecSessionMetadata.create({
  root:document.getElementById('session-metadata'),
  save:async (sessionId, changes) => {
    try{
      await api(
        `/sessions/${encodeURIComponent(sessionId)}/metadata`,
        changes,
        'PATCH'
      );
      await refresh();
      flash('Session details updated.', 'success');
    }catch(error){
      flash(error.message, 'error');
      throw error;
    }
  }
});
const FORGE_WORKFLOW = window.WfrecForgeWorkflow.create({
  root:document.getElementById('forge-workflow'),
  summary:document.getElementById('forge-workflow-summary'),
  stages:document.getElementById('forge-workflow-stages'),
  action:document.getElementById('forge-workflow-action'),
  dialog:document.getElementById('forge-dialog'),
  dialogTitle:document.getElementById('forge-dialog-title'),
  dialogStatus:document.getElementById('forge-dialog-status'),
  dialogContent:document.getElementById('forge-dialog-content'),
  dialogMessage:document.getElementById('forge-dialog-message'),
  dialogAction:document.getElementById('forge-dialog-action'),
  dialogClose:document.getElementById('forge-dialog-close'),
  dialogCancel:document.getElementById('forge-dialog-cancel'),
  listRuns:sessionId => api(`/sessions/${encodeURIComponent(sessionId)}/forge-runs`),
  createRun:sessionId => api(
    `/sessions/${encodeURIComponent(sessionId)}/forge-runs`, {}, 'POST'
  ),
  loadRun:runId => api(`/forge-runs/${encodeURIComponent(runId)}`),
  approveRun:(runId, reviewer) => api(
    `/forge-runs/${encodeURIComponent(runId)}/approve`, {reviewer}, 'POST'
  ),
  packageRun:runId => api(`/forge-runs/${encodeURIComponent(runId)}/package`, {}, 'POST'),
  reviewer:() => STATE && STATE.default_analyst || 'unknown-analyst',
  notify:(message, kind) => flash(message, kind)
});

async function api(path, body, method){
  const requestMethod = method || (body ? 'POST' : 'GET');
  const res = await fetch(path, {method:requestMethod, headers:H,
                                body: body?JSON.stringify(body):undefined});
  if(!res.ok){
    let message = `Request failed (${res.status})`;
    try{ message = (await res.json()).detail || message; }catch(_error){}
    throw new Error(message);
  }
  return res.json();
}

function pastedFlag(el){ return el.dataset.pasted === '1'; }
document.getElementById('note').addEventListener('paste', e => {
  // Use the paste event rather than reading the clipboard: it needs no
  // permission, works in every webview, and only ever sees what the user
  // deliberately pasted into this box.
  e.currentTarget.dataset.pasted = '1';
});

async function act(kind){
  try{
    if(kind === 'start'){
      STATE = await api('/sessions/start', {
        title: document.getElementById('title').value,
        analyst: document.getElementById('analyst').value,
        workflow_family: document.getElementById('workflow-family').value.trim(),
        tags:sessionTags(document.getElementById('session-tags').value)
      });
      SELECTED_SESSION = STATE.session.id;
      CREATING_SESSION = false;
      document.getElementById('title').value = '';
      document.getElementById('workflow-family').value = '';
      document.getElementById('session-tags').value = '';
    } else if(kind === 'resume'){
      STATE = await api('/sessions/resume', {session_id:SELECTED_SESSION});
    } else if(kind === 'stop'){
      STATE = await api('/sessions/stop', {session_id:SELECTED_SESSION});
    }
    await refresh();
    await refreshLog();
    const focusTarget = {
      start:'pause-session', resume:'pause-session', stop:'trash-session'
    }[kind];
    if(focusTarget) focusElement(focusTarget);
  }catch(e){ flash(e.message, 'error'); }
}

function startSession(event){
  event.preventDefault();
  act('start');
}

function sessionTags(value){
  return [...new Set(String(value).split(',').map(tag => tag.trim()).filter(Boolean))];
}

function showNewSessionForm(){
  clearFeedback();
  CREATING_SESSION = true;
  RENAMING_SESSION = false;
  render();
  document.getElementById('title').focus();
}

function cancelNewSession(){
  CREATING_SESSION = false;
  render();
  focusElement('new-session');
}

function showRenameSessionForm(){
  const session = STATE && STATE.session;
  if(!session || session.sealed) return;
  clearFeedback();
  RENAMING_SESSION = true;
  const input = document.getElementById('session-title-input');
  input.value = session.title || '';
  input.setCustomValidity('');
  renderSessionTitleEditor(session);
  input.focus();
  input.select();
}

function cancelRenameSession(){
  RENAMING_SESSION = false;
  const session = STATE && STATE.session;
  renderSessionTitleEditor(session);
  focusElement('rename-session');
}

function handleRenameKeydown(event){
  if(event.key === 'Escape'){
    event.preventDefault();
    cancelRenameSession();
  }
}

async function renameSession(event){
  event.preventDefault();
  const input = document.getElementById('session-title-input');
  const title = input.value.trim();
  if(!title){
    input.setCustomValidity('Enter a session title.');
    input.reportValidity();
    return;
  }
  input.setCustomValidity('');
  try{
    await api(
      `/sessions/${encodeURIComponent(SELECTED_SESSION)}`,
      {title},
      'PATCH'
    );
    RENAMING_SESSION = false;
    await refresh();
    flash('Session renamed.', 'success');
    focusElement('rename-session');
  }catch(e){ flash(e.message, 'error'); }
}

async function trashSelectedSession(){
  const session = STATE && STATE.session;
  if(!session || session.status !== 'stopped') return;
  const sessionId = SELECTED_SESSION;
  try{
    await api(`/sessions/${encodeURIComponent(sessionId)}/trash`, {}, 'POST');
    SELECTED_SESSION = '';
    SESSION_SIGNATURE = '';
    clearFeedback();
    await refresh();
    await refreshLog();
    focusElement('main-content');
  }catch(e){ flash(e.message, 'error'); }
}

function pause(){
  const dialog = document.getElementById('pause-dialog');
  const reason = document.getElementById('pause-reason');
  reason.value = '';
  dialog.showModal();
  reason.focus();
}

function closePauseDialog(){
  document.getElementById('pause-dialog').close();
}

async function confirmPause(event){
  event.preventDefault();
  const dialog = document.getElementById('pause-dialog');
  const reason = document.getElementById('pause-reason').value.trim();
  dialog.close();
  try{
    STATE = await api('/sessions/pause', {session_id:SELECTED_SESSION, reason});
    await refresh();
    await refreshLog();
    focusElement('resume-session');
  }
  catch(e){ flash(e.message, 'error'); }
}

async function toggle(name, on){
  try{
    STATE = await api('/sources/'+name, {enabled:!on});
    render();
  }
  catch(e){ flash(e.message, 'error'); }
}

async function saveNote(event){
  event.preventDefault();
  const el = document.getElementById('note');
  const text = el.value.trim();
  if(!text) return;
  try{
    await api('/notes', {text, label:document.getElementById('label').value,
                         pasted:pastedFlag(el)});
    el.value=''; el.dataset.pasted='0'; document.getElementById('label').value='';
    flash('Note added to the timeline.', 'success');
    refreshLog();
  }catch(e){ flash(e.message, 'error'); }
}

function exportMenuItems(){
  return [...document.querySelectorAll('#export-format-menu [role="menuitem"]')];
}

function openExportMenu(focusPosition='first'){
  const menu = document.getElementById('export-format-menu');
  const toggle = document.getElementById('export-menu-toggle');
  if(toggle.disabled) return;
  menu.hidden = false;
  toggle.setAttribute('aria-expanded', 'true');
  const items = exportMenuItems();
  const target = focusPosition === 'last' ? items.at(-1) : items[0];
  if(target) target.focus();
}

function closeExportMenu({restoreToggle=false}={}){
  const menu = document.getElementById('export-format-menu');
  const toggle = document.getElementById('export-menu-toggle');
  menu.hidden = true;
  toggle.setAttribute('aria-expanded', 'false');
  if(restoreToggle) restoreFocus(toggle);
}

function toggleExportMenu(){
  const menu = document.getElementById('export-format-menu');
  if(menu.hidden) openExportMenu();
  else closeExportMenu({restoreToggle:true});
}

function handleExportToggleKeydown(event){
  if(event.key === 'ArrowDown' || event.key === 'ArrowUp'){
    event.preventDefault();
    openExportMenu(event.key === 'ArrowUp' ? 'last' : 'first');
  } else if(event.key === 'Escape'){
    event.preventDefault();
    closeExportMenu({restoreToggle:true});
  }
}

function handleExportMenuKeydown(event){
  const items = exportMenuItems();
  const current = items.indexOf(document.activeElement);
  let next = null;
  if(event.key === 'ArrowDown') next = (current + 1) % items.length;
  else if(event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length;
  else if(event.key === 'Home') next = 0;
  else if(event.key === 'End') next = items.length - 1;
  else if(event.key === 'Escape'){
    event.preventDefault();
    closeExportMenu({restoreToggle:true});
    return;
  } else if(event.key === 'Tab'){
    closeExportMenu();
    return;
  }
  if(next !== null){
    event.preventDefault();
    items[next].focus();
  }
}

function selectExportFormat(formats, label){
  closeExportMenu({restoreToggle:true});
  exportSession(formats, label);
}

function exportSession(formats, label){
  PENDING_EXPORT = {formats, label};
  document.getElementById('export-dialog').showModal();
}

function clearPendingExport(){
  PENDING_EXPORT = null;
}

function closeExportDialog(){
  clearPendingExport();
  document.getElementById('export-dialog').close();
}

async function confirmExport(event){
  event.preventDefault();
  const request = PENDING_EXPORT;
  clearPendingExport();
  document.getElementById('export-dialog').close();
  if(!request) return;
  await writeSessionExport(request.formats, request.label);
}

async function writeSessionExport(formats, label){
  EXPORT_IN_PROGRESS = true;
  renderExportControls();
  try{
    const r = await api('/export', {
      formats, session_id:SELECTED_SESSION, choose_destination:true
    });
    if(r.cancelled){
      flash('Export canceled.');
      return;
    }
    const paths = r.written || [];
    const destinations = paths.join('\n');
    const message = paths.length === 1
      ? `Exported ${label}: ${destinations}`
      : `Exported ${paths.length} session files:\n${destinations}`;
    flash(message, 'success');
  }catch(e){ flash(e.message, 'error'); }
  finally{
    EXPORT_IN_PROGRESS = false;
    renderExportControls();
  }
}

document.addEventListener('pointerdown', event => {
  const control = document.getElementById('export-control');
  const menu = document.getElementById('export-format-menu');
  if(!menu.hidden && !control.contains(event.target)) closeExportMenu();
});

async function copySessionPath(){
  const path = STATE && STATE.session && STATE.session.root;
  if(!path) return;
  try{
    await navigator.clipboard.writeText(path);
    flash('Session path copied.', 'success');
  }catch(_error){
    flash(`Session folder:\n${path}`, 'error');
  }
}

async function openSessionFolder(){
  if(!STATE || !STATE.session) return;
  try{
    await api(
      `/sessions/${encodeURIComponent(STATE.session.id)}/open-folder`,
      {},
      'POST'
    );
    flash('Opened the session folder.', 'success');
  }catch(error){
    flash(error.message, 'error');
  }
}

function focusElement(id){
  const element = document.getElementById(id);
  if(element && !element.hidden && !element.disabled) element.focus();
}

function restoreFocus(element){
  if(element) element.focus({preventScroll:true});
}

function toggleLiveUpdates(){
  LIVE_UPDATES = !LIVE_UPDATES;
  const button = document.getElementById('live-updates');
  const label = document.getElementById('live-updates-label');
  button.setAttribute('aria-pressed', String(LIVE_UPDATES));
  button.classList.toggle('paused', !LIVE_UPDATES);
  button.title = LIVE_UPDATES
    ? 'Pause dashboard updates. Recording will continue.'
    : 'Resume dashboard updates.';
  label.textContent = LIVE_UPDATES ? 'Live updates' : 'Updates paused';
  if(LIVE_UPDATES) refresh().then(refreshLog);
}

function openProvenance(){
  PROVENANCE.open(SELECTED_SESSION);
}

function closeProvenance(){
  PROVENANCE.close();
}

function openSettings(){
  SETTINGS.open();
}

function closeSettings(){
  SETTINGS.close();
}

function flash(message, kind=''){
  const feedback = document.getElementById('feedback');
  feedback.textContent = message;
  feedback.className = `feedback ${kind}`;
}

function clearFeedback(){
  const feedback = document.getElementById('feedback');
  feedback.textContent = '';
  feedback.className = 'feedback';
}

function sourceDetail(name, col){
  const extra = col.extra || {};
  const details = [];
  if(name === 'shell' && extra.provider) details.push('provider: '+extra.provider);
  if(name === 'agents' && extra.detected){
    const active = Object.entries(extra.detected)
      .filter(([, available]) => available)
      .map(([provider]) => provider);
    if(active.length) details.push('providers: '+active.join(', '));
  }
  if((extra.failed || []).length) details.push('failed: '+extra.failed.join(', '));
  if(col.detail) details.push(col.detail);
  return details.join(' · ');
}

function sourceSummary(name, col, unavailable){
  const statusDetail = sourceDetail(name, col);
  if(unavailable) return statusDetail || 'Unavailable';

  const summary = [SOURCE_HELP[name]];
  const extra = col.extra || {};
  if(name === 'shell' && extra.provider){
    summary.push(titleCase(extra.provider));
  }
  if(name === 'agents' && extra.detected){
    const availableCount = Object.values(extra.detected).filter(Boolean).length;
    if(availableCount){
      summary.push(`${availableCount} provider${availableCount === 1 ? '' : 's'} available`);
    }
  }
  return summary.filter(Boolean).join(' · ');
}

function appendInlineCode(parent, value){
  for(const part of String(value).split(/(`[^`]+`)/g)){
    if(!part) continue;
    if(part.startsWith('`') && part.endsWith('`')){
      const code = document.createElement('code');
      code.textContent = part.slice(1, -1);
      parent.appendChild(code);
    } else {
      parent.appendChild(document.createTextNode(part));
    }
  }
}

function renderExportControls(){
  const session = STATE && STATE.session;
  const control = document.getElementById('export-control');
  const primary = document.getElementById('export-events');
  const toggle = document.getElementById('export-menu-toggle');
  const exportable = session
    && (session.status === 'paused' || session.status === 'stopped');
  const canExport = Boolean(exportable) && !EXPORT_IN_PROGRESS;
  const help = EXPORT_IN_PROGRESS
    ? 'Preparing a pattern-checked export.'
    : session && session.status === 'active'
      ? 'Pause or archive the session before exporting events.'
      : 'Choose where to save exported events.';
  primary.disabled = !canExport;
  toggle.disabled = !canExport;
  primary.textContent = EXPORT_IN_PROGRESS ? 'Preparing…' : 'Export Session';
  primary.title = help;
  toggle.title = help;
  control.title = help;
  control.setAttribute('aria-busy', String(EXPORT_IN_PROGRESS));
  if(!canExport) closeExportMenu();
}

function render(){
  if(!STATE) return;
  STATE_RECEIVED_AT = performance.now();
  if(!ANALYST_INITIALIZED){
    const analyst = document.getElementById('analyst');
    analyst.value = analyst.value || STATE.default_analyst || '';
    ANALYST_INITIALIZED = true;
  }
  const s = STATE.session;
  PROVENANCE.setSession(s && s.id);
  SESSION_METADATA.render(s);
  FORGE_WORKFLOW.setSession(s);
  const badge = document.getElementById('badge');
  const status = s ? s.status : 'idle';
  const active = status === 'active';
  const paused = status === 'paused';
  const creating = CREATING_SESSION || !s;
  document.getElementById('session-detail').hidden = creating;
  document.getElementById('session-create').hidden = !creating;
  document.getElementById('session-content').hidden = creating;
  document.getElementById('cancel-create').hidden = !s;
  const pauseButton = document.getElementById('pause-session');
  const resumeButton = document.getElementById('resume-session');
  const archiveButton = document.getElementById('archive-session');
  const trashButton = document.getElementById('trash-session');
  pauseButton.hidden = !active;
  pauseButton.disabled = !active;
  resumeButton.hidden = !paused;
  resumeButton.disabled = !paused;
  archiveButton.disabled = !active && !paused;
  trashButton.disabled = status !== 'stopped';
  trashButton.title = status === 'stopped'
    ? 'Remove archived session from dashboard'
    : 'Archive the selected session before removing it from the dashboard';
  const noteDisabled = !active;
  document.getElementById('note').disabled = noteDisabled;
  document.getElementById('label').disabled = noteDisabled;
  document.getElementById('add-note').disabled = noteDisabled;
  badge.textContent = SESSION_STATUS_LABELS[status] || titleCase(status);
  badge.className = 'badge ui-pill '
    + (status==='active'?'active':status==='paused'?'paused':'');
  const sessionTitle = s ? (s.title || '').trim() : '';
  document.getElementById('session-title').textContent = s
    ? (sessionTitle || 'Untitled session')
    : 'No session selected';
  renderSessionTitleEditor(s);
  renderExportControls();
  const folder = document.getElementById('session-folder');
  folder.textContent = s ? sessionFolderName(s.root) : '';
  folder.title = s ? s.root : '';
  renderSessionStats();
  if(!s) showEmptyLog('Start or resume a session to see activity.');
  renderSources(active);
}

function renderSources(active){
  const host = document.getElementById('sources');
  const signature = JSON.stringify({
    active,
    sources:STATE.sources || {},
    collectors:STATE.collectors || {}
  });
  if(signature === SOURCE_SIGNATURE) return;

  const focused = host.contains(document.activeElement)
    ? document.activeElement.dataset.source
    : '';
  SOURCE_SIGNATURE = signature;
  host.replaceChildren();
  for(const [name, on] of Object.entries(STATE.sources || {})){
    const col = (STATE.collectors||{})[name] || {};
    const unavailable = col.available === false;
    const statusDetail = sourceDetail(name, col);
    const why = sourceSummary(name, col, unavailable);
    const div = document.createElement('div');
    div.className = 'src';
    const displayName = SOURCE_NAMES[name] || name;
    const meta = document.createElement('div');
    const sourceName = document.createElement('span');
    const sourceDescription = document.createElement('span');
    meta.className = 'meta';
    sourceName.className = 'name';
    sourceName.id = `source-${name}-name`;
    sourceName.textContent = displayName;
    sourceDescription.className = 'why';
    sourceDescription.id = `source-${name}-description`;
    appendInlineCode(sourceDescription, why);
    sourceDescription.title = [SOURCE_DETAILS[name], col.backend, statusDetail]
      .filter(Boolean).join(' · ');
    meta.append(sourceName, sourceDescription);
    div.appendChild(meta);
    const btn = document.createElement('button');
    btn.className='toggle'; btn.dataset.on = on ? '1':'0';
    btn.dataset.source = name;
    btn.setAttribute('role', 'switch');
    btn.setAttribute('aria-checked', String(on));
    btn.setAttribute('aria-labelledby', sourceName.id);
    btn.setAttribute('aria-describedby', sourceDescription.id);
    btn.onclick = () => toggle(name, on);
    if(!active) btn.disabled = true;
    const control = document.createElement('div');
    const switchState = document.createElement('span');
    control.className = 'source-control';
    switchState.className = 'switch-state';
    switchState.textContent = on ? 'On' : 'Off';
    switchState.setAttribute('aria-hidden', 'true');
    control.append(switchState, btn);
    div.appendChild(control);
    host.appendChild(div);
  }
  if(focused){
    const replacement = [...host.querySelectorAll('.toggle')]
      .find(button => button.dataset.source === focused);
    restoreFocus(replacement);
  }
}

function renderSessionTitleEditor(session){
  if(!session || session.sealed) RENAMING_SESSION = false;
  const editing = Boolean(session && RENAMING_SESSION);
  const renameButton = document.getElementById('rename-session');
  document.getElementById('session-title-display').hidden = editing;
  document.getElementById('session-title-form').hidden = !editing;
  renameButton.disabled = !session || session.sealed;
  renameButton.title = session && session.sealed
    ? 'Sealed sessions cannot be renamed.'
    : 'Rename session';
}

function renderSessionStats(){
  const stats = document.getElementById('stats');
  const session = STATE && STATE.session;
  if(!session){ stats.replaceChildren(); return; }
  const elapsed = Math.max(0, (performance.now() - STATE_RECEIVED_AT) / 1000);
  const active = Number(session.active_seconds) || 0;
  const paused = Number(session.paused_seconds) || 0;
  const activeNow = active + (session.status === 'active' ? elapsed : 0);
  const pausedNow = paused + (session.status === 'paused' ? elapsed : 0);
  const details = [
    sessionStat(`Analyst · ${session.analyst}`, 'analyst'),
    sessionStat(`${session.events} events`),
    sessionStat(`${elapsedDuration(activeNow)} active`),
    sessionStat(
      session.sealed ? 'PHI redaction applied' : 'PHI redaction pending',
      session.sealed ? 'phi-applied' : 'phi-pending'
    )
  ];
  if(pausedNow >= 1){
    details.push(sessionStat(`${elapsedDuration(pausedNow)} paused total`));
  }
  stats.replaceChildren(...details.filter(Boolean));
}

function phiPendingIcon(){
  const namespace = 'http://www.w3.org/2000/svg';
  const icon = document.createElementNS(namespace, 'svg');
  const path = document.createElementNS(namespace, 'path');
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.setAttribute('aria-hidden', 'true');
  icon.setAttribute('focusable', 'false');
  path.setAttribute('d', 'M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6Zm0 5v4l2 1');
  icon.appendChild(path);
  return icon;
}

function sessionStat(value, modifier=''){
  const text = normalizeText(value);
  if(!text) return null;
  const chip = document.createElement('span');
  chip.className = `ui-pill session-stat${modifier ? ` session-stat--${modifier}` : ''}`;
  if(modifier === 'phi-pending') chip.appendChild(phiPendingIcon());
  chip.appendChild(document.createTextNode(text));
  chip.title = text;
  return chip;
}

async function refresh(){
  try{
    await refreshSessions();
    const query = SELECTED_SESSION ? `?session_id=${encodeURIComponent(SELECTED_SESSION)}` : '';
    STATE = await api('/status'+query);
    render();
    await Promise.all([renderDashboard(), FORGE_WORKFLOW.refresh(STATE.session)]);
    if(!CONNECTED) flash('Recorder reconnected.', 'success');
    CONNECTED = true;
  }catch(_error){
    if(CONNECTED) flash('Recorder disconnected. Retrying…', 'error');
    CONNECTED = false;
  }
}

async function refreshSessions(){
  const result = await api('/sessions');
  SESSION_LIST = result.sessions || [];
  if(!SESSION_LIST.some(session => session.id === SELECTED_SESSION)){
    const sessions = [...SESSION_LIST].sort(sessionNewestFirst);
    const preferred = sessions.find(session => session.is_default)
      || sessions.find(session => session.status === 'active')
      || sessions.find(session => session.status === 'paused')
      || sessions[0];
    SELECTED_SESSION = preferred ? preferred.id : '';
  }
  const signature = JSON.stringify({
    selected:SELECTED_SESSION,
    showAllArchived:SHOW_ALL_ARCHIVED,
    sessions:SESSION_LIST.map(session => [
      session.id, session.title, session.status, session.is_default, session.sealed
    ])
  });
  if(signature === SESSION_SIGNATURE){
    updateSessionNavigationMeta();
    return;
  }
  SESSION_SIGNATURE = signature;
  renderSessionNavigation();
}

function renderSessionNavigation(){
  const host = document.getElementById('session-list');
  const menu = document.getElementById('session-mobile');
  const focusedSession = host.contains(document.activeElement)
    ? document.activeElement.dataset.sessionId
    : '';
  const mobileFocused = document.activeElement === menu;
  host.replaceChildren();
  const recording = sessionsWithStatus('active');
  const paused = sessionsWithStatus('paused');
  const archived = sessionsWithStatus('stopped');
  if(!SESSION_LIST.length){
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = 'No sessions yet.';
    host.appendChild(empty);
  }
  appendSessionGroup(host, 'Recording', recording);
  appendSessionGroup(host, 'Paused', paused);
  const visibleArchived = SHOW_ALL_ARCHIVED ? archived : archived.slice(0,5);
  appendSessionGroup(host, 'Archived', visibleArchived);
  if(archived.length > 5){
    const viewAll = document.createElement('button');
    viewAll.type = 'button';
    viewAll.className = 'view-all';
    viewAll.textContent = SHOW_ALL_ARCHIVED
      ? 'Show latest 5'
      : `View all archived (${archived.length})`;
    viewAll.onclick = toggleArchivedSessions;
    host.appendChild(viewAll);
  }
  renderMobileSessionMenu(recording, paused, archived);
  if(focusedSession){
    const replacement = [...host.querySelectorAll('.session-item')]
      .find(button => button.dataset.sessionId === focusedSession);
    restoreFocus(replacement);
  } else if(mobileFocused){
    restoreFocus(menu);
  }
}

function updateSessionNavigationMeta(){
  const sessions = new Map(SESSION_LIST.map(session => [session.id, session]));
  for(const button of document.querySelectorAll('.session-item')){
    const session = sessions.get(button.dataset.sessionId);
    if(!session) continue;
    const title = session.title || 'Untitled session';
    button.setAttribute('aria-current', String(session.id === SELECTED_SESSION));
    button.setAttribute('aria-label', `${title}, ${SESSION_STATUS_LABELS[session.status]}`);
    button.querySelector('.session-meta').textContent = sessionMetaText(session);
  }
}

function sessionsWithStatus(status){
  return SESSION_LIST
    .filter(session => session.status === status)
    .sort(sessionNewestFirst);
}

function sessionNewestFirst(left, right){
  return right.id.localeCompare(left.id);
}

function appendSessionGroup(host, label, sessions){
  if(!sessions.length) return;
  const group = document.createElement('div');
  const heading = document.createElement('h3');
  group.className = 'session-group';
  heading.className = 'session-group-title';
  heading.textContent = `${label} ${sessions.length}`;
  group.appendChild(heading);
  for(const session of sessions){
    group.appendChild(sessionButton(session));
  }
  host.appendChild(group);
}

function sessionButton(session){
  const title = session.title || 'Untitled session';
  const button = document.createElement('button');
  const state = document.createElement('span');
  const text = document.createElement('span');
  const name = document.createElement('span');
  const meta = document.createElement('span');
  button.type = 'button';
  button.className = `session-item ${session.status}`;
  button.dataset.sessionId = session.id;
  button.setAttribute('aria-current', String(session.id === SELECTED_SESSION));
  button.setAttribute('aria-label', `${title}, ${SESSION_STATUS_LABELS[session.status]}`);
  button.onclick = () => selectSession(session.id);
  state.className = 'session-state';
  state.setAttribute('aria-hidden', 'true');
  state.textContent = session.status === 'active' ? '●' : session.status === 'paused' ? 'Ⅱ' : '■';
  name.className = 'session-name';
  name.textContent = title;
  meta.className = 'session-meta';
  meta.textContent = sessionMetaText(session);
  text.append(name, meta);
  button.append(state, text);
  return button;
}

function sessionMetaText(session){
  return `${session.events} events · ${sessionDuration(session.active_seconds)}`;
}

function renderMobileSessionMenu(recording, paused, archived){
  const menu = document.getElementById('session-mobile');
  menu.replaceChildren();
  if(!SESSION_LIST.length){
    const option = document.createElement('option');
    option.textContent = 'No sessions yet';
    menu.appendChild(option);
    menu.disabled = true;
    return;
  }
  menu.disabled = false;
  for(const [label, sessions] of [
    ['Recording', recording], ['Paused', paused], ['Archived', archived]
  ]){
    if(!sessions.length) continue;
    const group = document.createElement('optgroup');
    group.label = label;
    for(const session of sessions){
      const option = document.createElement('option');
      option.value = session.id;
      option.textContent = session.title || 'Untitled session';
      option.selected = session.id === SELECTED_SESSION;
      group.appendChild(option);
    }
    menu.appendChild(group);
  }
}

function toggleArchivedSessions(){
  SHOW_ALL_ARCHIVED = !SHOW_ALL_ARCHIVED;
  SESSION_SIGNATURE = '';
  renderSessionNavigation();
}

async function selectSession(sessionId){
  if(!sessionId) return;
  if(sessionId !== SELECTED_SESSION) clearFeedback();
  SELECTED_SESSION = sessionId;
  CREATING_SESSION = false;
  RENAMING_SESSION = false;
  EVENT_SESSION = '';
  EVENT_LIMIT = EVENT_PAGE_SIZE;
  await refresh();
  await refreshLog();
}

async function refreshLog(){
  if(EVENT_REFRESH_IN_PROGRESS){
    EVENT_REFRESH_PENDING = true;
    return;
  }
  EVENT_REFRESH_IN_PROGRESS = true;
  try{
    do{
      EVENT_REFRESH_PENDING = false;
      await refreshLogOnce();
    }while(EVENT_REFRESH_PENDING);
  }finally{
    EVENT_REFRESH_IN_PROGRESS = false;
  }
}

async function refreshLogOnce(){
  if(!STATE || !STATE.session){
    EVENT_SESSION = '';
    EVENT_LIMIT = EVENT_PAGE_SIZE;
    showEmptyLog('Start or resume a session to see activity.');
    return;
  }
  if(EVENT_SESSION !== STATE.session.id){
    EVENT_SESSION = STATE.session.id;
    EVENT_LIMIT = EVENT_PAGE_SIZE;
    EVENT_SIGNATURE = '';
  }
  try{
    const sessionId = STATE.session.id;
    const host = document.getElementById('log');
    const previousTop = host.scrollTop;
    const anchor = previousTop > 4 ? scrollAnchor(host) : null;
    const r = await api(
      `/sessions/${sessionId}/events?limit=${EVENT_LIMIT}&tail=true`
    );
    if(!STATE || !STATE.session || STATE.session.id !== sessionId){
      EVENT_REFRESH_PENDING = true;
      return;
    }
    const events = r.events || [];
    const total = Number(r.total || 0);
    document.getElementById('log-count').textContent = events.length
      ? `Latest ${events.length} of ${total} events`
      : '0 events';
    document.getElementById('load-older').hidden = events.length >= total;
    const signature = `${EVENT_SESSION}:${total}:${events.map(eventKey).join('|')}`;
    if(signature === EVENT_SIGNATURE) return;

    const activeElement = document.activeElement;
    const focusedEvent = host.contains(activeElement)
      ? activeElement.dataset.eventKey
      : '';
    const focusedControl = host.contains(activeElement)
      ? activeElement.dataset.eventControl
      : '';
    const logFocused = activeElement === host;
    EVENT_SIGNATURE = signature;
    delete host.dataset.emptyMessage;
    host.replaceChildren();
    if(!events.length){
      showEmptyLog('Waiting for activity…');
      EVENT_SIGNATURE = signature;
      return;
    }
    for(const event of [...events].reverse()) host.appendChild(eventRow(event));
    restoreScroll(host, anchor, previousTop);
    if(focusedEvent && focusedControl){
      const replacement = [...host.querySelectorAll('a, button')]
        .find(element => element.dataset.eventKey === focusedEvent
          && element.dataset.eventControl === focusedControl);
      restoreFocus(replacement);
    } else if(logFocused){
      restoreFocus(host);
    }
  }catch(e){}
}

async function loadOlder(){
  const button = document.getElementById('load-older');
  button.disabled = true;
  EVENT_LIMIT += EVENT_PAGE_SIZE;
  EVENT_SIGNATURE = '';
  await refreshLog();
  button.disabled = false;
}

function scrollAnchor(host){
  const top = host.scrollTop;
  const row = [...host.querySelectorAll('.event-row')]
    .find(candidate => candidate.offsetTop + candidate.offsetHeight > top);
  return row ? {key:row.dataset.eventKey, offset:row.offsetTop - top} : null;
}

function restoreScroll(host, anchor, fallback){
  if(!anchor){ host.scrollTop = 0; return; }
  const row = [...host.querySelectorAll('.event-row')]
    .find(candidate => candidate.dataset.eventKey === anchor.key);
  host.scrollTop = row ? row.offsetTop - anchor.offset : fallback;
}

function showEmptyLog(message){
  const host = document.getElementById('log');
  if(host.dataset.emptyMessage === message) return;
  const empty = document.createElement('li');
  empty.className = 'empty';
  empty.textContent = message;
  host.dataset.emptyMessage = message;
  host.replaceChildren(empty);
  EVENT_SIGNATURE = '';
  document.getElementById('log-count').textContent = '';
  document.getElementById('load-older').hidden = true;
}

function eventRow(event){
  const presentation = presentEvent(event);
  const row = document.createElement('li');
  row.className = 'event-row' + (presentation.warning ? ' warning' : '');
  row.dataset.eventKey = eventKey(event);

  const time = document.createElement('span');
  time.className = 'event-time';
  time.textContent = eventTime(event.ts);

  const main = document.createElement('div');
  main.className = 'event-main';
  const title = document.createElement('div');
  title.className = 'event-title';
  title.textContent = presentation.title;
  main.appendChild(title);
  if(presentation.detail){
    main.appendChild(eventDetail(event, presentation));
  }
  [...main.querySelectorAll('a, button')].forEach((control, index) => {
    control.dataset.eventKey = row.dataset.eventKey;
    control.dataset.eventControl = String(index);
  });
  row.append(time, main);
  return row;
}

function eventDetail(event, presentation){
  const detail = document.createElement('div');
  detail.className = 'event-detail';

  const key = eventKey(event);
  const normalizedDetail = normalizeText(presentation.detail);
  const preview = cleanText(normalizedDetail, presentation.detailLimit);
  const content = document.createElement('div');
  const accessiblePreview = document.createElement('span');
  const contentId = `event-detail-${key.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  content.className = 'event-detail-content';
  content.id = contentId;
  accessiblePreview.className = 'sr-only';
  accessiblePreview.textContent = preview;
  accessiblePreview.hidden = true;
  detail.append(content, accessiblePreview);

  function renderPlainText(value){
    content.classList.remove('formatted');
    content.classList.remove('collapsed');
    content.removeAttribute('aria-hidden');
    content.replaceChildren();
    content.textContent = value;
    accessiblePreview.hidden = true;
  }

  function renderFormattedDetail(){
    content.classList.add('formatted');
    content.replaceChildren();
    if(presentation.detailMeta){
      const metadata = document.createElement('div');
      metadata.className = 'event-detail-meta';
      metadata.textContent = presentation.detailMeta;
      content.appendChild(metadata);
    }
    const markdown = document.createElement('div');
    markdown.className = 'event-markdown';
    // This field is produced by the API's allowlist-based Markdown sanitizer.
    markdown.innerHTML = presentation.detailHtml;
    content.appendChild(markdown);
  }

  if(preview === normalizedDetail){
    if(presentation.detailHtml) renderFormattedDetail();
    else renderPlainText(preview);
    return detail;
  }

  const more = document.createElement('button');

  function renderDetail(){
    const expanded = EXPANDED_EVENTS.has(key);
    if(presentation.detailHtml){
      renderFormattedDetail();
      content.classList.toggle('collapsed', !expanded);
      if(expanded) content.removeAttribute('aria-hidden');
      else content.setAttribute('aria-hidden', 'true');
      accessiblePreview.hidden = expanded;
    } else {
      renderPlainText(expanded ? normalizedDetail : preview);
    }
    more.textContent = expanded ? 'Less' : 'More';
    more.setAttribute('aria-expanded', String(expanded));
  }

  more.type = 'button';
  more.className = 'event-more';
  more.setAttribute('aria-controls', contentId);
  more.onclick = () => {
    if(EXPANDED_EVENTS.has(key)) EXPANDED_EVENTS.delete(key);
    else EXPANDED_EVENTS.add(key);
    renderDetail();
  };
  detail.append(document.createTextNode(' '), more);
  renderDetail();
  return detail;
}

function eventKey(event){
  return `${event.session || ''}:${event.seq ?? `${event.ts}:${event.type}`}`;
}

function presentEvent(event){
  const p = event.payload || {};
  let title = EVENT_NAMES[event.type] || readableEventName(event.type);
  let detail = '';
  let detailLimit = 180;
  let detailHtml = '';
  let detailMeta = '';
  const formattedDetail = event.presentation?.detail_format === 'markdown'
    && typeof event.presentation.detail_html === 'string'
      ? event.presentation.detail_html
      : '';

  if(event.type === 'agent.message'){
    const tool = TOOL_NAMES[p.tool] || cleanText(p.tool || 'Agent', 30);
    const role = titleCase(p.role || 'message');
    title = `${tool} · ${role}`;
    detailMeta = branchDetail(p);
    detail = joinDetails([normalizeText(p.text), detailMeta]);
    detailHtml = formattedDetail;
  } else if(event.type === 'shell.command.completed'){
    detailLimit = 150;
    detail = joinDetails([
      normalizeText(p.command),
      p.exit_code !== undefined && p.exit_code !== null ? `exit ${p.exit_code}` : '',
      durationDetail(p.duration_ms),
      pathTail(p.cwd)
    ]);
  } else if(event.type === 'file.changed'){
    title = `File ${p.op || 'changed'}`;
    detail = pathTail(p.path);
  } else if(event.type === 'file.diff'){
    detail = joinDetails([pathTail(p.path), changeDetail(p)]);
  } else if(event.type === 'git.snapshot'){
    detail = joinDetails([
      p.branch ? `branch ${p.branch}` : '',
      Number.isFinite(Number(p.changed_count)) ? `${p.changed_count} changed` : '',
      p.trigger ? `trigger: ${p.trigger}` : ''
    ]);
  } else if(event.type === 'screen.ocr'){
    detail = normalizeText(p.ocr_text);
  } else if(event.type === 'screen.window'){
    detail = joinDetails([normalizeText(p.app), normalizeText(p.window_title)]);
  } else if(event.type === 'screen.frame'){
    detail = joinDetails([
      p.index ? `frame ${p.index}` : '',
      p.width && p.height ? `${p.width}×${p.height}` : ''
    ]);
  } else if(event.type === 'context.note'){
    detailMeta = normalizeText(p.label);
    detail = joinDetails([detailMeta, normalizeText(p.text)]);
    detailHtml = formattedDetail;
  } else if(event.type === 'marker.user'){
    detailLimit = 140;
    detail = joinDetails([normalizeText(p.label), normalizeText(p.detail)]);
  } else if(event.type === 'session.created'){
    detail = joinDetails([normalizeText(p.title), normalizeText(p.analyst)]);
  } else if(event.type === 'session.paused'){
    detail = joinDetails([
      normalizeText(p.reason),
      p.expect ? `expected wait: ${p.expect}` : ''
    ]);
  } else if(event.type === 'session.preempted'){
    detail = joinDetails([normalizeText(p.reason), normalizeText(p.preempted_by)]);
  } else if(event.type === 'session.waiting'){
    detail = joinDetails([normalizeText(p.reason), durationDetail(p.elapsed_ms)]);
  } else if(event.type === 'source.enabled' || event.type === 'source.disabled'){
    detail = normalizeText(p.source);
  } else if(event.type === 'job.submitted' || event.type === 'job.completed'){
    detail = joinDetails([
      p.job_id || p.JobID ? `job ${p.job_id || p.JobID}` : '',
      normalizeText(p.job_name || p.JobName),
      normalizeText(p.state || p.State)
    ]);
  } else if(event.type === 'agent.adapter.failed'){
    detailLimit = 160;
    detail = joinDetails([normalizeText(p.tool), normalizeText(p.error)]);
  } else if(event.type === 'collector.error'){
    detailLimit = 160;
    detail = joinDetails([normalizeText(p.stage), normalizeText(p.error)]);
  } else {
    detail = summarize(event);
  }

  if((event.redactions || []).length){
    const redactions = `redacted: ${event.redactions.join(', ')}`;
    detail = joinDetails([detail, redactions]);
    if(detailHtml) detailMeta = joinDetails([detailMeta, redactions]);
  }
  return {
    title,
    detail,
    detailLimit,
    detailHtml,
    detailMeta,
    warning: event.type === 'agent.adapter.failed' || event.type === 'collector.error'
  };
}

function summarize(e){
  const p = e.payload || {};
  if(p.command) return p.command.slice(0,70);
  if(p.path) return p.path.split('/').pop();
  if(p.window_title) return p.window_title.slice(0,60);
  if(p.text) return p.text.slice(0,60).replace(/\n/g,' ');
  if(p.ocr_text) return p.ocr_text.slice(0,60).replace(/\n/g,' ');
  if(p.label) return p.label;
  if(p.reason) return p.reason;
  return '';
}

function eventTime(value){
  const moment = new Date(value);
  if(Number.isNaN(moment.getTime())) return cleanText(value, 12);
  return moment.toLocaleTimeString([], {
    hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false
  });
}

function eventDateTime(value){
  const moment = new Date(value);
  if(Number.isNaN(moment.getTime())) return cleanText(value, 24);
  return moment.toLocaleString([], {
    month:'short', day:'numeric', hour:'2-digit', minute:'2-digit',
    second:'2-digit', hour12:false
  });
}

function cleanText(value, limit=120){
  const text = normalizeText(value);
  return text.length > limit ? text.slice(0, limit - 1) + '…' : text;
}

function normalizeText(value){
  if(value === undefined || value === null) return '';
  return String(value).replace(/\s+/g, ' ').trim();
}

function titleCase(value){
  const text = cleanText(value, 40);
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
}

function readableEventName(value){
  return String(value || 'Event').split('.').map(titleCase).join(' · ');
}

function joinDetails(values){ return values.filter(Boolean).join(' · '); }

function pathTail(value){
  const parts = cleanText(value, 300).split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join('/');
}

function branchDetail(payload){
  return payload.git_branch ? `branch ${payload.git_branch}` : '';
}

function durationDetail(value){
  const milliseconds = Number(value);
  if(!Number.isFinite(milliseconds) || milliseconds < 0) return '';
  return milliseconds < 1000
    ? `${Math.round(milliseconds)} ms`
    : `${(milliseconds / 1000).toFixed(1)} s`;
}

function sessionDuration(value){
  const seconds = Math.max(0, Number(value) || 0);
  if(seconds < 60) return `${Math.floor(seconds)}s active`;
  if(seconds < 3600) return `${Math.floor(seconds / 60)}m active`;
  return `${(seconds / 3600).toFixed(1)}h active`;
}

function elapsedDuration(value){
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if(hours) return `${hours}h ${minutes}m`;
  if(minutes) return `${minutes}m ${remainder}s`;
  return `${remainder}s`;
}

function sessionFolderName(value){
  const parts = String(value || '').split(/[\\/]/).filter(Boolean);
  return parts.at(-1) || '';
}

function changeDetail(payload){
  const added = payload.added !== undefined ? `+${payload.added}` : '';
  const deleted = payload.deleted !== undefined ? `-${payload.deleted}` : '';
  return [added, deleted].filter(Boolean).join(' ');
}

async function renderDashboard(){
  if(CREATING_SESSION || !STATE || !STATE.session){
    ACTIVITY_DASHBOARD.empty('Select a session to see activity.');
    return;
  }
  try{
    const sessionId = STATE.session.id;
    const events = await dashboardEvents(sessionId, Number(STATE.session.events) || 0);
    ACTIVITY_DASHBOARD.render(events, {
      isLive:STATE.session.status === 'active',
      now:Date.now()
    });
  }catch(_error){
    ACTIVITY_DASHBOARD.empty('Could not load events for this session.');
  }
}

async function dashboardEvents(sessionId, expectedTotal){
  let cached = DASHBOARD_CACHE.get(sessionId);
  if(!cached || expectedTotal < cached.events.length){
    cached = {events:[], total:expectedTotal};
    DASHBOARD_CACHE.set(sessionId, cached);
  }
  while(cached.events.length < expectedTotal){
    const offset = cached.events.length;
    const result = await api(
      `/sessions/${sessionId}/events?limit=${DASHBOARD_PAGE_SIZE}&offset=${offset}`
    );
    const page = result.events || [];
    cached.total = Number(result.total || expectedTotal);
    if(!page.length) break;
    cached.events.push(...page);
    expectedTotal = cached.total;
  }
  return cached.events.slice().sort((left, right) => {
    const timeDifference = new Date(left.ts) - new Date(right.ts);
    return timeDifference || (Number(left.seq) || 0) - (Number(right.seq) || 0);
  });
}

refresh().then(refreshLog);
setInterval(() => { if(LIVE_UPDATES) refresh(); }, 2000);
setInterval(() => { if(LIVE_UPDATES) refreshLog(); }, 1000);
setInterval(() => { if(LIVE_UPDATES) renderSessionStats(); }, 1000);
