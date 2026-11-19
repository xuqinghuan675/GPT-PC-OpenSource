# Security

## Reporting a vulnerability

Please do not publish exploitable details in a public issue. Use GitHub's private vulnerability reporting feature for this repository when available. Include affected versions, impact, reproduction steps, and a proposed mitigation if known.

## Deployment guidance

- Keep the MCP server on stdio or loopback by default.
- Do not expose the Streamable HTTP listener directly to the public internet.
- Use a dedicated Windows account for automation where practical.
- Keep OAuth client files, refresh tokens, API keys, browser profiles, and local auth keys outside Git.
- Review requested file paths and commands before granting an MCP host access to a machine.
- Install BrowserSkill and VirtualDesktopAccessor only from their upstream projects.
- Keep Python, Node, Rust, Windows, and third-party dependencies updated.

## Dependency audit

The frontend dependency tree was checked with `npm audit` on 2026-11-19. The audit reported **0 vulnerabilities** across 100 dependency entries. Re-run the audit when dependencies are updated.
