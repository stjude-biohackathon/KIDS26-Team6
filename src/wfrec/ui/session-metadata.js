(function sessionMetadataModule(global){
  'use strict';

  const ICONS = {
    add:'<path d="M12 5v14M5 12h14" />',
    edit:'<path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />',
    remove:'<path d="m7 7 10 10M17 7 7 17" />'
  };

  function element(tag, className='', text=''){
    const node = document.createElement(tag);
    if(className) node.className = className;
    if(text) node.textContent = text;
    return node;
  }

  function iconButton(label, icon, onClick, className='session-metadata__action'){
    const button = element('button', className);
    button.type = 'button';
    button.setAttribute('aria-label', label);
    button.title = label;
    button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICONS[icon]}</svg>`;
    button.addEventListener('click', onClick);
    return button;
  }

  function cleanTags(value){
    return [...new Set(String(value).split(',').map(tag => tag.trim()).filter(Boolean))];
  }

  function create({root, save}){
    let session = null;
    let editor = '';
    let saving = false;
    let signature = '';

    async function persist(changes, nextEditor=''){
      if(!session || saving) return;
      saving = true;
      root.setAttribute('aria-busy', 'true');
      try{
        await save(session.id, changes);
        editor = nextEditor;
      }catch(_error){
        // The dashboard owns the visible error message. Keep the editor open.
      }finally{
        saving = false;
        root.removeAttribute('aria-busy');
        renderFields();
      }
    }

    function editForm({value, placeholder, maxLength, submitLabel, onSubmit}){
      const form = element('form', 'session-metadata__form');
      const input = element('input');
      const submit = element('button', 'primary', submitLabel);
      const cancel = element('button', '', 'Cancel');
      input.value = value;
      input.placeholder = placeholder;
      input.maxLength = maxLength;
      input.setAttribute('aria-label', placeholder);
      submit.type = 'submit';
      cancel.type = 'button';
      cancel.addEventListener('click', () => {
        editor = '';
        renderFields();
      });
      input.addEventListener('keydown', event => {
        if(event.key === 'Escape'){
          event.preventDefault();
          editor = '';
          renderFields();
        }
      });
      form.addEventListener('submit', event => {
        event.preventDefault();
        onSubmit(input.value);
      });
      form.append(input, submit, cancel);
      queueMicrotask(() => input.focus());
      return form;
    }

    function field(label){
      const fieldNode = element('div', 'session-metadata__field');
      fieldNode.appendChild(element('span', 'session-metadata__label', label));
      return fieldNode;
    }

    function workflowField(){
      const fieldNode = field('Workflow');
      const content = element('div', 'session-metadata__content');
      const value = session.workflow_family || 'Not set';
      content.appendChild(element(
        'span',
        session.workflow_family
          ? 'ui-pill session-metadata__value'
          : 'session-metadata__empty',
        value
      ));
      if(!session.sealed){
        const action = session.workflow_family ? 'Edit workflow' : 'Add workflow';
        content.appendChild(iconButton(action, session.workflow_family ? 'edit' : 'add', () => {
          editor = 'workflow';
          renderFields();
        }));
      }
      fieldNode.appendChild(content);
      return fieldNode;
    }

    function removeTag(index){
      const tags = session.tags.filter((_tag, tagIndex) => tagIndex !== index);
      persist({tags});
    }

    function tagsField(){
      const fieldNode = field('Tags');
      const tags = element('div', 'session-metadata__tags');
      if(!session.tags.length){
        tags.appendChild(element('span', 'session-metadata__empty', 'Not set'));
      }
      session.tags.forEach((tag, index) => {
        const chip = element('span', 'ui-pill session-metadata__chip', tag);
        if(!session.sealed){
          chip.appendChild(iconButton(
            `Remove tag ${tag}`, 'remove', () => removeTag(index),
            'session-metadata__remove'
          ));
        }
        tags.appendChild(chip);
      });
      if(!session.sealed){
        tags.appendChild(iconButton('Add tags', 'add', () => {
          editor = 'tags';
          renderFields();
        }));
      }
      fieldNode.appendChild(tags);
      return fieldNode;
    }

    function metadataEditor(){
      let form = null;
      if(editor === 'workflow'){
        form = editForm({
          value:session.workflow_family || '',
          placeholder:'Workflow name',
          maxLength:100,
          submitLabel:'Save',
          onSubmit:value => persist({workflow_family:value.trim()})
        });
      }else if(editor === 'tags'){
        form = editForm({
          value:'',
          placeholder:'Add tags',
          maxLength:300,
          submitLabel:'Add',
          onSubmit:value => {
            const tags = [...new Set([...session.tags, ...cleanTags(value)])];
            if(tags.length === session.tags.length){
              editor = '';
              renderFields();
              return;
            }
            persist({tags});
          }
        });
      }
      if(!form) return null;
      const editorRow = element('div', 'session-metadata__editor');
      editorRow.appendChild(form);
      return editorRow;
    }

    function renderFields(){
      if(!session){
        root.replaceChildren();
        return;
      }
      const fields = [workflowField(), tagsField(), metadataEditor()].filter(Boolean);
      root.replaceChildren(...fields);
    }

    return {
      render(nextSession){
        const nextSignature = JSON.stringify([
          nextSession?.id || '', nextSession?.workflow_family || '',
          nextSession?.tags || [], Boolean(nextSession?.sealed)
        ]);
        if(nextSignature === signature) return;
        if(nextSession?.id !== session?.id) editor = '';
        signature = nextSignature;
        session = nextSession;
        renderFields();
      }
    };
  }

  global.WfrecSessionMetadata = {create};
})(window);
