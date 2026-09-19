'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

global.window = {};
require('../src/autocab/ui/dashboard.js');

const {timelineModel} = window.WfrecActivityDashboard;

function event(ts, seq, type='context.note'){
  return {ts, seq, type, payload:{}};
}

function modelEventCount(model){
  return model.buckets.reduce((total, bucket) => total + bucket.total, 0);
}

test('timeline sorts recorded events by timestamp and sequence', () => {
  const events = [
    event('2026-09-18T12:00:03.000Z', 1),
    event('2026-09-18T12:00:01.000Z', 3),
    event('2026-09-18T12:00:01.000Z', 2)
  ];

  const model = timelineModel(events, false, Date.parse('2026-09-18T12:01:00Z'));

  assert.equal(model.first, Date.parse('2026-09-18T12:00:01.000Z'));
  assert.equal(model.last, Date.parse('2026-09-18T12:00:03.000Z'));
  assert.equal(model.eventTotal, 3);
  assert.equal(modelEventCount(model), 3);
});

test('timeline excludes events without a valid timestamp from its total', () => {
  const model = timelineModel([
    event('not-a-timestamp', 1),
    event('2026-09-18T12:00:00.000Z', 2)
  ], false, Date.parse('2026-09-18T12:01:00Z'));

  assert.equal(model.eventTotal, 1);
  assert.equal(modelEventCount(model), 1);
});

test('live timeline ends at now when the recorder clock is current', () => {
  const now = Date.parse('2026-09-18T12:01:00.000Z');
  const model = timelineModel([
    event('2026-09-18T12:00:00.000Z', 1)
  ], true, now);

  assert.equal(model.endsAtNow, true);
  assert.equal(model.last, now);
});

test('live timeline preserves a future event timestamp caused by clock skew', () => {
  const future = Date.parse('2026-09-18T12:02:00.000Z');
  const model = timelineModel([
    event('2026-09-18T12:00:00.000Z', 1),
    event('2026-09-18T12:02:00.000Z', 2)
  ], true, Date.parse('2026-09-18T12:01:00.000Z'));

  assert.equal(model.endsAtNow, false);
  assert.equal(model.last, future);
});

test('timeline reports when its local date range crosses midnight', () => {
  const model = timelineModel([
    event('2026-09-18T00:00:00.000Z', 1),
    event('2026-09-20T00:00:00.000Z', 2)
  ], false, Date.parse('2026-09-20T00:00:00.000Z'));

  assert.equal(model.spansMultipleDays, true);
});
