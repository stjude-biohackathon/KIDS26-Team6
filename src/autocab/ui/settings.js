(function settingsModule(global){
  'use strict';

  function create({button, dialog, form, input, message, load, save, onSaved}){
    let busy = false;

    function setBusy(value){
      busy = value;
      input.disabled = value;
      form.querySelector('button[type="submit"]').disabled = value;
      button.toggleAttribute('aria-busy', value);
    }

    function showMessage(text){
      message.textContent = text || '';
    }

    async function open(){
      if(busy) return;
      showMessage('');
      dialog.showModal();
      setBusy(true);
      let loaded = false;
      try{
        const settings = await load();
        input.value = settings.default_analyst || '';
        loaded = true;
      }catch(error){
        showMessage(error.message || 'Could not load settings.');
      }finally{
        setBusy(false);
      }
      if(loaded){
        input.focus({preventScroll:true});
        input.select();
      }
    }

    async function submit(event){
      event.preventDefault();
      if(busy) return;
      showMessage('');
      setBusy(true);
      try{
        const settings = await save({default_analyst:input.value});
        input.value = settings.default_analyst || '';
        onSaved(settings);
        dialog.close();
      }catch(error){
        showMessage(error.message || 'Could not save settings.');
      }finally{
        setBusy(false);
      }
    }

    form.addEventListener('submit', submit);
    dialog.addEventListener('close', () => button.focus({preventScroll:true}));

    return {
      open,
      close(){ dialog.close(); }
    };
  }

  global.WfrecSettings = {create};
})(window);
