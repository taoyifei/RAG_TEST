import { useEffect, useState } from "react";

import AppShell from "./app/AppShell";
import { WanshitongApp } from "./public/WanshitongApp";
import { ConsoleProvider } from "./state/console-context";

function isAdminPath(pathname: string): boolean {
  return pathname === "/admin" || pathname.startsWith("/admin/");
}

export default function App() {
  const [admin, setAdmin] = useState(() =>
    isAdminPath(window.location.pathname),
  );
  useEffect(() => {
    const handleLocation = () =>
      setAdmin(isAdminPath(window.location.pathname));
    window.addEventListener("popstate", handleLocation);
    return () => window.removeEventListener("popstate", handleLocation);
  }, []);

  if (!admin) {
    return <WanshitongApp />;
  }
  return (
    <ConsoleProvider>
      <AppShell />
    </ConsoleProvider>
  );
}
