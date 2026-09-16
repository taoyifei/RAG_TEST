import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AdminDocumentsPage, DOCX_ONLY_MESSAGE } from "./AdminDocumentsPage";
import { DOCX_MEDIA_TYPE } from "./adminApi";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestPath(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function requestMetadata(path: string): Record<string, unknown> {
  const serialized = new URL(path, "http://localhost").searchParams.get(
    "metadata",
  );
  return serialized ? (JSON.parse(serialized) as Record<string, unknown>) : {};
}

function job(id: string, state: string, stage: string) {
  return {
    job_id: id,
    document_id: `doc_${id}`,
    document_version_id: `dver_${id}`,
    revision_id: `irev_${id}`,
    state,
    stage,
    safe_error: null,
    error_code: null,
    retryable: false,
    attempt: 1,
    slot_progress: [],
  };
}

function docx(name: string, relativePath?: string): File {
  const file = new File(["synthetic-ooxml"], name, { type: DOCX_MEDIA_TYPE });
  if (relativePath) {
    Object.defineProperty(file, "webkitRelativePath", { value: relativePath });
  }
  return file;
}

afterEach(() => vi.restoreAllMocks());

describe("湾事通 DOCX-only 文档管理", () => {
  it("限制 accept，显示禁用提示与 WB-07 空状态", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response({ items: [], next_cursor: null }),
    );
    render(<AdminDocumentsPage />);

    expect(screen.getByText(DOCX_ONLY_MESSAGE)).toBeVisible();
    expect(await screen.findByText("最终业务资料将在 WB-07 阶段统一导入。")).toBeVisible();
    expect(screen.getByTestId("wst-document-files")).toHaveAttribute(
      "accept",
      `.docx,${DOCX_MEDIA_TYPE}`,
    );
    expect(screen.getByTestId("wst-document-files")).toHaveAttribute("multiple");
    expect(screen.getByTestId("wst-document-directory")).toHaveAttribute(
      "webkitdirectory",
      "",
    );
  });

  it("多文件独立上传、保留目录路径并轮询真实 Job", async () => {
    const requests: Array<{ path: string; method: string; key: string | null }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = requestPath(input);
      const method = init?.method || "GET";
      requests.push({
        path,
        method,
        key: new Headers(init?.headers).get("Idempotency-Key"),
      });
      const relativePath = requestMetadata(path).source_relative_path;
      if (method === "GET" && path.includes("/documents")) {
        return Promise.resolve(response({ items: [], next_cursor: null }));
      }
      if (
        method === "POST" &&
        typeof relativePath === "string" &&
        relativePath.endsWith("失败制度.docx")
      ) {
        return Promise.resolve(
          response(
            { error: { code: "INVALID_DOCX", message: "文件签名不合法。" } },
            400,
          ),
        );
      }
      if (
        method === "POST" &&
        typeof relativePath === "string" &&
        relativePath.endsWith("成功制度.docx")
      ) {
        const queued = job("job_success", "queued", "queued");
        return Promise.resolve(
          response(
            {
              document: {
                document_id: "doc_success",
                display_name: "成功制度.docx",
                relative_path: "01 科管/成功制度.docx",
                status: "active",
                retrievable: false,
                created_at: "2026-01-01T00:00:00Z",
                updated_at: "2026-01-01T00:00:00Z",
              },
              job: queued,
            },
            202,
          ),
        );
      }
      if (method === "GET" && path.endsWith("/jobs/job_success")) {
        return Promise.resolve(response(job("job_success", "succeeded", "activated")));
      }
      throw new Error(`unexpected request: ${method} ${path}`);
    });
    const user = userEvent.setup();
    render(<AdminDocumentsPage />);
    await screen.findByText("最终业务资料将在 WB-07 阶段统一导入。");
    const success = docx("成功制度.docx", "01 科管/成功制度.docx");
    const failure = docx("失败制度.docx", "02 运营/失败制度.docx");

    await user.upload(screen.getByTestId("wst-document-directory"), [success, failure]);

    await waitFor(() => expect(screen.getByText("文件签名不合法。")).toBeVisible());
    await waitFor(() => expect(screen.getByText("已完成")).toBeVisible());
    const uploads = requests.filter((item) => item.method === "POST");
    expect(uploads).toHaveLength(2);
    expect(
      uploads.some(
        (item) =>
          requestMetadata(item.path).source_relative_path ===
          "01 科管/成功制度.docx",
      ),
    ).toBe(true);
    expect(uploads.every((item) => !!item.key)).toBe(true);
    expect(
      requests.some((item) => item.path.endsWith("/jobs/job_success")),
    ).toBe(true);
  });

  it("拖拽非 DOCX 时在前端逐项阻断且不创建请求", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response({ items: [], next_cursor: null }),
    );
    render(<AdminDocumentsPage />);
    await screen.findByText("最终业务资料将在 WB-07 阶段统一导入。");
    const pdf = new File(["pdf"], "水印材料.pdf", {
      type: "application/pdf",
    });
    fireEvent.drop(
      screen.getByRole("button", {
        name: "拖拽 DOCX 到此处，或按回车选择文件",
      }),
      { dataTransfer: { files: [pdf] } },
    );

    expect(await screen.findAllByText(DOCX_ONLY_MESSAGE)).toHaveLength(2);
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(0);
  });

  it("上传响应不确定后复用同一 Idempotency-Key 重试", async () => {
    const keys: string[] = [];
    let attempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      if ((init?.method || "GET") === "GET") {
        return Promise.resolve(response({ items: [], next_cursor: null }));
      }
      keys.push(new Headers(init?.headers).get("Idempotency-Key") || "");
      attempts += 1;
      if (attempts === 1) return Promise.reject(new TypeError("连接中断"));
      const completed = job("job_retry", "succeeded", "activated");
      return Promise.resolve(
        response({
          document: {
            document_id: "doc_retry",
            display_name: "重试.docx",
            relative_path: "重试.docx",
            status: "active",
            retrievable: true,
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          },
          job: completed,
        }, 202),
      );
    });
    const user = userEvent.setup();
    render(<AdminDocumentsPage />);
    await screen.findByText("最终业务资料将在 WB-07 阶段统一导入。");
    await user.upload(screen.getByTestId("wst-document-files"), docx("重试.docx"));
    await screen.findByText("连接中断");
    await user.click(screen.getByRole("button", { name: "重试此文件" }));
    await waitFor(() => expect(screen.getByText("已完成")).toBeVisible());
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
    expect(keys[0]).not.toBe("");
  });

  it("上传新版本沿用原逻辑文档相对路径", async () => {
    let uploadedPath = "";
    const document = {
      document_id: "doc_nested",
      display_name: "制度.docx",
      relative_path: "01 科管/02 项目/制度.docx",
      status: "active",
      current_version_id: "dver_old",
      current_version_status: "active",
      active_index_revision_id: "irev_old",
      latest_job: null,
      retrievable: true,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const path = requestPath(input);
      if ((init?.method || "GET") === "GET") {
        return Promise.resolve(response({ items: [document], next_cursor: null }));
      }
      const sourceRelativePath = requestMetadata(path).source_relative_path;
      uploadedPath =
        typeof sourceRelativePath === "string" ? sourceRelativePath : "";
      const completed = job("job_version", "succeeded", "activated");
      return Promise.resolve(response({ document, job: completed }, 202));
    });
    const user = userEvent.setup();
    render(<AdminDocumentsPage />);
    const row = await screen.findByRole("row", { name: /制度\.docx/ });

    await user.upload(
      row.querySelector('input[type="file"]') as HTMLInputElement,
      docx("本机临时名称.docx"),
    );

    await waitFor(() =>
      expect(uploadedPath).toBe("01 科管/02 项目/制度.docx"),
    );
  });
});
