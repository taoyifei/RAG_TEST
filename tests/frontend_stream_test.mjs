import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";


class FakeNode {
  constructor() {
    this.children = [];
    this.disabled = false;
    this.hidden = false;
    this.href = "";
    this.textContent = "";
  }

  addEventListener() {}

  append(...children) {
    this.children.push(...children);
  }

  focus() {}

  replaceChildren() {
    this.children = [];
  }

  scrollIntoView() {}
}


function frontendContext() {
  const nodes = new Map(
    [
      "#token",
      "#question",
      "#ask",
      "#clear",
      "#result",
      "#stages",
      "#answer",
      "#citations",
      "#trace-id",
      "#copy-trace",
      "#view-trace",
      "#feedback-useful",
      "#feedback-not-useful",
    ].map((selector) => [selector, new FakeNode()]),
  );
  const context = vm.createContext({
    AbortController,
    Map,
    TextDecoder,
    Uint8Array,
    console,
    crypto: { randomUUID: () => "conversation-test" },
    document: {
      createElement: () => new FakeNode(),
      querySelector: (selector) => nodes.get(selector),
    },
    fetch: async () => new Response("", { status: 204 }),
    navigator: { clipboard: { writeText: async () => {} } },
    window: {
      addEventListener: () => {},
      matchMedia: () => ({ matches: true }),
    },
  });
  return { context, nodes };
}


const claim = {
  claim_index: 0,
  text: "唯一答案",
  supports: [
    {
      evidence_id: "E1",
      chunk_id: "chunk-1",
      quote: "唯一引用",
      locator: "测试文档 > 段落1",
    },
  ],
};


test("final does not duplicate an already streamed claim", async () => {
  const source = await readFile("frontend/app.js", "utf8");
  const { context, nodes } = frontendContext();
  vm.runInContext(source, context);

  vm.runInContext(`renderClaim(${JSON.stringify(claim)})`, context);
  vm.runInContext(
    `renderFinal(${JSON.stringify({
      type: "final",
      trace_id: "trace-test",
      status: "answered",
      answer: "唯一答案",
      user_message: null,
      refusal_code: null,
      claims: [claim],
    })})`,
    context,
  );

  assert.equal(nodes.get("#answer").textContent, "唯一答案");
  assert.equal(nodes.get("#citations").children.length, 1);
  assert.equal(vm.runInContext("streamedClaims.size", context), 1);
});


test("interrupted stream keeps validated text and marks it incomplete", async () => {
  const source = await readFile("frontend/app.js", "utf8");
  const { context, nodes } = frontendContext();
  vm.runInContext(source, context);

  vm.runInContext(`renderClaim(${JSON.stringify(claim)})`, context);
  vm.runInContext("renderInterrupted()", context);

  const answer = nodes.get("#answer").textContent;
  assert.match(answer, /回答流中断，已显示内容可能不完整/);
  assert.match(answer, /唯一答案/);
});
