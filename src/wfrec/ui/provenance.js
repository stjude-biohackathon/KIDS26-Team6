(function provenanceModule(global){
  'use strict';

  const SOURCE_NAMES = {
    screen:'Screen', context:'Context', shell:'Shell', agents:'Agents', files:'Files'
  };
  const ENGINE_NAMES = {
    surrogate_guard:'Protected replacements', regex:'Pattern matching',
    gliner_onnx:'GLiNER', gliner2_pii:'GLiNER2 PII'
  };
  const REDACTION_ENGINE_NAMES = {
    regex:'Regex', gliner:'Regex + GLiNER',
    'gliner2-pii':'Regex + GLiNER2 PII'
  };
  const SESSION_STATUSES = {active:'Recording', paused:'Paused', stopped:'Archived'};
  const SHELL_BACKENDS = {devsql:'DevSQL', spool:'Spool'};

  function text(value, fallback='Not recorded'){
    if(value === null || value === undefined || value === '') return fallback;
    return String(value);
  }

  function titleCase(value){
    return text(value).replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase());
  }

  function dateTime(value){
    if(!value) return 'Not recorded';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, {
      dateStyle:'medium', timeStyle:'short'
    }).format(date);
  }

  function duration(value){
    const total = Math.max(0, Math.floor(Number(value) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    if(hours) return `${hours}h ${minutes}m ${seconds}s`;
    if(minutes) return `${minutes}m ${seconds}s`;
    return `${seconds}s`;
  }

  function joined(values, fallback='None'){
    const present = (values || []).filter(Boolean);
    return present.length ? present.join(' · ') : fallback;
  }

  function labeledCounts(counts){
    return joined(Object.entries(counts || {}).map(
      ([label, count]) => `${titleCase(label)}: ${count}`
    ));
  }

  function sourceList(sources){
    return joined(Object.entries(sources || {}).map(([name, enabled]) => {
      const label = SOURCE_NAMES[name] || titleCase(name);
      return enabled ? label : `${label} (off)`;
    }));
  }

  function statusValue(value){
    const span = document.createElement('span');
    const normalized = text(value, 'unavailable').toLowerCase();
    span.className = `provenance-status provenance-status--${normalized}`;
    span.textContent = titleCase(normalized);
    return span;
  }

  function definitionList(rows){
    const list = document.createElement('dl');
    list.className = 'provenance-list';
    for(const [label, value, options={}] of rows){
      if(options.omit && (value === null || value === undefined || value === '')) continue;
      const term = document.createElement('dt');
      const detail = document.createElement('dd');
      term.textContent = label;
      if(value instanceof Node) detail.appendChild(value);
      else {
        const target = options.code ? document.createElement('code') : detail;
        target.textContent = text(value, options.fallback);
        if(target !== detail) detail.appendChild(target);
      }
      list.append(term, detail);
    }
    return list;
  }

  function section(title, rows){
    const block = document.createElement('section');
    const heading = document.createElement('h3');
    block.className = 'provenance-section';
    heading.textContent = title;
    block.append(heading, definitionList(rows));
    return block;
  }

  function platformName(platform){
    return joined([platform.system, platform.release, platform.machine], 'Not recorded');
  }

  function versionSummary(environment){
    const current = environment.dashboard_version || '';
    const versions = [...new Set((environment.recorded_versions || []).filter(Boolean))];
    if(!versions.length) return current || 'Not recorded';
    if(versions.length === 1 && versions[0] === current) return current;
    const values = versions.map((version, index) => {
      if(index === 0 && environment.recorded_by_version) return `${version} (started)`;
      if(index === versions.length - 1 && version === current) return `${version} (current)`;
      return `${version} (resumed)`;
    });
    if(current && versions.at(-1) !== current) values.push(`${current} (current)`);
    return values.join(' · ');
  }

  function engineSection(engine){
    const block = document.createElement('div');
    const heading = document.createElement('h4');
    block.className = 'provenance-engine';
    heading.textContent = ENGINE_NAMES[engine.name] || titleCase(engine.name);
    block.append(heading, definitionList([
      ['Type', titleCase(engine.kind)],
      ['Available', engine.available === false ? 'No' : 'Yes'],
      ['Version', engine.version, {omit:true}],
      ['Model', engine.model, {omit:true}],
      ['Repository', engine.repository, {omit:true, code:true}],
      ['Revision', engine.revision, {omit:true, code:true}],
      ['Source repository', engine.source_repository, {omit:true, code:true}],
      ['Source revision', engine.source_revision, {omit:true, code:true}],
      ['Variant', engine.variant, {omit:true}],
      ['License', engine.license, {omit:true}],
      ['Weights SHA-256', engine.weights_sha256, {omit:true, code:true}],
      ['Patterns SHA-256', engine.patterns_sha256, {omit:true, code:true}],
      ['Custom deny terms', engine.deny_terms, {omit:true}],
      ['Unavailable', engine.unavailable, {omit:true}]
    ]));
    return block;
  }

  function redactionEngineName(redaction){
    if(!redaction.engine) return 'Not recorded';
    const name = REDACTION_ENGINE_NAMES[redaction.engine] || titleCase(redaction.engine);
    return redaction.engine_source === 'dashboard-default'
      ? `${name} (dashboard default)`
      : name;
  }

  function render(data, host){
    const session = data.session || {};
    const capture = data.capture || {};
    const environment = data.environment || {};
    const platform = environment.platform || {};
    const redaction = data.redaction || {};
    const integrity = redaction.integrity || {};
    const applied = redaction.status === 'applied';
    const redactionRows = [
      ['Status', statusValue(redaction.status)],
      ['Engine', redactionEngineName(redaction)]
    ];
    const integrityRows = [['Status', statusValue(integrity.status)]];
    if(applied){
      redactionRows.push(
        ['Applied', dateTime(redaction.sealed_at)],
        ['Profile', redaction.profile, {omit:true}],
        ['Assurance', redaction.assurance, {omit:true}],
        ['Rendering', redaction.render_mode, {omit:true}],
        ['Generation', redaction.generation, {omit:true}],
        ['Findings', redaction.findings, {omit:true}],
        ['Distinct values', redaction.distinct_values, {omit:true}],
        ['Masked during capture', redaction.masked_at_capture, {omit:true}],
        ['Findings by type', labeledCounts(redaction.counts_by_label)]
      );
      integrityRows.push(
        ['Events sealed through', redaction.sealed_through_seq, {omit:true}],
        ['Verified files', integrity.targets, {omit:true}],
        ['Target files', joined(redaction.target_files), {code:true}]
      );
    }
    if(integrity.detail) integrityRows.push(['Details', integrity.detail]);
    const environmentRows = [
      ['Host', environment.host, {code:true}],
      ['Platform', platformName(platform)],
      ['Python', platform.python],
      ['Display', joined([platform.session_type, platform.display_server])],
      ['AutoCAB version', versionSummary(environment)]
    ];
    const sessionRows = [
      ['ID', session.id, {code:true}],
      ['Title', session.title || 'Untitled session'],
      ['Analyst', session.analyst]
    ];
    if(session.workflow_family) sessionRows.push(['Workflow', session.workflow_family]);
    if((session.tags || []).length) sessionRows.push(['Tags', joined(session.tags)]);
    sessionRows.push(
      ['Status', SESSION_STATUSES[session.status] || titleCase(session.status)],
      ['Created', dateTime(session.created_at)],
      ['Last updated', dateTime(session.updated_at)],
      ['Events', session.events],
      ['Active time', duration(session.active_seconds)],
      ['Paused time', duration(session.paused_seconds)],
      ['Folder', session.root, {code:true}]
    );
    const blocks = [
      section('Session', sessionRows),
      section('Capture', [
        ['Sources', sourceList(capture.sources)],
        [
          'Shell backend',
          SHELL_BACKENDS[capture.shell_backend] || titleCase(capture.shell_backend)
        ],
        ['Shell output', capture.shell_output ? 'Enabled' : 'Disabled'],
        ['Watched folders', joined(capture.watch_roots), {code:true}],
        ['Remote hosts', joined(capture.remote_hosts)],
        ['Lifecycle records', capture.lifecycle_records],
        ['Source changes', capture.source_changes]
      ]),
      section('Environment', environmentRows),
      section('Redaction', redactionRows),
      section('Integrity', integrityRows)
    ];
    if((redaction.engines || []).length){
      const engines = document.createElement('section');
      const heading = document.createElement('h3');
      engines.className = 'provenance-section';
      heading.textContent = 'Redaction engines';
      engines.appendChild(heading);
      for(const engine of redaction.engines) engines.appendChild(engineSection(engine));
      blocks.push(engines);
    }
    host.replaceChildren(...blocks);
  }

  function create({button, dialog, content, load}){
    dialog.addEventListener('close', () => button.focus({preventScroll:true}));
    return {
      setSession(sessionId){ button.disabled = !sessionId; },
      close(){ dialog.close(); },
      async open(sessionId){
        if(!sessionId || button.disabled) return;
        const loading = document.createElement('p');
        loading.className = 'provenance-message';
        loading.textContent = 'Loading provenance…';
        content.replaceChildren(loading);
        dialog.showModal();
        button.setAttribute('aria-busy', 'true');
        try{
          render(await load(sessionId), content);
        }catch(error){
          const message = document.createElement('p');
          message.className = 'provenance-message provenance-message--error';
          message.textContent = error.message || 'Could not load session provenance.';
          content.replaceChildren(message);
        }finally{
          button.removeAttribute('aria-busy');
        }
      }
    };
  }

  global.WfrecProvenance = {create};
})(window);
