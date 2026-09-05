import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { ConsoleProvider } from "./state/console-context";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConsoleProvider>
      <App />
    </ConsoleProvider>
  </StrictMode>,
);
