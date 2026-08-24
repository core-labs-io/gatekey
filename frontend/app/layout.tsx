import type { ReactNode } from "react";
import { ToastProvider } from "@/components/ui";
import "./globals.css";

export const metadata = {
  title: "Gatekey Admin",
  description: "Gatekey - self-hosted AI gateway admin console.",
};

// Runs before first paint so the page never flashes the wrong theme.
// Stored preference: "light" | "dark"; anything else (or nothing) means
// "system", which stamps no attribute and lets prefers-color-scheme decide
// (globals.css defines the token sets for all three states).
const THEME_BOOTSTRAP = `
(function () {
  try {
    var t = localStorage.getItem("gatekey-theme");
    if (t === "light" || t === "dark") {
      document.documentElement.dataset.theme = t;
    }
  } catch (e) {}
})();
`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body>
        <ToastProvider>{children}</ToastProvider>
      </body>
    </html>
  );
}
