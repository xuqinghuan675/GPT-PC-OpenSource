import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.tsx";
import "./dsh/app.css";

const rootNode = document.querySelector<HTMLElement>("#app");
const selfTestNode = document.querySelector<HTMLElement>("#self-test");
if (!rootNode || !selfTestNode) throw new Error("App root is missing");

const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
if (prefersDark) document.body.dataset.dsDarkTheme = "";

createRoot(rootNode).render(createElement(App));
