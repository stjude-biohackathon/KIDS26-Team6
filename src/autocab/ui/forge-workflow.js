(function forgeWorkflowModule(global){
  'use strict';

  const STATE_LABELS = {
    draft:'Draft', blocked:'Blocked', needs_review:'Needs review',
    approved:'Approved', packaged:'Packaged'
  };

  function stateModel(session, run, busy=false){
    if(!session){
      return {
        summary:'Select a session to view its skill workflow.',
        action:'none', label:'Create skill draft', disabled:true, current:-1
      };
    }
    if(!run){
      const ready = Boolean(session.sealed);
      return {
        summary:ready
          ? 'Ready to create a draft from sealed session evidence.'
          : 'Apply PHI redaction before creating a skill draft.',
        action:'forge', label:'Create skill draft', disabled:!ready || busy,
        current:ready ? 1 : 0, complete:ready ? 0 : -1
      };
    }
    const state = run.state;
    const unresolved = Number(run.unresolved_count) || 0;
    const models = {
      draft:{
        summary:'Draft is being prepared.', action:'view', label:'View draft',
        current:1, complete:0
      },
      blocked:{
        summary:`Draft blocked by ${unresolved} review question${unresolved === 1 ? '' : 's'}.`,
        action:'view', label:'Review draft', current:2, complete:1
      },
      needs_review:{
        summary:'Draft is ready for explicit approval.', action:'view',
        label:'Review draft', current:2, complete:1
      },
      approved:{
        summary:'Approved and ready to package.', action:'package',
        label:'Package skill', current:4, complete:3
      },
      packaged:{
        summary:'Skill package is ready.', action:'view', label:'View package',
        current:4, complete:4
      }
    };
    return {...(models[state] || models.draft), disabled:busy};
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

  function create(options){
    const {
      root, summary, stages, action, dialog, dialogTitle, dialogStatus,
      dialogContent, dialogMessage, dialogAction, dialogClose, dialogCancel,
      listRuns, createRun, loadRun, approveRun, packageRun, reviewer, notify
    } = options;
    let session = null;
    let run = null;
    let detail = null;
    let busy = false;
    let refreshing = false;
    let refreshPending = false;

    function model(){ return stateModel(session, run, busy); }

    function render(){
      const view = model();
      root.hidden = !session;
      summary.textContent = view.summary;
      action.textContent = view.label;
      action.disabled = view.disabled;
      action.title = view.disabled ? view.summary : '';
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
      if(changed){ run = null; detail = null; }
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
          run = (result.runs || [])[0] || null;
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
        for(const question of questions) list.appendChild(element('li', question.question));
        blocks.push(detailSection('Needs review', list));
      }
      if(currentRun.package_path){
        const path = element('code', currentRun.package_path);
        blocks.push(detailSection('Package', path));
      }
      dialogContent.replaceChildren(...blocks);
      dialogMessage.textContent = currentRun.state === 'blocked'
        ? 'Resolve the listed questions in the SkillSpec before approval.'
        : '';
      const actions = {
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
        render();
        notify('Skill draft created for review.', 'success');
        await open();
      }catch(error){
        notify(error.message || 'Could not create the skill draft.', 'error');
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
        run = detail.run;
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

    async function approveSkill(){
      if(!run) return;
      busy = true;
      renderDialog();
      try{
        detail = await approveRun(run.run_id, reviewer());
        run = detail.run;
        notify('Skill draft approved.', 'success');
        renderDialog();
      }catch(error){
        dialogMessage.textContent = error.message || 'Could not approve the skill draft.';
      }finally{
        busy = false;
        render();
        dialogAction.disabled = false;
      }
    }

    action.addEventListener('click', () => {
      const next = model().action;
      if(next === 'forge') createDraft();
      else if(next === 'package') packageSkill();
      else if(next === 'view') open();
    });
    dialogAction.addEventListener('click', () => {
      if(dialogAction.dataset.action === 'approve') approveSkill();
      else if(dialogAction.dataset.action === 'package') packageSkill();
    });
    for(const closeButton of [dialogClose, dialogCancel]){
      closeButton.addEventListener('click', () => dialog.close());
    }
    dialog.addEventListener('close', () => action.focus({preventScroll:true}));
    render();
    return {setSession, refresh, open};
  }

  global.WfrecForgeWorkflow = {create, stateModel};
})(window);
