import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";
import { applyTheme, resolveInitialTheme } from "@/lib/theme";

// 尽早应用主题，避免首屏出现错误配色的闪烁。
applyTheme(resolveInitialTheme());

createRoot(document.getElementById("root")!).render(<App />);
