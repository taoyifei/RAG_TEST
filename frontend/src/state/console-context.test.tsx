import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ConsoleProvider, useConsole } from "./console-context";

function Probe() {
  const { scope, setProject, setKnowledgeBase } = useConsole();
  return (
    <>
      <output>{`${scope.projectId}|${scope.kbId}|${scope.revisionId}`}</output>
      <button onClick={() => setProject("prj_a")}>项目 A</button>
      <button onClick={() => setKnowledgeBase("kb_a", "irev_a")}>
        知识库 A
      </button>
      <button onClick={() => setProject("prj_b")}>项目 B</button>
    </>
  );
}

describe("内存范围", () => {
  it("切换项目时清空知识库和 Revision，避免串库", async () => {
    const user = userEvent.setup();
    render(
      <ConsoleProvider>
        <Probe />
      </ConsoleProvider>,
    );
    await user.click(screen.getByRole("button", { name: "项目 A" }));
    await user.click(screen.getByRole("button", { name: "知识库 A" }));
    expect(screen.getByRole("status")).toHaveTextContent("prj_a|kb_a|irev_a");
    await user.click(screen.getByRole("button", { name: "项目 B" }));
    expect(screen.getByRole("status")).toHaveTextContent("prj_b||");
  });
});
