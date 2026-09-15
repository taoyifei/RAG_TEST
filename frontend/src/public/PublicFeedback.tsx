import { ThumbsDown, ThumbsUp } from "lucide-react";

export function PublicFeedback({
  onSubmit,
  status,
}: {
  onSubmit: (useful: boolean) => void;
  status: "idle" | "submitting" | "sent" | "failed";
}) {
  if (status === "sent") {
    return (
      <p className="wst-feedback-status" role="status">
        感谢你的反馈
      </p>
    );
  }
  if (status === "failed") {
    return (
      <p className="wst-feedback-status is-error" role="status">
        反馈暂未提交成功，答案内容不受影响
      </p>
    );
  }
  return (
    <div className="wst-feedback" aria-label="回答反馈">
      <span>这个回答怎么样？</span>
      <button
        disabled={status === "submitting"}
        onClick={() => onSubmit(true)}
        type="button"
      >
        <ThumbsUp aria-hidden="true" size={15} />
        有帮助
      </button>
      <button
        disabled={status === "submitting"}
        onClick={() => onSubmit(false)}
        type="button"
      >
        <ThumbsDown aria-hidden="true" size={15} />
        没帮助
      </button>
      {status === "submitting" && (
        <span className="wst-feedback-pending" role="status">
          正在提交
        </span>
      )}
    </div>
  );
}
