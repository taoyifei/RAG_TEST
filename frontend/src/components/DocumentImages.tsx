import { useEffect, useState } from "react";
import {
  api,
  type DocumentOcrScan,
  type Evidence,
  type Job,
} from "../api/client";
import { ErrorPanel, Modal } from "./ui";

export function DocumentImages({
  projectId,
  kbId,
  documentId,
  onClose,
  onSubmitted,
}: {
  projectId: string;
  kbId: string;
  documentId: string;
  onClose: () => void;
  onSubmitted: (job: Job) => void;
}) {
  const [scan, setScan] = useState<DocumentOcrScan>();
  const [selected, setSelected] = useState<string[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    let active = true;
    void api
      .scanDocumentImages(kbId, documentId)
      .then((value) => {
        if (active) {
          setScan(value);
          setSelected(
            value.media
              .filter(
                (media) => media.approved && media.supported && !media.cached,
              )
              .map((media) => media.media_sha256),
          );
        }
      })
      .catch((reason) => {
        if (active) setError(reason);
      });
    return () => {
      active = false;
    };
  }, [kbId, documentId]);
  async function submit() {
    if (pending || !confirmed || !selected.length) return;
    setPending(true);
    setError(undefined);
    try {
      onSubmitted(
        await api.recognizeDocumentImages(kbId, documentId, selected),
      );
    } catch (reason) {
      setError(reason);
      setConfirmed(false);
    } finally {
      setPending(false);
    }
  }
  return (
    <Modal title="文档图片识别" onClose={onClose} drawer>
      {!scan && error === undefined && (
        <p role="status">正在扫描文档图片，不调用识别服务…</p>
      )}
      {scan && (
        <div className="stack">
          <p>
            共 {scan.media_count} 张 · 已缓存 {scan.recognized_count} 张 ·
            待识别 {scan.pending_count} 张 · 不支持{" "}
            {scan.media.filter((item) => !item.supported).length} 张
            {" · 未授权 "}
            {
              scan.media.filter(
                (item) => item.supported && !item.cached && !item.approved,
              ).length
            }{" "}
            张
          </p>
          <p>
            选择需要识别的图片。执行使用知识库已绑定的模型与授权预算，仅处理服务端已授权的图片，不会增加授权范围。
          </p>
          {!scan.media.length && <p>此文档未发现内嵌图片。</p>}
          {scan.media.map((media) => (
            <article className="panel stack" key={media.media_sha256}>
              <label className="ocr-selection">
                <input
                  type="checkbox"
                  checked={selected.includes(media.media_sha256)}
                  disabled={
                    pending ||
                    media.cached ||
                    !media.supported ||
                    !media.approved
                  }
                  onChange={(event) => {
                    setSelected(
                      event.target.checked
                        ? [...selected, media.media_sha256]
                        : selected.filter(
                            (hash) => hash !== media.media_sha256,
                          ),
                    );
                    setConfirmed(false);
                  }}
                />
                {media.part_uri} ·{" "}
                {media.cached
                  ? "已缓存"
                  : media.supported
                    ? media.approved
                      ? "待识别"
                      : "未授权，暂不能识别"
                    : "不支持"}
              </label>
              <small>
                {media.media_type} · {Math.ceil(media.size_bytes / 1024)} KB
                {media.width && media.height
                  ? ` · ${media.width} × ${media.height}`
                  : ""}
              </small>
              {media.reason_code && <small>{media.reason_code}</small>}
              <SourceImage
                projectId={projectId}
                kbId={kbId}
                documentId={documentId}
                artifactId={media.artifact_id}
              />
            </article>
          ))}
          {selected.length > 0 && (
            <p>
              已选 {selected.length}{" "}
              张。识别完成后创建新索引，当前索引在任务成功前继续可用。
            </p>
          )}
          {confirmed && (
            <p role="status">
              确认发送所选 {selected.length}{" "}
              张图片到已配置的识别服务，可能消耗服务额度。
            </p>
          )}
          <div className="row-actions">
            <button
              className="primary"
              disabled={pending || !selected.length}
              onClick={() => (confirmed ? void submit() : setConfirmed(true))}
            >
              {pending
                ? "提交中…"
                : confirmed
                  ? "确认识别所选图片"
                  : "识别所选图片"}
            </button>
            {confirmed && (
              <button disabled={pending} onClick={() => setConfirmed(false)}>
                返回选择
              </button>
            )}
          </div>
        </div>
      )}
      {error !== undefined && <ErrorPanel error={error} />}
    </Modal>
  );
}

export function isOcrEvidence(evidence: Evidence): boolean {
  return (
    evidence.metadata?.some(
      ([key, value]) => key === "origin" && value === "ocr",
    ) ?? false
  );
}

export function OcrEvidenceSource({
  evidence,
  projectId,
  kbId,
}: {
  evidence: Evidence;
  projectId: string;
  kbId: string;
}) {
  if (!isOcrEvidence(evidence)) return null;
  const artifact = evidence.metadata.find(
    ([key]) => key === "artifact_id",
  )?.[1];
  return (
    <section className="panel">
      <h3>图片识别文字</h3>
      <p>这段文字由文档内图片识别得到，可核对原图。</p>
      {typeof artifact === "string" && evidence.document_id && (
        <SourceImage
          projectId={projectId}
          kbId={kbId}
          documentId={evidence.document_id}
          artifactId={artifact}
        />
      )}
    </section>
  );
}

function SourceImage({
  projectId,
  kbId,
  documentId,
  artifactId,
}: {
  projectId: string;
  kbId: string;
  documentId: string;
  artifactId: string;
}) {
  const [visible, setVisible] = useState(false);
  const [failed, setFailed] = useState(false);
  const url = api.documentImageUrl(projectId, kbId, documentId, artifactId);
  return (
    <div>
      <button
        type="button"
        onClick={() => {
          setVisible(!visible);
          setFailed(false);
        }}
      >
        {visible ? "收起原图" : "查看原图"}
      </button>
      {visible &&
        (failed ? (
          <p role="alert">原图不可访问，可能已删除或权限发生变化。</p>
        ) : (
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="source-image"
          >
            <img
              src={url}
              alt="文档内原图，点击在新标签页放大查看"
              onError={() => setFailed(true)}
            />
            <span>点击原图在新标签页放大查看</span>
          </a>
        ))}
    </div>
  );
}
