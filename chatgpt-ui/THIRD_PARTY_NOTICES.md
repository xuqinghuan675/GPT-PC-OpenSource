# Third-Party Notices

## DeepSeek Harness

Portions of the user interface are copied from or adapted from DeepSeek Harness:

- Upstream: https://github.com/deepseek-ai/deepseek-harness
- Upstream branch studied: `master`
- Upstream commit studied: `0a53fb55bea101816fa226bb964ae2bed71c343b`
- Upstream release merge: `dsh-0.1.2-alpha.2`
- License: MIT

Copied or adapted source areas include:

- `packages/client/ui-theme/src/styles/{base,design-platform,gradient-shadow-text,scrollbar}.css`
- `packages/client/ui-primitives/src/{DiffBlock,TerminalBlock,ReadBlock,SearchBlock,DisclosureRow,FoldToggle,StateDot,Pill}.*`
- selected glyphs from `packages/client/ui-primitives/src/icons/index.tsx`
- `packages/client/ui-tool/src/client/tool/{components/ToolRow,toolviews/GenericToolCard}.*`
- `packages/client/ui-chat/src/client/chat/{ReasoningRow,MessageItem}.*`
- the presentation structure and composer geometry from `packages/client/ui-conversation/src/client/skeleton/{InputBar,ConversationRoot}.*`

This project does **not** import the DeepSeek Harness agent/runtime stack. Harness-specific Cordis services, session/workspace services, Lexical composer runtime, Shiki highlighting path, model services, tool runtime, attachments, commands, compaction, token meter, plugins, and server/runtime packages were intentionally omitted. The copied UI code is adapted to this project's local `AgentEvent -> SessionStore -> DshPresentationAdapter` seam.

MIT License

Copyright (c) 2026 DeepSeek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

DeepSeek Harness also ships its own `THIRD_PARTY_NOTICES.md`; dependencies actually installed in this project remain governed by their respective package licenses. This notice does not imply endorsement by or affiliation with DeepSeek.
