'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

global.window = {};
require('../src/autocab/ui/forge-workflow.js');

const {
  isSealIntegrityError,
  resolveDependencyQuestion,
  resolveInputOutputQuestion,
  runOptionLabel,
  selectRun,
  stateModel
} = window.WfrecForgeWorkflow;

test('unsealed sessions explain why drafting is unavailable', () => {
  const model = stateModel({id:'session', status:'active', sealed:false}, null);

  assert.equal(model.action, 'redact');
  assert.equal(model.disabled, true);
  assert.match(model.summary, /PHI redaction/);
  assert.equal(model.current, 0);
  assert.equal(model.complete, -1);
});

test('sealed sessions can create a draft', () => {
  const model = stateModel({id:'session', sealed:true}, null);

  assert.equal(model.label, 'Create skill draft');
  assert.equal(model.disabled, false);
  assert.equal(model.current, 2);
  assert.equal(model.complete, 1);
});

test('invalid seals offer PHI redaction repair', () => {
  const model = stateModel(
    {id:'session', status:'stopped', sealed:true},
    null,
    false,
    true
  );

  assert.equal(model.action, 'repair');
  assert.equal(model.label, 'Apply PHI redaction again');
  assert.equal(model.disabled, false);
  assert.equal(model.current, 1);
  assert.equal(model.complete, 0);
});

test('sealed digest errors are recognized as repairable', () => {
  assert.equal(
    isSealIntegrityError(new Error('screen/ocr/1.txt no longer matches its sealed digest.')),
    true
  );
  assert.equal(isSealIntegrityError(new Error('Network request failed.')), false);
});

