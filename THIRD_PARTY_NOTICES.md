# Third-party notices

GPT-PC's own source code is licensed under the MIT License in LICENSE. Third-party components retain their own licenses.

## Code and presentation

- **DeepSeek Harness presentation components** — portions of the chatgpt-ui presentation shell are derived from deepseek-ai/deepseek-harness. The original MIT notice and attribution are preserved in chatgpt-ui/THIRD_PARTY_NOTICES.md. Upstream: https://github.com/deepseek-ai/deepseek-harness

## Optional external tools (not bundled)

- **BrowserSkill CLI** — optional external CLI, installed separately; upstream MIT License, Copyright (c) 2026 Tencent. No BrowserSkill executable or source is redistributed here. https://github.com/Tencent/BrowserSkill
- **VirtualDesktopAccessor** — optional Windows DLL; upstream MIT License, Copyright (c) 2015–2023 Jari Otto Oskari Pennanen. The DLL is not bundled. Obtain it from the upstream project and retain its license when redistributing it independently. https://github.com/ciantic/VirtualDesktopAccessor

## Direct dependencies

- Python: mcp (MIT), psutil (BSD-3-Clause), Pillow (PIL/Pillow license), OpenCV (Apache-2.0), Playwright Python (Apache-2.0), Google API Python Client and Google Auth libraries (Apache-2.0).
- Web: React (MIT), Vite (MIT), TypeScript (Apache-2.0), and Biome (MIT/Apache-2.0).
- Tauri/Rust dependencies include projects distributed under MIT and/or Apache-2.0; consult the package manifests and upstream license files for the exact version resolved by the lockfiles.

The dependency lockfiles identify the versions used for this source snapshot. Each dependency's own license applies; this file does not relicense third-party packages.
