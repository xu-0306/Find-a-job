"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const CompanionSprite = require(path.join(__dirname, "..", "app", "static", "sprite.js"));

class FakeElement {
  constructor() { this.style = Object.create(null); }
}

class FakeClock {
  constructor() { this.time = 0; this.nextId = 1; this.timers = new Map(); }
  setTimeout = (callback, delay) => {
    const id = this.nextId++;
    this.timers.set(id, {at: this.time + Number(delay), callback});
    return id;
  };
  clearTimeout = (id) => { this.timers.delete(id); };
  advance(milliseconds) {
    const target = this.time + milliseconds;
    while (true) {
      const due = [...this.timers.entries()]
        .filter(([, timer]) => timer.at <= target)
        .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!due) break;
      const [id, timer] = due;
      this.timers.delete(id);
      this.time = timer.at;
      timer.callback();
    }
    this.time = target;
  }
}

const sprites = [
  {state: "idle", frame_width: 16, frame_height: 20, frame_count: 3, fps: 4},
  {state: "thinking", frame_width: 18, frame_height: 20, frame_count: 2, fps: 8},
  {state: "happy", frame_width: 20, frame_height: 20, frame_count: 2, fps: 10},
  {state: "mail", frame_width: 22, frame_height: 20, frame_count: 2, fps: 5},
  {state: "encourage", frame_width: 24, frame_height: 20, frame_count: 2, fps: 2},
];

function testAnimationAndProtectedUrl() {
  const clock = new FakeClock();
  const element = new FakeElement();
  const controller = CompanionSprite.create(element, sprites, {setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout});
  assert.equal(element.style.backgroundImage, 'url("/api/sprites/idle/image")');
  assert.equal(element.style.backgroundPosition, "0px 0px");
  clock.advance(1999);
  assert.equal(element.style.backgroundPosition, "0px 0px", "idle rests on its first frame");
  clock.advance(1);
  assert.equal(element.style.backgroundPosition, "-16px 0px");
  clock.advance(250);
  assert.equal(element.style.backgroundPosition, "-32px 0px");
  clock.advance(250);
  assert.equal(element.style.backgroundPosition, "0px 0px");
  clock.advance(1999);
  assert.equal(element.style.backgroundPosition, "0px 0px", "each idle cycle rests again");
  controller.setThinking(true);
  clock.advance(125);
  assert.equal(element.style.backgroundPosition, "-18px 0px", "thinking retains its FPS without idle rests");
  controller.destroy();
}

function testFallbackAndReducedMotion() {
  const clock = new FakeClock();
  const element = new FakeElement();
  const controller = CompanionSprite.create(element, [sprites[2]], {
    fallbackUrl: "/placeholder.png", reducedMotion: true,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
  });
  assert.equal(element.style.backgroundImage, 'url("/api/sprites/happy/image")', "missing idle uses available uploaded artwork");
  assert.equal(element.style.width, "20px");
  controller.play("happy", 100);
  assert.equal(element.style.backgroundImage, 'url("/api/sprites/happy/image")');
  assert.equal(element.style.backgroundPosition, "0px 0px");
  assert.equal(clock.timers.size, 1, "reduced motion keeps only the end timer");
  clock.advance(100);
  assert.equal(element.style.backgroundImage, 'url("/api/sprites/happy/image")');
  controller.destroy();

  const idleClock = new FakeClock();
  const idleElement = new FakeElement();
  const idleController = CompanionSprite.create(idleElement, [sprites[0]], {
    setTimeout: idleClock.setTimeout, clearTimeout: idleClock.clearTimeout,
  });
  idleController.play("happy", 100);
  assert.equal(idleElement.style.backgroundImage, 'url("/api/sprites/idle/image")');
  idleController.destroy();
}

function testPriorityAndThinkingReturn() {
  const clock = new FakeClock();
  const element = new FakeElement();
  const controller = CompanionSprite.create(element, sprites, {setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout});
  controller.setThinking(true);
  assert.equal(element.style.backgroundImage, 'url("/api/sprites/thinking/image")');
  controller.play("encourage", 100);
  controller.play("happy", 10);
  assert.equal(element.style.backgroundImage, 'url("/api/sprites/encourage/image")');
  clock.advance(100);
  assert.equal(element.style.backgroundImage, 'url("/api/sprites/thinking/image")');
  controller.play("mail", 100);
  controller.play("encourage", 100);
  assert.equal(element.style.backgroundImage, 'url("/api/sprites/encourage/image")');
  controller.reset();
  assert.equal(element.style.backgroundImage, 'url("/api/sprites/idle/image")');
}