test('blocked drafts expose their unresolved question count', () => {
  const model = stateModel(
    {id:'session', sealed:true},
    {state:'blocked', unresolved_count:2}
  );

  assert.equal(model.label, 'Review draft');
  assert.equal(model.current, 3);
  assert.equal(model.complete, 2);
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

test('forge run history labels identify the newest run and its state', () => {
  const label = runOptionLabel({
    run_id:'20260920T120000Z-1234abcd',
    state:'blocked',
    created_at:'2026-09-20T12:00:00Z'
  }, 0);

  assert.equal(label, 'Newest · Blocked · 2026-09-20 12:00:00Z · 1234abcd');
});

test('forge run selection preserves history choices and defaults to newest', () => {
  const runs = [
    {run_id:'new-run', state:'blocked'},
    {run_id:'old-run', state:'packaged'}
  ];

  assert.equal(selectRun(runs, 'old-run').run_id, 'old-run');
  assert.equal(selectRun(runs, 'missing-run').run_id, 'new-run');
  assert.equal(selectRun([], 'missing-run'), null);
});

test('input and output answers become structured user-confirmed roles', () => {
  const spec = {
    assumptions:[], evidence:[], inputs:[], outputs:[],
    unresolvedQuestions:[
      {id:'q-input-output-roles'},
      {id:'q-dependency-closure'}
    ]
  };

  const updated = resolveInputOutputQuestion(spec, {
    inputs:'Source repository\nReview notes',
    outputs:'Reviewed pull request',
    notApplicable:false,
    runId:'20260919T000000Z-1234abcd'
  });

  assert.deepEqual(updated.unresolvedQuestions, [{id:'q-dependency-closure'}]);
  assert.equal(updated.inputs.length, 2);
  assert.equal(updated.outputs.length, 1);
  assert.equal(updated.inputs[0].description, 'Source repository');
  assert.equal(updated.evidence[0].basis, 'user_confirmed');
  assert.deepEqual(updated.inputs[0].evidenceIds, [updated.evidence[0].id]);
});

test('input and output questions can be marked as not applicable', () => {
  const spec = {
    assumptions:[], evidence:[], inputs:[{name:'old'}], outputs:[{name:'old'}],
    unresolvedQuestions:[
      {id:'q-input-output-roles'},
      {id:'q-dependency-closure'}
    ]
  };

  const updated = resolveInputOutputQuestion(spec, {
    inputs:'', outputs:'', notApplicable:true, runId:'run'
  });

  assert.deepEqual(updated.inputs, []);
  assert.deepEqual(updated.outputs, []);
  assert.deepEqual(updated.unresolvedQuestions, [{id:'q-dependency-closure'}]);
  assert.match(updated.assumptions[0], /interactive work practice/);
});

test('blank input and output answers require the explicit not-applicable choice', () => {
  assert.throws(
    () => resolveInputOutputQuestion(
      {assumptions:[], evidence:[], unresolvedQuestions:[]},
      {inputs:'', outputs:'', notApplicable:false, runId:'run'}
    ),
    /Add at least one input or output/
  );
});

test('dependency review keeps selected tools and removes false detections', () => {
  const spec = {
    decision:'blocked', requestedPackaging:'auto', packaging:'cbd', name:'work-cbd',
    codebase:{roots:[]}, evidence:[], licenseDecision:'Unknown',
    runtimeEnvironment:{manager:'none'},
    unresolvedQuestions:[
      {id:'q-input-output-roles', blocking:true},
      {id:'q-dependency-closure', blocking:true},
      {id:'q-runtime-verification', blocking:true}
    ],
    dependencies:[
      {
        name:'git', evidenceIds:['event-1'], versionConstraint:'2.51.0',
        notes:'Detected version 2.51.0 when the draft was created using git --version.'
      },
      {name:'dir', evidenceIds:['event-2']}
    ],
    steps:[
      {status:'blocked', rationale:'The command was observed, but its inputs and dependency closure need review.', dependencies:['git']},
      {status:'blocked', rationale:'The command was observed, but its inputs and dependency closure need review.', dependencies:['dir']}
    ]
  };

  const updated = resolveDependencyQuestion(spec, {
    runId:'20260919T000000Z-1234abcd', notPackaged:false,
    dependencies:[
      {name:'git', included:true, version:'2.51.0', license:'GPL-2.0-only', licenseStatus:'approved'},
      {name:'dir', included:false, version:'', license:'', licenseStatus:'unknown'}
    ]
  });

  assert.deepEqual(updated.dependencies.map(item => item.name), ['git']);
  assert.deepEqual(updated.steps[1].dependencies, []);
  assert.equal(updated.runtimeEnvironment.manager, 'system');
  assert.deepEqual(updated.runtimeEnvironment.systemDependencies, ['git==2.51.0']);
  assert.match(updated.dependencies[0].notes, /Detected version 2\.51\.0/);
  assert.match(updated.dependencies[0].notes, /Reviewed license: GPL-2\.0-only/);
  assert.deepEqual(updated.unresolvedQuestions, [
    {id:'q-input-output-roles', blocking:true},
    {id:'q-runtime-verification', blocking:true}
  ]);
  assert.equal(updated.steps[0].status, 'blocked');
});

test('user-managed dependencies create a manual review-ready skill', () => {
  const spec = {
    decision:'blocked', requestedPackaging:'auto', packaging:'cbd', name:'work-cbd',
    codebase:{roots:[]}, evidence:[], licenseDecision:'Unknown',
    runtimeEnvironment:{manager:'none'}, unresolvedQuestions:[
      {id:'q-dependency-closure', blocking:true},
      {id:'q-runtime-verification', blocking:true}
    ],
    dependencies:[{name:'git', evidenceIds:['event-1']}],
    steps:[{
      status:'blocked',
      rationale:'The command was observed, but its inputs and dependency closure need review.',
      dependencies:['git']
    }]
  };

  const updated = resolveDependencyQuestion(spec, {
    runId:'run', dependencies:[], notPackaged:true
  });

  assert.deepEqual(updated.dependencies, []);
  assert.deepEqual(updated.steps[0].dependencies, []);
  assert.equal(updated.steps[0].status, 'manual');
  assert.equal(updated.decision, 'novel');
  assert.equal(updated.packaging, 'std');
  assert.equal(updated.name, 'work-std');
  assert.deepEqual(updated.unresolvedQuestions, []);
});

test('review resolves current and legacy generated activity rationales', () => {
  const spec = {
    decision:'blocked', requestedPackaging:'auto', packaging:'cbd', name:'work-cbd',
    codebase:{roots:[]}, evidence:[], licenseDecision:'Unknown',
    runtimeEnvironment:{manager:'none'}, dependencies:[],
    unresolvedQuestions:[
      {id:'q-dependency-closure', blocking:true},
      {id:'q-runtime-verification', blocking:true}
    ],
    steps:[
      {
        status:'blocked', dependencies:[],
        rationale:'The command was observed, but its inputs and dependency closure need review.'
      },
      {
        status:'blocked', dependencies:[],
        rationale:'The activity was observed, but its inputs and dependency closure need review.'
      }
    ]
  };

  const updated = resolveDependencyQuestion(spec, {
    runId:'run', dependencies:[], notPackaged:true
  });

  assert.deepEqual(updated.steps.map(step => step.status), ['manual', 'manual']);
  assert.deepEqual(updated.unresolvedQuestions, []);
});

test('dependency review requires versions and a resolved license status', () => {
  const spec = {dependencies:[], unresolvedQuestions:[]};
  assert.throws(
    () => resolveDependencyQuestion(spec, {
      runId:'run', notPackaged:false,
      dependencies:[{
        name:'git', included:true, version:'', license:'', licenseStatus:'unknown'
      }]
    }),
    /Add a version for git/
  );
});
