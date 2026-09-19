'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

global.window = {};
require('../src/autocab/ui/forge-workflow.js');

const {stateModel} = window.WfrecForgeWorkflow;

test('unsealed sessions explain why drafting is unavailable', () => {
  const model = stateModel({id:'session', sealed:false}, null);

  assert.equal(model.action, 'forge');
  assert.equal(model.disabled, true);
  assert.match(model.summary, /PHI redaction/);
  assert.equal(model.current, 0);
});

test('sealed sessions can create a draft', () => {
  const model = stateModel({id:'session', sealed:true}, null);

  assert.equal(model.label, 'Create skill draft');
  assert.equal(model.disabled, false);
  assert.equal(model.complete, 0);
});

test('blocked drafts expose their unresolved question count', () => {
  const model = stateModel(
    {id:'session', sealed:true},
    {state:'blocked', unresolved_count:2}
  );

  assert.equal(model.label, 'Review draft');
  assert.equal(model.current, 2);
  assert.match(model.summary, /2 review questions/);
});

test('approval and packaging remain separate actions', () => {
  const review = stateModel(
    {id:'session', sealed:true},
    {state:'needs_review', unresolved_count:0}
  );
  const approved = stateModel(
    {id:'session', sealed:true},
    {state:'approved', unresolved_count:0}
  );

  assert.equal(review.action, 'view');
  assert.equal(review.label, 'Review draft');
  assert.equal(approved.action, 'package');
  assert.equal(approved.label, 'Package skill');
});
