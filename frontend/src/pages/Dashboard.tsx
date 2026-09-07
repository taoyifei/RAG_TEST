import { Upload } from "lucide-react";
import { useConsole } from "../state/console-context";

export function Dashboard({ go }: { go: (path: string) => void }) {
  const { scope } = useConsole();
  return (
    <>
      <section className="dashboard-actions">
        <div>
          <h2>当前工作范围</h2>
          <p>选择项目和知识库后即可管理文档与检索。</p>
        </div>
        <button className="primary" onClick={() => go("/documents")}>
          <Upload aria-hidden="true" size={18} />
          上传文档
        </button>
      </section>
      <div className="metric-grid">
        <article>
          <span>项目</span>
          <strong>{scope.projectId ? "已选择" : "待选择"}</strong>
          <code>{scope.projectId || "—"}</code>
        </article>
        <article>
          <span>知识库</span>
          <strong>{scope.kbId ? "已选择" : "待选择"}</strong>
          <code>{scope.kbId || "—"}</code>
        </article>
        <article>
          <span>当前索引版本</span>
          <strong>{scope.revisionId ? "已绑定" : "待构建"}</strong>
          <code>{scope.revisionId || "—"}</code>
        </article>
      </div>
    </>
  );
}
