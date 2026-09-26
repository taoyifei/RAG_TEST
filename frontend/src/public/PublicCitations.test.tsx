import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import { PublicCitations } from "./PublicCitations";
import type { PublicCitation } from "./publicSse";

function citation(
  index: number,
  overrides: Partial<PublicCitation> = {},
): PublicCitation {
  return {
    document_name: "开发中心三种工作模式",
    department_name: "开发中心",
    category_path: ["制度", "研发"],
    source_relative_path: "制度/研发/开发中心三种工作模式.docx",
    locator: `表格第 ${index} 行`,
    quote: `合成证据片段 ${index}`,
    ...overrides,
  };
}

it("按完整来源身份分组并保留全部片段和原始顺序", async () => {
  const user = userEvent.setup();
  const citations = Array.from({ length: 5 }, (_, index) =>
    citation(index + 1),
  );
  const original = structuredClone(citations);

  const { container } = render(<PublicCitations citations={citations} />);

  const panelSummary = screen.getByText("引用依据（5段）");
  const panel = container.querySelector(".wst-citations");
  expect(panel).not.toHaveAttribute("open");
  panelSummary.focus();
  expect(panelSummary).toHaveFocus();
  await user.click(panelSummary);
  expect(panel).toHaveAttribute("open");

  const groups = screen.getAllByRole("article");
  expect(groups).toHaveLength(1);
  const groupSummary = within(groups[0])
    .getByText("展开全部片段")
    .closest("summary");
  const group = groups[0].querySelector(".wst-citation-group");
  expect(groupSummary).not.toBeNull();
  expect(group).not.toHaveAttribute("open");
  groupSummary?.focus();
  expect(groupSummary).toHaveFocus();
  if (groupSummary) await user.click(groupSummary);
  expect(group).toHaveAttribute("open");

  for (let index = 1; index <= 5; index += 1) {
    expect(within(groups[0]).getByText(`表格第 ${index} 行`)).toBeVisible();
    expect(within(groups[0]).getByText(`合成证据片段 ${index}`)).toBeVisible();
  }
  expect(citations).toEqual(original);
});

it("同名不同完整路径分组，缺路径的旧响应保持独立", () => {
  render(
    <PublicCitations
      citations={[
        citation(1, { source_relative_path: "甲部门/同名制度.docx" }),
        citation(2, { source_relative_path: "乙部门/同名制度.docx" }),
        citation(3, { source_relative_path: undefined }),
        citation(4, { source_relative_path: undefined }),
      ]}
    />,
  );

  expect(screen.getAllByRole("article")).toHaveLength(4);
  expect(screen.getByText("甲部门/同名制度.docx")).toBeInTheDocument();
  expect(screen.getByText("乙部门/同名制度.docx")).toBeInTheDocument();
  expect(screen.getAllByText("1段")).toHaveLength(4);
});

it("同路径的相同引文不去重并保留不同定位", () => {
  render(
    <PublicCitations
      citations={[
        citation(1, { locator: "第 3 页", quote: "相同合成引文" }),
        citation(2, { locator: "第 8 页", quote: "相同合成引文" }),
      ]}
    />,
  );

  const group = screen.getByRole("article");
  expect(within(group).getByText("2段")).toBeInTheDocument();
  expect(within(group).getByText("第 3 页")).toBeInTheDocument();
  expect(within(group).getByText("第 8 页")).toBeInTheDocument();
  expect(within(group).getAllByText("相同合成引文")).toHaveLength(2);
});

it("原生引用缺少路径时仍按知识 ID 聚合同一文件", () => {
  render(
    <PublicCitations
      citations={[
        citation(1, {
          source_kind: "weknora",
          source_relative_path: undefined,
          native_knowledge_id: "knowledge-1",
        }),
        citation(2, {
          source_kind: "weknora",
          source_relative_path: undefined,
          native_knowledge_id: "knowledge-1",
        }),
        citation(3, {
          source_kind: "weknora",
          source_relative_path: undefined,
          native_knowledge_id: "knowledge-2",
        }),
      ]}
    />,
  );
  expect(screen.getAllByRole("article")).toHaveLength(2);
  expect(screen.getByText("2段")).toBeInTheDocument();
  expect(screen.getByText("1段")).toBeInTheDocument();
});

it("空引用不渲染，旧响应缺少可选字段时仍可展开", () => {
  const { container, rerender } = render(<PublicCitations citations={[]} />);
  expect(container).toBeEmptyDOMElement();

  rerender(<PublicCitations citations={[{ document_name: "旧版合成资料" }]} />);

  expect(screen.getByText("引用依据（1段）")).toBeInTheDocument();
  expect(screen.getByText("旧版合成资料")).toBeInTheDocument();
  expect(screen.getByText("1段")).toBeInTheDocument();
});

it("仅对标记原件可用的 WeKnora 引用展示下载入口", () => {
  const referenceId = `ref_${"a".repeat(32)}`;
  const { container, rerender } = render(
    <PublicCitations
      citations={[
        citation(1, {
          source_kind: "weknora",
          reference_id: referenceId,
          source_available: false,
        }),
      ]}
      onDownloadReference={() => Promise.resolve()}
    />,
  );
  expect(container.querySelector(".wst-citation-segment button")).toBeNull();
  rerender(
    <PublicCitations
      citations={[
        citation(1, {
          source_kind: "weknora",
          reference_id: referenceId,
          source_available: true,
        }),
      ]}
      onDownloadReference={() => Promise.resolve()}
    />,
  );
  expect(
    container.querySelector(".wst-citation-segment button"),
  ).toBeInTheDocument();
  expect(screen.getByText("查看引用片段")).toBeInTheDocument();
});

it("原生引用的片段与原件分别使用明确入口", async () => {
  const user = userEvent.setup();
  const onDownload = vi.fn().mockResolvedValue(undefined);
  render(
    <PublicCitations
      citations={[
        citation(1, {
          source_kind: "weknora",
          reference_id: `ref_${"b".repeat(32)}`,
          source_available: true,
          original_available: true,
        }),
      ]}
      onDownloadReference={onDownload}
    />,
  );
  await user.click(screen.getByText("引用依据（1段）"));
  await user.click(screen.getByText("开发中心三种工作模式"));
  await user.click(screen.getByRole("button", { name: "下载原件" }));
  expect(onDownload).toHaveBeenCalledWith(
    `ref_${"b".repeat(32)}`,
    "开发中心三种工作模式",
    true,
  );
});

it("关闭原件下载时保留原生引用片段和依据，隐藏新旧原件入口", async () => {
  const user = userEvent.setup();
  render(
    <PublicCitations
      allowSourceDownload={false}
      citations={[
        citation(1, {
          source_kind: "weknora",
          reference_id: `ref_${"a".repeat(32)}`,
          source_available: true,
          original_available: true,
        }),
        citation(2, {
          source_kind: "legacy",
          reference_id: `ref_${"b".repeat(32)}`,
        }),
      ]}
      onDownloadReference={() => Promise.resolve()}
    />,
  );
  await user.click(screen.getByText("引用依据（2段）"));
  for (const group of screen.getAllByRole("article")) {
    const summary = group.querySelector(".wst-citation-heading");
    if (summary) await user.click(summary);
  }
  expect(screen.getByRole("button", { name: "查看引用片段" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "下载原件" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "下载本次引用的原件" })).not.toBeInTheDocument();
  expect(screen.getByText("合成证据片段 1")).toBeInTheDocument();
});
