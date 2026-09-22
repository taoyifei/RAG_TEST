import { RotateCcw, ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";

import type {
  PublicFeedbackReasonDetail,
  PublicFeedbackSubmission,
} from "./publicApi";

const reasons: ReadonlyArray<{
  value: PublicFeedbackReasonDetail;
  label: string;
}> = [
  { value: "INCORRECT", label: "答案不正确" },
  { value: "INCOMPLETE", label: "答案不完整" },
  { value: "WRONG_SOURCE", label: "来源不对" },
  { value: "FALSE_REFUSAL", label: "应该回答但拒答" },
  { value: "UNSAFE_ANSWER", label: "应该拒答却回答" },
  { value: "TOO_SLOW", label: "回答太慢" },
  { value: "OTHER", label: "其他" },
];

export function PublicFeedback({
  detailsEnabled,
  errorMessage,
  onLogin,
  onSubmit,
  status,
}: {
  detailsEnabled: boolean;
  errorMessage?: string;
  onLogin?: () => void;
  onSubmit: (feedback: PublicFeedbackSubmission) => void;
  status: "idle" | "submitting" | "sent" | "failed";
}) {
  const [negativeOpen, setNegativeOpen] = useState(false);
  const [reasonDetail, setReasonDetail] =
    useState<PublicFeedbackReasonDetail>();
  const [comment, setComment] = useState("");
  const submitting = status === "submitting";

  if (status === "sent") {
    return (
      <p className="wst-feedback-status" role="status">
        感谢你的反馈
      </p>
    );
  }
  return (
    <div className="wst-feedback-block">
      <div className="wst-feedback" aria-label="回答反馈">
        <span>这次结果怎么样？</span>
        <button
          disabled={submitting}
          onClick={() => onSubmit({ useful: true })}
          type="button"
        >
          <ThumbsUp aria-hidden="true" size={15} />
          有帮助
        </button>
        <button
          disabled={submitting}
          onClick={() => {
            if (detailsEnabled) setNegativeOpen(true);
            else onSubmit({ useful: false });
          }}
          type="button"
        >
          <ThumbsDown aria-hidden="true" size={15} />
          没帮助
        </button>
        {submitting && (
          <span className="wst-feedback-pending" role="status">
            正在提交
          </span>
        )}
      </div>
      {negativeOpen && detailsEnabled && (
        <form
          className="wst-feedback-form"
          onSubmit={(event) => {
            event.preventDefault();
            onSubmit({ useful: false, reasonDetail, comment });
          }}
        >
          <label htmlFor="wst-feedback-reason">主要原因（可选）</label>
          <select
            disabled={submitting}
            id="wst-feedback-reason"
            onChange={(event) =>
              setReasonDetail(
                (event.target.value || undefined) as
                  | PublicFeedbackReasonDetail
                  | undefined,
              )
            }
            value={reasonDetail ?? ""}
          >
            <option value="">不选择，按“其他”处理</option>
            {reasons.map((reason) => (
              <option key={reason.value} value={reason.value}>
                {reason.label}
              </option>
            ))}
          </select>
          <label htmlFor="wst-feedback-comment">补充说明（可选）</label>
          <textarea
            disabled={submitting}
            id="wst-feedback-comment"
            maxLength={1000}
            onChange={(event) => setComment(event.target.value)}
            placeholder="例如：更合适的来源、缺少的条件或出现问题的位置"
            rows={3}
            value={comment}
          />
          <div className="wst-feedback-form-actions">
            <span>{comment.length}/1000</span>
            <button disabled={submitting} type="submit">
              {status === "failed" ? (
                <RotateCcw aria-hidden="true" size={15} />
              ) : null}
              {status === "failed" ? "重新提交反馈" : "提交反馈"}
            </button>
          </div>
        </form>
      )}
      {status === "failed" && (
        <div className="wst-feedback-status is-error" role="alert">
          <span>{errorMessage ?? "反馈暂未提交成功，已保留你的输入。"}</span>
          {errorMessage?.includes("登录会话已过期") && onLogin ? (
            <button onClick={onLogin} type="button">
              重新登录
            </button>
          ) : null}
        </div>
      )}
    </div>
  );
}