function testInvalidStateAndCleanup() {
  const clock = new FakeClock();
  const element = new FakeElement();
  const controller = CompanionSprite.create(element, sprites, {setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout});
  const before = element.style.backgroundImage;
  controller.play("unknown", 10);
  assert.equal(element.style.backgroundImage, before);
  controller.setThinking(true);
  assert.notEqual(clock.timers.size, 0);
  controller.destroy();
  assert.equal(clock.timers.size, 0);
  const after = element.style.backgroundPosition;
  clock.advance(1000);
  assert.equal(element.style.backgroundPosition, after);
  controller.setThinking(false);
  assert.equal(element.style.backgroundPosition, after);
}

function testEmptyMetadataAndLargeFrameScale() {
  const emptyClock = new FakeClock();
  const emptyElement = new FakeElement();
  const emptyController = CompanionSprite.create(emptyElement, [], {
    reducedMotion: false, setTimeout: emptyClock.setTimeout, clearTimeout: emptyClock.clearTimeout,
  });
  assert.doesNotThrow(() => {
    emptyController.setThinking(true);
    emptyController.play("happy", 20);
    emptyController.play("mail", 20);
    emptyController.play("encourage", 20);
    emptyController.setThinking(false);
    emptyController.reset();
    emptyController.destroy();
  });
  assert.equal(emptyClock.timers.size, 0);

  const largeClock = new FakeClock();
  const largeElement = new FakeElement();
  const largeController = CompanionSprite.create(largeElement, [
    {state: "idle", frame_width: 512, frame_height: 512, frame_count: 2, fps: 1},
  ], {setTimeout: largeClock.setTimeout, clearTimeout: largeClock.clearTimeout});
  assert.equal(largeElement.style.width, "128px");
  assert.equal(largeElement.style.height, "128px");
  assert.equal(largeElement.style.backgroundSize, "256px 128px");
  largeClock.advance(2000);
  assert.equal(largeElement.style.backgroundPosition, "-128px 0px");
  largeController.destroy();
}

function testPreviewPauseAndFrameRenderer() {
  const clock = new FakeClock();
  const frames = [];
  const controller = CompanionSprite.create(new FakeElement(), [sprites[3]], {
    paused: true, renderFrame(value, index) { frames.push([value.state, index]); },
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
  });
  assert.deepEqual(frames, [["mail", 0]]);
  assert.equal(clock.timers.size, 0);
  controller.setPaused(false);
  clock.advance(200);
  assert.deepEqual(frames.at(-1), ["mail", 1], "non-idle preview uses its own FPS");
  controller.setPaused(true);
  assert.equal(clock.timers.size, 0);
  clock.advance(10000);
  assert.equal(frames.length, 2);
  controller.setPaused(false);
  clock.advance(200);
  assert.deepEqual(frames.at(-1), ["mail", 0], "resume retains its frame position");
  controller.destroy();
  assert.equal(clock.timers.size, 0);
  controller.setPaused(false);
  assert.equal(clock.timers.size, 0);
}

function testMoreFramesPlayInOrderAndRepeat() {
  for (const count of [8, 12]) {
    for (const fps of [4, 8]) {
      const clock = new FakeClock();
      const frames = [];
      const controller = CompanionSprite.create(new FakeElement(), [
        {state: "happy", frame_width: 64, frame_height: 64, frame_count: count, fps},
      ], {
        renderFrame(value, index) { frames.push(index); },
        setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
      });
      clock.advance(count * 2 * 1000 / fps);
      assert.deepEqual(frames, Array.from({length: count * 2 + 1}, (_, index) => index % count),
        `${count} frames at ${fps} FPS play in order across two complete loops`);
      controller.destroy();
      assert.equal(clock.timers.size, 0);
    }
  }
}

testAnimationAndProtectedUrl();
testFallbackAndReducedMotion();
testPriorityAndThinkingReturn();
testInvalidStateAndCleanup();
testEmptyMetadataAndLargeFrameScale();
testPreviewPauseAndFrameRenderer();
testMoreFramesPlayInOrderAndRepeat();
const portrait = new FakeElement();
CompanionSprite.renderStatic(portrait, [{state: "mail", frame_width: 512, frame_height: 128, frame_count: 7, fps: 8}]);
assert.equal(portrait.style.backgroundImage, 'url("/api/sprites/mail/image")');
assert.equal(portrait.style.width, "48px");
assert.equal(portrait.style.height, "12px");
assert.equal(portrait.style.backgroundSize, "336px 12px");
assert.equal(portrait.style.backgroundPosition, "0px 0px");
CompanionSprite.renderStatic(portrait, [], "idle", 36);
assert.equal(portrait.style.backgroundImage, 'url("/static/companion.png")');
assert.equal(portrait.style.width, "36px");
console.log("test_m3_sprite: 8 checks passed");
