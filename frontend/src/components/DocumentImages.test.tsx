import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { api, type DocumentOcrScan } from "../api/client";
import { DocumentImages } from "./DocumentImages";

const scan: DocumentOcrScan = {
  media_count: 4,
  recognized_count: 1,
  pending_count: 2,
  media: [
    {
      media_sha256: "a".repeat(64),
      artifact_id: "sha256:" + "a".repeat(64),
      part_uri: "word/media/image1.png",
      media_type: "image/png",
      size_bytes: 1024,
      width: 800,
      height: 600,
      supported: true,
      approved: true,
      cached: false,
    },
    {
      media_sha256: "b".repeat(64),
      artifact_id: "sha256:" + "b".repeat(64),
      part_uri: "word/media/image2.png",
      media_type: "image/png",
      size_bytes: 1024,
      supported: true,
      approved: true,
      cached: true,
      indexed: true,
    },
    {
      media_sha256: "c".repeat(64),
      artifact_id: "sha256:" + "c".repeat(64),
      part_uri: "word/media/image3.emf",
      media_type: "image/emf",
      size_bytes: 1024,
      supported: false,
      approved: true,
      cached: false,
      reason_code: "UNSUPPORTED_MEDIA",
    },
    {
      media_sha256: "d".repeat(64),
      artifact_id: "sha256:" + "d".repeat(64),
      part_uri: "word/media/image4.png",
      media_type: "image/png",
      size_bytes: 1024,
      supported: true,
      approved: false,
      cached: false,
    },
  ],
};
beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "scanDocumentImages").mockResolvedValue(scan);
});

it("只扫描不调用OCR；已索引和不支持图片禁选，确认只提交所选hash", async () => {
  const user = userEvent.setup();
  const submit = vi
    .spyOn(api, "recognizeDocumentImages")
    .mockRejectedValue(new Error("当前授权不包含这张图片"));
  render(
    <DocumentImages
      projectId="prj_test"
      kbId="kb_test"
      documentId="doc_test"
      onClose={vi.fn()}
      onSubmitted={vi.fn()}
    />,
  );
  expect(
    await screen.findByText(
      /已缓存 1 张 · 待识别 2 张 · 不支持 1 张 · 未授权 1 张/,
    ),
  ).toBeVisible();
  expect(submit).not.toHaveBeenCalled();
  expect(screen.getByRole("checkbox", { name: /image2/ })).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: /image3/ })).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: /image4/ })).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: /image4/ })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: /image1/ })).toBeChecked();
  await user.click(screen.getByRole("button", { name: "识别所选图片" }));
  expect(submit).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "确认识别所选图片" }));
  expect(submit).toHaveBeenCalledWith("kb_test", "doc_test", ["a".repeat(64)]);
  expect(await screen.findByText("当前授权不包含这张图片")).toBeVisible();
  expect(screen.getByRole("checkbox", { name: /image1/ })).toBeChecked();
  expect(
    screen.queryByRole("button", { name: "确认识别所选图片" }),
  ).not.toBeInTheDocument();
});

it("只有点击查看后读取受控图片，加载失败不保留原图", async () => {
  const user = userEvent.setup();
  render(
    <DocumentImages
      projectId="prj_test"
      kbId="kb_test"
      documentId="doc_test"
      onClose={vi.fn()}
      onSubmitted={vi.fn()}
    />,
  );
  const buttons = await screen.findAllByRole("button", { name: "查看原图" });
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  await user.click(buttons[0]);
  const image = screen.getByRole("img");
  expect(image).toHaveAttribute(
    "src",
    `/api/v1/projects/prj_test/knowledge-bases/kb_test/documents/doc_test/images/sha256%3A${"a".repeat(64)}`,
  );
  fireEvent.error(image);
  expect(
    within(screen.getByRole("alert")).getByText(/原图不可访问/),
  ).toBeVisible();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

it("缓存尚未进入Active时可选择零识别调用的索引恢复", async () => {
  const user = userEvent.setup();
  vi.mocked(api.scanDocumentImages).mockResolvedValue({
    media_count: 1,
    recognized_count: 1,
    pending_count: 0,
    indexed_count: 0,
    rebuild_count: 1,
    media: [{ ...scan.media[1], indexed: false }],
  });
  const submit = vi
    .spyOn(api, "recognizeDocumentImages")
    .mockRejectedValue(new Error("合成队列错误"));
  render(
    <DocumentImages
      projectId="prj_test"
      kbId="kb_test"
      documentId="doc_test"
      onClose={vi.fn()}
      onSubmitted={vi.fn()}
    />,
  );
  const checkbox = await screen.findByRole("checkbox", { name: /image2/ });
  expect(checkbox).toBeEnabled();
  expect(checkbox).toBeChecked();
  expect(screen.getByText(/已缓存，尚未进入当前索引/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "使用缓存重建索引" }));
  expect(screen.getByRole("status")).toHaveTextContent(
    "不再次调用图片识别服务",
  );
  expect(submit).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "确认重建索引" }));
  expect(submit).toHaveBeenCalledWith("kb_test", "doc_test", ["b".repeat(64)]);
  expect(await screen.findByText("合成队列错误")).toBeVisible();
});
