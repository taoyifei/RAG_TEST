import { useEffect, useState } from "react";

import AppShell from "./app/AppShell";
import { applicationPath, withAppBase } from "./app/basePath";
import { detectProductMode, type ProductMode } from "./app/product-mode";
import { isWanshitongAdminPath } from "./app/router";
import { ErrorPanel } from "./components/ui";
import { WanshitongApp } from "./public/WanshitongApp";
import { ConsoleProvider } from "./state/console-context";

type ModeState =
  | { state: "loading" }
  | { state: "ready"; mode: ProductMode }
  | { state: "error"; error: unknown };

export default function App() {
  const [location, setLocation] = useState(() => applicationPath());
  const [modeState, setModeState] = useState<ModeState>({ state: "loading" });
  const [modeRefresh, setModeRefresh] = useState(0);
  useEffect(() => {
    const handleLocation = () => setLocation(applicationPath());
    window.addEventListener("popstate", handleLocation);
    return () => window.removeEventListener("popstate", handleLocation);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void detectProductMode(controller.signal)
      .then((mode) => setModeState({ state: "ready", mode }))
      .catch((error: unknown) => setModeState({ state: "error", error }));
    return () => controller.abort();
  }, [modeRefresh]);
  useEffect(() => {
    if (modeState.state !== "ready") return;
    document.title =
      modeState.mode === "wanshitong" ? "湾事通" : "Universal RAG 控制台";
  }, [modeState]);

  if (modeState.state === "loading") {
    return (
      <main className="mode-screen" id="main-content">
        <p role="status">正在确认产品模式…</p>
      </main>
    );
  }
  if (modeState.state === "error") {
    return (
      <main className="mode-screen" id="main-content">
        <ErrorPanel error={modeState.error} />
        <button
          className="primary"
          onClick={() => {
            setModeState({ state: "loading" });
            setModeRefresh((value) => value + 1);
          }}
        >
          重新连接
        </button>
      </main>
    );
  }
  if (modeState.mode === "wanshitong" && !isWanshitongAdminPath(location)) {
    return <WanshitongApp />;
  }
  if (modeState.mode === "wanshitong") {
    return (
      <main className="mode-screen" id="main-content">
        <a href={withAppBase("/admin/overview")}>进入湾事通管理后台</a>
      </main>
    );
  }
  return (
    <ConsoleProvider productMode="universal">
      <AppShell />
    </ConsoleProvider>
  );
}
