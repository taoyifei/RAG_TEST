import { Check, Copy, FileDown, Link2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import {
  answerClipboardText,
  answerWordFilename,
  canExportAnswer,
} from "./answerExport";
import { PublicApiError } from "./publicApi";
import type { PublicTurn } from "./usePublicChat";
import "./AnswerActions.css";

type Action = "copy" | "sources" | "word";

async function copyText(text: string): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {
    // 8289使用HTTP时可能不提供Clipboard API；继续尝试浏览器本地复制。
  }
  const field = document.createElement("textarea");
  field.value = text;
  field.readOnly = true;
  field.setAttribute("aria-hidden", "true");
  field.style.cssText = "position:fixed;left:-10000px;top:0;opacity:0";
  const focus = document.activeElement;
  document.body.append(field);
  try {
    field.select();
    if (!document.execCommand("copy")) throw new Error("clipboard unavailable");
  } finally {
    field.remove();
    if (focus instanceof HTMLElement) focus.focus();
  }
}

export function AnswerActions({
  turn,
  beforeExport,
}: {
  turn: PublicTurn;
  beforeExport?: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<Action>();
  const [notice, setNotice] = useState<{ text: string; failed: boolean }>();
  const mounted = useRef(false);
  const running = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  if (!canExportAnswer(turn)) return null;

  async function run(action: Action) {
    if (running.current) return;
    running.current = true;
    setBusy(action);
    setNotice(undefined);
    try {
      await beforeExport?.();
      if (!mounted.current) return;
      if (action === "word") {
        const { answerWordBlob } = await import("./answerDocx");
        const blob = await answerWordBlob(turn);
        if (!mounted.current) return;
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = answerWordFilename(turn);
        document.body.append(anchor);
        try {
          anchor.click();
        } finally {
          anchor.remove();
          window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
      } else {
        await copyText(answerClipboardText(turn, action === "sources"));
      }
      if (mounted.current)
        setNotice({
          text:
            action === "word"
              ? "Word文件已生成。"
              : action === "sources"
                ? "已复制回答和来源。"
                : "已复制回答。",
          failed: false,
        });
    } catch (error) {
      if (mounted.current)
        setNotice({
          text:
            error instanceof PublicApiError &&
            [401, 403, 404].includes(error.status)
              ? "当前登录或资料权限已变化，请刷新后重试。"
              : action === "word"
                ? "Word导出未完成，请稍后重试。"
                : "复制未成功，请允许浏览器使用剪贴板后重试。",
          failed: true,
        });
    } finally {
      running.current = false;
      if (mounted.current) setBusy(undefined);
    }
  }

  return (
    <div className="wst-completed-answer-actions">
      <div className="wst-answer-export-buttons" aria-label="回答操作">
        <button
          type="button"
          disabled={Boolean(busy)}
          onClick={() => run("copy")}
        >
          <Copy size={15} aria-hidden="true" />
          {busy === "copy" ? "正在复制…" : "复制回答"}
        </button>
        <button
          type="button"
          disabled={Boolean(busy)}
          onClick={() => run("sources")}
        >
          <Link2 size={15} aria-hidden="true" />
          {busy === "sources" ? "正在复制…" : "复制含来源"}
        </button>
        <button
          type="button"
          disabled={Boolean(busy)}
          onClick={() => run("word")}
        >
          <FileDown size={15} aria-hidden="true" />
          {busy === "word" ? "正在导出…" : "导出Word"}
        </button>
      </div>
      {notice && (
        <p
          className={notice.failed ? "is-error" : "is-success"}
          role={notice.failed ? "alert" : "status"}
        >
          {!notice.failed && <Check size={14} aria-hidden="true" />}
          {notice.text}
        </p>
      )}
    </div>
  );
}
