# Notices: third parties and trademarks

Everything in this repository (code, documentation, logo and screenshots) is
licensed under the MIT License in `LICENSE`, © 2026 Ankur J.

## Third-party software (not bundled)

AiQuotaBar ships no third-party code. The installer, app and engine use these tools
already on your Mac or installed from their own sources:

| Tool | Used for | Licence | Bundled? |
|---|---|---|---|
| Swift and SwiftUI (Apple) | Builds and draws the menu bar app | Apple SDK | No (Xcode Command Line Tools) |
| Python 3 standard library | Everything the engine does | PSF | No (Xcode Command Line Tools) |

## Trademarks and affiliation

AiQuotaBar is an **unofficial**, independent project. It is **not affiliated
with, endorsed by, or sponsored by Anthropic, PBC**.

- "Claude", "Claude Code" and "Anthropic" are trademarks of Anthropic, PBC. They
  appear here only to describe what AiQuotaBar works with. AiQuotaBar uses no
  Anthropic logo or brand artwork.
- macOS is a trademark of Apple Inc.

## The usage data

AiQuotaBar reads the same usage endpoint that Claude Code's `/usage` command uses
(`api.anthropic.com/api/oauth/usage`), with the OAuth token Claude Code already
stored on your Mac. The endpoint is **undocumented** and can change or stop
working at any time. Your use of it is governed by your own agreement with
Anthropic ([Consumer Terms](https://www.anthropic.com/legal/consumer-terms) or
[Commercial Terms](https://www.anthropic.com/legal/commercial-terms)). Read
[Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
before you use it.
