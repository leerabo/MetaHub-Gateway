import test from 'node:test';
import assert from 'node:assert/strict';
import { constrainDialogOffset } from '../../src/lib/dialogs.js';

test('dragging cannot hide either edge of a dialog or its title', () => {
  const box = { left: 100, right: 700, top: 100, bottom: 600 };
  const viewport = { width: 1000, height: 800 };
  assert.deepEqual(constrainDialogOffset(box, { x: -1000, y: -1000 }, viewport), { x: -84, y: -84 });
  assert.deepEqual(constrainDialogOffset(box, { x: 1000, y: 1000 }, viewport), { x: 284, y: 184 });
});

test('a long form that fills the viewport has no vertical drag range', () => {
  const box = { left: 200, right: 800, top: 16, bottom: 784 };
  assert.deepEqual(constrainDialogOffset(box, { x: 40, y: 80 }, { width: 1000, height: 800 }), { x: 40, y: 0 });
});

test('shrinking the viewport recovers a previously valid displaced dialog', () => {
  const box = { left: 200, right: 700, top: 100, bottom: 400 };
  const next = constrainDialogOffset(box, { x: 260, y: 200 }, { width: 760, height: 480 });
  assert.equal(box.right + next.x, 744);
  assert.equal(box.bottom + next.y, 464);
});
