(function forgeWorkflowModule(global){
  'use strict';

  const STATE_LABELS = {
    draft:'Draft', blocked:'Blocked', needs_review:'Needs review',
    approved:'Approved', packaged:'Packaged'
  };
  const INPUT_OUTPUT_QUESTION = 'q-input-output-roles';
  const DEPENDENCY_QUESTION = 'q-dependency-closure';
  const RUNTIME_VERIFICATION_QUESTION = 'q-runtime-verification';
  const NO_FORMAL_IO_ASSUMPTION =
    'This skill describes an interactive work practice with no formal input or output artifacts.';
  const GENERATED_STEP_RATIONALES = new Set([
    'The command was observed, but its inputs and dependency closure need review.',
    'The activity was observed, but its inputs and dependency closure need review.'
  ]);

  function runOptionLabel(run, index){
    const state = STATE_LABELS[run.state] || run.state;
    const created = String(run.created_at || '').replace('T', ' ').replace(/\.\d+Z$/, 'Z');
    const shortId = String(run.run_id || '').slice(-8);
    const newest = index === 0 ? 'Newest · ' : '';
    return `${newest}${state} · ${created || 'Unknown time'} · ${shortId}`;
  }

  function selectRun(runs, selectedRunId){
    return runs.find(candidate => candidate.run_id === selectedRunId) || runs[0] || null;
  }

  function stateModel(session, run, busy=false, needsRepair=false){
    if(!session){
      return {
        summary:'Select a session to view its skill workflow.',
        action:'none', label:'Create skill draft', disabled:true, current:-1
      };
    }
    if(!run){
      if(needsRepair){
        return {
          summary:'PHI redaction needs repair before skill creation.',
          action:'repair',
          label:busy ? 'Applying redaction…' : 'Apply PHI redaction again',
          disabled:session.status !== 'stopped' || busy,
          current:1, complete:0
        };
      }
      const ready = Boolean(session.sealed);
      if(!ready){
        const archived = session.status === 'stopped';
        return {
          summary:archived
            ? 'PHI redaction is required before skill creation.'
            : 'Archive this session to apply PHI redaction.',
          action:'redact',
          label:busy ? 'Applying redaction…' : 'Apply PHI redaction',
          disabled:!archived || busy,
          current:archived ? 1 : 0,
          complete:archived ? 0 : -1
        };
      }
      return {
        summary:'Ready to create a draft from sealed session evidence.',
        action:'forge', label:'Create skill draft', disabled:busy,
        current:2, complete:1
      };
    }
    const state = run.state;
    const unresolved = Number(run.unresolved_count) || 0;
    const models = {
      draft:{
        summary:'Draft is being prepared.', action:'view', label:'View draft',
        current:2, complete:1
      },
      blocked:{
        summary:`Draft blocked by ${unresolved} review question${unresolved === 1 ? '' : 's'}.`,
        action:'view', label:'Review draft', current:3, complete:2
      },
      needs_review:{
        summary:'Draft is ready for explicit approval.', action:'view',
        label:'Review draft', current:3, complete:2
      },
      approved:{
        summary:'Approved and ready to package.', action:'package',
        label:'Package skill', current:5, complete:4
      },
      packaged:{
        summary:'Skill package is ready.', action:'view', label:'View package',
        current:5, complete:5
      }
    };
    return {...(models[state] || models.draft), disabled:busy};
  }

  function isSealIntegrityError(error){
    const message = String(error && error.message || '').toLowerCase();
    return message.includes('sealed digest') || message.includes('seal target set');
  }

  function answerLines(value){
    return String(value || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  }

  function roleName(description, kind, index){
    const slug = description.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    return `${kind}-${index + 1}-${slug || 'artifact'}`.slice(0, 64).replace(/-$/g, '');
  }

  function reviewEvidence(runId, suffix, summary){
    return {
      id:`review-${runId}-${suffix}`,
      source:'dashboard-review',
      locator:`forge-run:${runId}#${suffix}`,
      basis:'user_confirmed',
      confidence:'high',
      summary
    };
  }

  function stdName(name){
    return `${String(name || 'recorded-workflow').replace(/-(?:cbd|std)$/, '')}-std`;
  }

  function finalizeResolvedDefaults(spec){
    const hasBlocker = (spec.unresolvedQuestions || []).some(
      question => question.blocking === true
    );
    if(hasBlocker) return spec;
    const managedDependencies = (spec.dependencies || []).length > 0;
    spec.decision = spec.decision === 'blocked' ? 'novel' : spec.decision;
    spec.requestedPackaging = 'std';
    spec.packaging = 'std';
    spec.name = stdName(spec.name);
    spec.codebase = {roots:[]};
    spec.steps = (spec.steps || []).map(step => {
      if(step.status !== 'blocked' || !GENERATED_STEP_RATIONALES.has(step.rationale)) return step;
      return {
        ...step,
        status:managedDependencies ? 'supported' : 'manual',
        rationale:managedDependencies
          ? 'The command was observed and its dependencies were reviewed.'
          : 'The command was observed and remains a manual action in the user environment.'
      };
    });
    return spec;
  }

  function resolveInputOutputQuestion(spec, options){
    const next = JSON.parse(JSON.stringify(spec));
    next.unresolvedQuestions = (next.unresolvedQuestions || []).filter(
      question => question.id !== INPUT_OUTPUT_QUESTION
    );
    if(options.notApplicable){
      next.inputs = [];
      next.outputs = [];
      next.assumptions = [...new Set([...(next.assumptions || []), NO_FORMAL_IO_ASSUMPTION])];
      return finalizeResolvedDefaults(next);
    }

    const inputs = answerLines(options.inputs);
    const outputs = answerLines(options.outputs);
    if(!inputs.length && !outputs.length){
      throw new Error('Add at least one input or output, or choose no formal inputs or outputs.');
    }
    const evidenceId = `review-${options.runId}-input-output`;
    const evidence = reviewEvidence(
      options.runId, 'input-output', 'The reviewer defined the skill input and output roles.'
    );
    next.evidence = [
      ...(next.evidence || []).filter(record => record.id !== evidenceId),
      evidence
    ];
    next.inputs = inputs.map((description, index) => ({
      name:roleName(description, 'input', index),
      description,
      required:true,
      evidenceIds:[evidenceId]
    }));
    next.outputs = outputs.map((description, index) => ({
      name:roleName(description, 'output', index),
      description,
      required:true,
      evidenceIds:[evidenceId]
    }));
    return finalizeResolvedDefaults(next);
  }

  function runtimeConstraint(name, version){
    const cleaned = version.trim();
    return /^[<>=!~@]/.test(cleaned) ? `${name}${cleaned}` : `${name}==${cleaned}`;
  }

  function resolveDependencyQuestion(spec, options){
    const next = JSON.parse(JSON.stringify(spec));
    const resolvedQuestionIds = new Set([DEPENDENCY_QUESTION]);
    if(options.notPackaged) resolvedQuestionIds.add(RUNTIME_VERIFICATION_QUESTION);
    next.unresolvedQuestions = (next.unresolvedQuestions || []).filter(
      question => !resolvedQuestionIds.has(question.id)
    );
    if(options.notPackaged){
      next.dependencies = [];
      next.steps = (next.steps || []).map(step => ({...step, dependencies:[]}));
      next.runtimeEnvironment = {
        ...next.runtimeEnvironment,
        manager:'none', channels:[], condaDependencies:[], pipDependencies:[],
        systemDependencies:[], externalArtifacts:[], containerImage:null,
        codebaseEnvironmentFile:null, lockStrategy:'none', verified:false,
        notes:['Dependencies are supplied by the user environment and are not packaged.']
      };
      next.licenseDecision =
        'No dependencies or source code are bundled. Commands use the user environment.';
      return finalizeResolvedDefaults(next);
    }

    const reviewed = (options.dependencies || []).filter(item => item.included);
    if(!reviewed.length){
      throw new Error('Keep at least one dependency, or choose do not package dependencies.');
    }
    for(const item of reviewed){
      if(!item.version.trim()) throw new Error(`Add a version for ${item.name}.`);
      if(!['approved', 'not_applicable'].includes(item.licenseStatus)){
        throw new Error(`Choose an approved or not-applicable license status for ${item.name}.`);
      }
      if(item.licenseStatus === 'approved' && !item.license.trim()){
        throw new Error(`Add the license name for ${item.name}.`);
      }
    }

    const reviewedNames = new Set(reviewed.map(item => item.name));
    const existing = new Map((next.dependencies || []).map(item => [item.name, item]));
    const addedEvidence = reviewed.map(item => reviewEvidence(
      options.runId,
      `dependency-${roleName(item.name, 'tool', 0)}`,
      `The reviewer confirmed the version and license status for ${item.name}.`
    ));
    const evidenceByName = new Map(
      reviewed.map((item, index) => [item.name, addedEvidence[index].id])
    );
    const evidenceIds = new Set(addedEvidence.map(item => item.id));
    next.evidence = [
      ...(next.evidence || []).filter(record => !evidenceIds.has(record.id)),
      ...addedEvidence
    ];
    next.dependencies = reviewed.map(item => {
      const prior = existing.get(item.name) || {};
      const versionNote = prior.versionConstraint === item.version.trim()
        && String(prior.notes || '').includes('Detected version')
        ? prior.notes
        : 'The reviewer supplied the dependency version.';
      const licenseNote = item.license.trim()
        ? `Reviewed license: ${item.license.trim()}`
        : 'The reviewer marked licensing as not applicable.';
      return {
        ...prior,
        name:item.name,
        kind:'public_tool',
        required:true,
        evidenceIds:[...new Set([...(prior.evidenceIds || []), evidenceByName.get(item.name)])],
        install:null,
        environmentPackage:item.name,
        versionConstraint:item.version.trim(),
        sourcePath:null,
        bundlePath:null,
        licenseStatus:item.licenseStatus,
        notes:`${versionNote} ${licenseNote}`
      };
    });
    next.steps = (next.steps || []).map(step => ({
      ...step,
      dependencies:(step.dependencies || []).filter(name => reviewedNames.has(name))
    }));
    next.runtimeEnvironment = {
      ...next.runtimeEnvironment,
      manager:'system', channels:[], condaDependencies:[], pipDependencies:[],
      systemDependencies:reviewed.map(item => runtimeConstraint(item.name, item.version)),
      externalArtifacts:[], containerImage:null, codebaseEnvironmentFile:null,
      lockStrategy:'direct-pins', verified:false,
      notes:['Dependency versions were reviewed; a clean-environment run is still required.']
    };
    next.licenseDecision =
      'Retained dependencies were reviewed and no third-party source code is bundled.';
    return finalizeResolvedDefaults(next);
  }

  function element(tag, text, className=''){
    const node = document.createElement(tag);
    node.textContent = text;
    if(className) node.className = className;
    return node;
  }

  function detailSection(title, content){
    const section = document.createElement('section');
    section.appendChild(element('h3', title));
    if(content instanceof Node) section.appendChild(content);
    else section.appendChild(element('p', content));
    return section;
  }

  function packageIconButton(label, shapes){
    const button = element('button', '', 'icon-action forge-dialog__package-action');
    button.type = 'button';
    button.setAttribute('aria-label', label);
    button.title = label;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    for(const [tag, attributes] of shapes){
      const shape = document.createElementNS('http://www.w3.org/2000/svg', tag);
      for(const [name, value] of Object.entries(attributes)) shape.setAttribute(name, value);
      svg.appendChild(shape);
    }
    button.appendChild(svg);
    return button;
  }

  function labeledTextarea(label, placeholder){
    const wrapper = element('label', '', 'forge-dialog__answer-field');
    wrapper.appendChild(element('span', label));
    const input = document.createElement('textarea');
    input.rows = 2;
    input.placeholder = placeholder;
    wrapper.appendChild(input);
    return {wrapper, input};
  }

  function labeledInput(label, placeholder){
    const wrapper = element('label', '', 'forge-dialog__answer-field');
    wrapper.appendChild(element('span', label));
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = placeholder;
    wrapper.appendChild(input);
    return {wrapper, input};
  }

  function runtimeVerificationReview(spec, onSave){
    const editor = element('div', '', 'forge-dialog__runtime-editor');
    const runtime = spec.runtimeEnvironment || {};
    editor.appendChild(element(
      'p',
      `Declared runtime: ${runtime.manager || 'unknown'} · ${runtime.lockStrategy || 'unknown lock'}`,
      'forge-dialog__runtime-summary'
    ));
    const fields = element('div', '', 'forge-dialog__answer-fields');
    const result = element('label', '', 'forge-dialog__answer-field');
    result.appendChild(element('span', 'Verification result'));
    const resultSelect = document.createElement('select');
    for(const [value, label] of [
      ['not_run', 'Not run'], ['failed', 'Failed'], ['passed', 'Passed']
    ]){
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      resultSelect.appendChild(option);
    }
    result.appendChild(resultSelect);
    const environment = labeledInput(
      'Clean environment or container', 'e.g. fresh conda prefix or image digest'
    );
    const platform = labeledInput('Platform', 'e.g. linux-64');
    const smokeTest = labeledTextarea(
      'Smoke test', 'Command or manual procedure that was actually performed'
    );
    const evidenceRef = labeledInput(
      'Evidence reference (optional)', 'Log, run ID, or artifact path'
    );
    const notes = labeledTextarea(
      'Verification notes', 'Required findings, failure, or unresolved blocker'
    );
    fields.append(
      result,
      environment.wrapper,
      platform.wrapper,
      smokeTest.wrapper,
      evidenceRef.wrapper,
      notes.wrapper
    );
    const controls = element('div', '', 'forge-dialog__question-actions');
    const save = element('button', 'Record runtime verification');
    save.type = 'button';
    save.addEventListener('click', () => onSave({
      result:resultSelect.value,
      environment:environment.input.value,
      platform:platform.input.value,
      smoke_test:smokeTest.input.value,
      evidence_ref:evidenceRef.input.value,
      notes:notes.input.value
    }));
    controls.appendChild(save);
    editor.append(fields, controls);
    return editor;
  }

  function dependencyReview(spec, onSave, onSkip){
    const editor = element('div', '', 'forge-dialog__dependency-editor');
    const rows = [];
    for(const dependency of spec.dependencies || []){
      const row = element('div', '', 'forge-dialog__dependency-row');
      const include = document.createElement('input');
      include.type = 'checkbox';
      include.checked = true;
      include.setAttribute('aria-label', `Include ${dependency.name}`);
      const name = element('code', dependency.name);
      const version = document.createElement('input');
      version.type = 'text';
      version.placeholder = 'Version';
      version.value = dependency.versionConstraint || '';
      version.setAttribute('aria-label', `${dependency.name} version`);
      const license = document.createElement('input');
      license.type = 'text';
      license.placeholder = 'License name';
      license.setAttribute('aria-label', `${dependency.name} license name`);
      const status = document.createElement('select');
      status.setAttribute('aria-label', `${dependency.name} license status`);
      for(const [value, label] of [
        ['unknown', 'Unknown'], ['approved', 'Approved'],
        ['not_applicable', 'Not applicable'], ['prohibited', 'Prohibited']
      ]){
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        option.selected = dependency.licenseStatus === value;
        status.appendChild(option);
      }
      row.append(include, name, version, license, status);
      editor.appendChild(row);
      rows.push({dependency, include, version, license, status});
    }
    const controls = element('div', '', 'forge-dialog__question-actions');
    const save = element('button', 'Save dependency review');
    save.type = 'button';
    save.addEventListener('click', () => onSave(rows.map(row => ({
      name:row.dependency.name,
      included:row.include.checked,
      version:row.version.value,
      license:row.license.value,
      licenseStatus:row.status.value
    }))));
    const skip = element('button', 'Do not package dependencies');
    skip.type = 'button';
    skip.addEventListener('click', onSkip);
    controls.append(save, skip);
    editor.appendChild(controls);
    return editor;
  }

  function create(options){
    const {
      root, summary, stages, action, newRun, runSelect, dialog, dialogTitle, dialogStatus,
      dialogContent, dialogMessage, dialogAction, dialogOpen, dialogReopen,
      dialogClose, dialogCancel,
      redactionDialog, redactionForm, redactionConfirm, redactionCancel,
      listRuns, sealSession, createRun, loadRun, openSpec, openPackage,
      reviewRun, approveRun, verifyRuntime, reopenRun, packageRun,
      reviewer, notify, refreshSession
    } = options;
    let session = null;
    let run = null;
    let runs = [];
    let detail = null;
    let busy = false;
    let needsRepair = false;
    let refreshing = false;
    let refreshPending = false;

    function model(){ return stateModel(session, run, busy, needsRepair); }

    function setCurrentRun(nextRun){
      run = nextRun;
      runs = runs.map(candidate => candidate.run_id === run.run_id ? run : candidate);
    }

    function render(){
      const view = model();
      root.hidden = !session;
      summary.textContent = view.summary;
      action.textContent = view.label;
      action.disabled = view.disabled;
      action.title = view.disabled ? view.summary : '';
      newRun.hidden = !session || !session.sealed || runs.length === 0;
      newRun.disabled = busy;
      runSelect.hidden = runs.length === 0;
      runSelect.disabled = busy;
      runSelect.replaceChildren(...runs.map((candidate, index) => {
        const option = document.createElement('option');
        option.value = candidate.run_id;
        option.textContent = runOptionLabel(candidate, index);
        option.selected = Boolean(run && candidate.run_id === run.run_id);
        return option;
      }));
      for(const [index, stage] of [...stages.children].entries()){
        stage.classList.toggle('is-complete', index <= (view.complete ?? -1));
        stage.classList.toggle('is-current', index === view.current);
        if(index === view.current) stage.setAttribute('aria-current', 'step');
        else stage.removeAttribute('aria-current');
      }
    }

    function setSession(nextSession){
      const changed = (session && session.id) !== (nextSession && nextSession.id);
      session = nextSession || null;
      if(changed){ runs = []; run = null; detail = null; needsRepair = false; }
      render();
    }

    async function refresh(nextSession=session){
      setSession(nextSession);
      if(!session || busy) return;
      if(refreshing){ refreshPending = true; return; }
      refreshing = true;
      const sessionId = session.id;
      try{
        do{
          refreshPending = false;
          const result = await listRuns(sessionId);
          if(!session || session.id !== sessionId) return;
          const selectedRunId = run && run.run_id;
          runs = result.runs || [];
          run = selectRun(runs, selectedRunId);
          render();
        }while(refreshPending);
      }catch(error){
        summary.textContent = error.message || 'Could not load the skill workflow.';
      }finally{
        refreshing = false;
      }
    }

    function renderDialog(){
      const currentRun = detail && detail.run;
      const spec = detail && detail.skill_spec;
      if(!currentRun || !spec) return;
      dialogTitle.textContent = spec.title || 'Skill draft';
      dialogStatus.textContent = `${STATE_LABELS[currentRun.state] || currentRun.state} · ${currentRun.run_id}`;
      const facts = element('div', '', 'forge-dialog__facts');
      for(const value of [
        `${currentRun.evidence_count} evidence records`,
        `${(spec.steps || []).length} workflow steps`,
        `${currentRun.unresolved_count} unresolved questions`
      ]) facts.appendChild(element('span', value, 'ui-pill'));
      const blocks = [facts, detailSection('Purpose', spec.purpose)];
      const questions = spec.unresolvedQuestions || [];
      if(questions.length){
        const list = document.createElement('ul');
        for(const question of questions){
          const item = element('li', '', 'forge-dialog__question');
          item.appendChild(element('p', question.question));
          if(question.id === INPUT_OUTPUT_QUESTION){
            const fields = element('div', '', 'forge-dialog__answer-fields');
            const inputRoles = labeledTextarea(
              'Required inputs', 'One role per line, such as source repository'
            );
            const outputRoles = labeledTextarea(
              'Expected outputs', 'One role per line, such as reviewed pull request'
            );
            fields.append(inputRoles.wrapper, outputRoles.wrapper);
            const controls = element('div', '', 'forge-dialog__question-actions');
            const save = element('button', 'Save answer');
            save.type = 'button';
            save.addEventListener('click', () => answerInputOutput(
              inputRoles.input.value, outputRoles.input.value
            ));
            const notApplicable = element('button', 'No formal inputs or outputs');
            notApplicable.type = 'button';
            notApplicable.addEventListener('click', () => answerInputOutput('', '', true));
            controls.append(save, notApplicable);
            item.append(fields, controls);
          } else if(question.id === DEPENDENCY_QUESTION){
            item.appendChild(dependencyReview(
              spec,
              dependencies => answerDependencies(dependencies),
              () => answerDependencies([], true)
            ));
          } else if(question.id === RUNTIME_VERIFICATION_QUESTION){
            item.appendChild(runtimeVerificationReview(
              spec,
              verification => recordRuntimeVerification(verification)
            ));
          }
          list.appendChild(item);
        }
        blocks.push(detailSection('Needs review', list));
      }
      if(currentRun.package_path){
        const fullPath = currentRun.package_full_path || currentRun.package_path;
        const control = element('div', '', 'forge-dialog__package');
        const path = element('code', fullPath);
        path.title = fullPath;
        const actions = element('div', '', 'forge-dialog__package-actions');
        const copy = packageIconButton('Copy package path', [
          ['rect', {x:'9', y:'9', width:'11', height:'11', rx:'2'}],
          ['path', {d:'M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3'}]
        ]);
        copy.addEventListener('click', () => copyPackagePath(fullPath));
        const openFolder = packageIconButton('Open package folder', [
          ['path', {d:'M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z'}]
        ]);
        openFolder.addEventListener('click', openPackageFolder);
        actions.append(copy, openFolder);
        control.append(path, actions);
        blocks.push(detailSection('Package', control));
      }
      dialogContent.replaceChildren(...blocks);
      dialogMessage.textContent = currentRun.state === 'blocked'
        ? 'Resolve the listed questions in the SkillSpec before approval.'
        : '';
      dialogOpen.hidden = currentRun.state !== 'blocked';
      dialogOpen.disabled = busy;
      dialogReopen.hidden = currentRun.state !== 'approved';
      dialogReopen.disabled = busy;
      const actions = {
        blocked:['Validate changes', 'review'],
        needs_review:['Approve skill', 'approve'],
        approved:['Package skill', 'package']
      };
      const next = actions[currentRun.state];
      dialogAction.hidden = !next;
      dialogAction.disabled = busy;
      if(next){
        dialogAction.textContent = next[0];
        dialogAction.dataset.action = next[1];
      } else {
        delete dialogAction.dataset.action;
      }
    }

    async function open(){
      if(!run) return;
      dialogMessage.textContent = '';
      dialog.showModal();
      dialogContent.replaceChildren(element('p', 'Loading skill draft…'));
      try{
        detail = await loadRun(run.run_id);
        renderDialog();
      }catch(error){
        dialogMessage.textContent = error.message || 'Could not load the skill draft.';
      }
    }

    async function createDraft(){
      if(!session || !session.sealed) return;
      busy = true;
      render();
      try{
        detail = await createRun(session.id);
        run = detail.run;
        runs = [run, ...runs.filter(candidate => candidate.run_id !== run.run_id)];
        render();
        notify('Skill draft created for review.', 'success');
        await open();
      }catch(error){
        if(isSealIntegrityError(error)) needsRepair = true;
        notify(error.message || 'Could not create the skill draft.', 'error');
      }finally{
        busy = false;
        render();
      }
    }

    function confirmRedaction(){
      if(!session || (session.sealed && !needsRepair) || session.status !== 'stopped') return;
      redactionDialog.showModal();
      redactionConfirm.focus();
    }

    async function applyRedaction(){
      if(!session || (session.sealed && !needsRepair) || session.status !== 'stopped') return;
      const sessionId = session.id;
      const repairing = needsRepair;
      busy = true;
      render();
      try{
        await sealSession(sessionId, repairing);
        needsRepair = false;
        notify(repairing ? 'PHI redaction applied again.' : 'PHI redaction applied.', 'success');
        await refreshSession();
      }catch(error){
        notify(error.message || 'Could not apply PHI redaction.', 'error');
      }finally{
        busy = false;
        render();
      }
    }

    async function packageSkill(){
      if(!run) return;
      busy = true;
      render();
      try{
        detail = await packageRun(run.run_id);
        setCurrentRun(detail.run);
        notify('Skill package created.', 'success');
        renderDialog();
      }catch(error){
        const message = error.message || 'Could not package the skill.';
        dialogMessage.textContent = message;
        notify(message, 'error');
      }finally{
        busy = false;
        render();
      }
    }

    async function openSkillSpec(){
      if(!run || run.state !== 'blocked') return;
      dialogOpen.disabled = true;
      try{
        await openSpec(run.run_id);
        notify('Opened the SkillSpec in the default editor.', 'success');
      }catch(error){
        const message = error.message || 'Could not open the SkillSpec.';
        dialogMessage.textContent = message;
        notify(message, 'error');
      }finally{
        dialogOpen.disabled = false;
      }
    }

    async function copyPackagePath(path){
      try{
        await navigator.clipboard.writeText(path);
        notify('Package path copied.', 'success');
      }catch(_error){
        notify(`Package folder:\n${path}`, 'error');
      }
    }

    async function openPackageFolder(){
      if(!run || !run.package_path) return;
      try{
        await openPackage(run.run_id);
        notify('Opened the package folder.', 'success');
      }catch(error){
        const message = error.message || 'Could not open the package folder.';
        dialogMessage.textContent = message;
        notify(message, 'error');
      }
    }

    async function reviewSkill(skillSpec, notes=''){
      if(!run || run.state !== 'blocked') return;
      busy = true;
      renderDialog();
      try{
        detail = await reviewRun(run.run_id, reviewer(), skillSpec, notes);
        setCurrentRun(detail.run);
        const remaining = Number(run.unresolved_count) || 0;
        notify(
          remaining
            ? `Draft checked. ${remaining} review question${remaining === 1 ? '' : 's'} remain.`
            : 'SkillSpec changes validated.',
          remaining ? '' : 'success'
        );
        render();
        renderDialog();
      }catch(error){
        const message = error.message || 'Could not validate the SkillSpec.';
        dialogMessage.textContent = message;
        notify(message, 'error');
      }finally{
        busy = false;
        dialogAction.disabled = false;
        dialogOpen.disabled = false;
      }
    }

    function answerInputOutput(inputs, outputs, notApplicable=false){
      try{
        const updated = resolveInputOutputQuestion(detail.skill_spec, {
          inputs, outputs, notApplicable, runId:run.run_id
        });
        const notes = notApplicable
          ? 'Confirmed that this skill has no formal input or output artifacts.'
          : 'Defined the skill input and output roles.';
        reviewSkill(updated, notes);
      }catch(error){
        dialogMessage.textContent = error.message;
      }
    }

    function answerDependencies(dependencies, notPackaged=false){
      try{
        const updated = resolveDependencyQuestion(detail.skill_spec, {
          dependencies, notPackaged, runId:run.run_id
        });
        const notes = notPackaged
          ? 'Confirmed that dependencies are supplied by the user environment.'
          : 'Reviewed dependency versions and license status.';
        reviewSkill(updated, notes);
      }catch(error){
        dialogMessage.textContent = error.message;
      }
    }

    async function approveSkill(){
      if(!run) return;
      busy = true;
      renderDialog();
      try{
        detail = await approveRun(run.run_id, reviewer());
        setCurrentRun(detail.run);
        notify('Skill draft approved.', 'success');
        renderDialog();
      }catch(error){
        dialogMessage.textContent = error.message || 'Could not approve the skill draft.';
      }finally{
        busy = false;
        render();
        renderDialog();
      }
    }

    async function recordRuntimeVerification(verification){
      if(!run || run.state !== 'blocked') return;
      busy = true;
      renderDialog();
      try{
        detail = await verifyRuntime(run.run_id, {
          reviewer:reviewer(),
          ...verification
        });
        setCurrentRun(detail.run);
        const passed = verification.result === 'passed';
        notify(
          passed ? 'Runtime verification passed.' : 'Runtime verification recorded.',
          passed ? 'success' : ''
        );
        render();
        renderDialog();
      }catch(error){
        const message = error.message || 'Could not record runtime verification.';
        dialogMessage.textContent = message;
        notify(message, 'error');
      }finally{
        busy = false;
        render();
        renderDialog();
      }
    }

    async function reopenSkill(){
      if(!run || run.state !== 'approved' || busy) return;
      const confirmed = global.confirm(
        'Reopen this approved draft? The approval will be invalidated and a new approval will be required.'
      );
      if(!confirmed) return;
      busy = true;
      renderDialog();
      try{
        detail = await reopenRun(
          run.run_id,
          reviewer(),
          'Reopened from the dashboard to correct review or package validation issues.'
        );
        setCurrentRun(detail.run);
        notify('Skill draft reopened for review.', 'success');
        render();
        renderDialog();
      }catch(error){
        const message = error.message || 'Could not reopen the skill draft.';
        dialogMessage.textContent = message;
        notify(message, 'error');
      }finally{
        busy = false;
        render();
        renderDialog();
      }
    }

    action.addEventListener('click', () => {
      const next = model().action;
      if(next === 'redact' || next === 'repair') confirmRedaction();
      else if(next === 'forge') createDraft();
      else if(next === 'package') packageSkill();
      else if(next === 'view') open();
    });
    newRun.addEventListener('click', () => {
      if(!session || !session.sealed || busy) return;
      const confirmed = global.confirm(
        'Create a new draft from the original sealed evidence? Existing drafts and packages will be preserved.'
      );
      if(confirmed) createDraft();
    });
    runSelect.addEventListener('change', () => {
      const selected = runs.find(candidate => candidate.run_id === runSelect.value);
      if(!selected) return;
      run = selected;
      detail = null;
      render();
    });
    redactionForm.addEventListener('submit', event => {
      event.preventDefault();
      redactionDialog.close();
      applyRedaction();
    });
    redactionCancel.addEventListener('click', () => redactionDialog.close());
    redactionDialog.addEventListener('close', () => action.focus({preventScroll:true}));
    dialogAction.addEventListener('click', () => {
      if(dialogAction.dataset.action === 'review') reviewSkill();
      else if(dialogAction.dataset.action === 'approve') approveSkill();
      else if(dialogAction.dataset.action === 'package') packageSkill();
    });
    dialogOpen.addEventListener('click', openSkillSpec);
    dialogReopen.addEventListener('click', reopenSkill);
    for(const closeButton of [dialogClose, dialogCancel]){
      closeButton.addEventListener('click', () => dialog.close());
    }
    dialog.addEventListener('close', () => action.focus({preventScroll:true}));
    render();
    return {setSession, refresh, open, applyRedaction};
  }

  global.WfrecForgeWorkflow = {
    create, stateModel, isSealIntegrityError,
    resolveDependencyQuestion, resolveInputOutputQuestion, runOptionLabel, selectRun
  };
})(window);
