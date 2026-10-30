# Accessibility

We want Code Graph RAG to work for people who use a screen reader, a screen magnifier, a braille display, voice control, high contrast or custom colours, or only a keyboard. This page says what works today, what does not work well yet, and how to tell us when something gets in your way.

## What this covers

- The `cgr` command line, including the interactive session that `cgr start` opens.
- The MCP server that `cgr mcp-server` runs.
- The documentation site at [docs.code-graph-rag.com](https://docs.code-graph-rag.com) and the Markdown files in this repository.

## Goals

For the documentation site we aim for WCAG 2.2 Level AA where feasible. That is a goal and not a claim of conformance, because nobody has audited the site against it.

WCAG was written for web content and does not map cleanly onto a terminal. For `cgr` we aim to follow the conventions terminal users already rely on:

- Colour never carries meaning on its own.
- Colour and animation can be turned off.
- Output written to a file or a pipe is plain text.
- Tabular results have a machine-readable form.
- Everything works from the keyboard.

## What works today

### In the terminal

- `NO_COLOR=1` turns colour off. Output written to a file or a pipe has no colour or escape sequences, and cgr does not hard-wrap plain messages there, so each message stays on one line.
- `TERM=dumb` also turns off animation. The interactive session then draws no spinner or status bar, and its input prompt falls back to plain line editing with the same key bindings.
- `cgr doctor` marks each check with ✓ or ✗ as well as colouring it. When the terminal's encoding cannot show those characters it prints `PASS` or `FAIL` instead.
- `cgr --quiet <command>` hides progress output, banners and informational logs.
- `cgr start --ask-agent "<question>"` asks one question, writes the answer to standard output and exits, so you can get an answer without the interactive session.
- `cgr dead-code --format json`, `cgr duplicates --format json` and `cgr start --ask-agent "<question>" --output-format json` print JSON in place of a table.
- The interactive session needs only the keyboard. Shift+Tab switches the permission mode and Ctrl+C ends the session.

### Through the MCP server

The MCP server has no interface of its own. Its tools return text and structured data to your AI client, such as Claude Code or Cursor, and the client decides how to show them. How accessible that is depends on the client.

### On the documentation site

- The site uses Material for MkDocs, which gives it a skip-to-content link, search and navigation you can use from the keyboard, and a toggle between light and dark colour schemes.
- Every terminal recording has alt text that says what it shows. Most also have the command they run next to them as text.

## Supported environments

- cgr runs in terminals on macOS, Linux and Windows, and CI tests it on all three.
- The documentation site supports current versions of the major desktop and mobile browsers.
- We have not yet tested cgr or the site with assistive technology in any systematic way. Reports from people who use VoiceOver, NVDA, JAWS, Orca, TalkBack, a braille display or a screen magnifier are especially welcome.

## Known limitations

**The interactive session redraws its status line.** While the agent works, `cgr start` shows a spinner and a status bar and redraws them several times a second. A screen reader may announce every redraw, or none of them. Set `TERM=dumb` to stop the redraws, or use `cgr start --ask-agent "<question>"` for single questions.

**Tables and panels use box-drawing characters.** Some screen readers read these out one character at a time. Where a command offers JSON output, listed above, use that. Other commands, `cgr doctor` among them, only print tables and panels for now.

**Recordings in the documentation play on their own and cannot be paused.** Nothing transcribes the output they show. Where the command a recording runs is next to it, running that command shows the same output in your own terminal.

**Nobody has checked the documentation site's colours against WCAG contrast ratios.** If any text is hard to read, please report it.

## Reporting a barrier

Open an issue with the [accessibility barrier form](https://github.com/vitali87/code-graph-rag/issues/new?template=accessibility.yml). It asks what you were trying to do, what got in the way and where. It also has optional fields for your environment and for any assistive technology or settings you use. You do not need to tell us anything about yourself or any disability.

If the form itself is hard to use, any other issue form will do. Say in it that the problem is about accessibility.

## What happens next

Accessibility reports get the `accessibility` label and go through triage with the other bugs. We cannot promise that a report will be fixed, or fixed by a particular date. Where a fix is feasible, we will make a best effort to provide one, and to point you to a workaround in the meantime if there is one. Where it is not feasible, the barrier may stay. Pull requests that remove a barrier are welcome.

## For contributors

When a change affects what people see or hear:

- Never let colour carry meaning alone. Pair it with a word or a symbol, the way `cgr doctor` does through `status_mark` in `codebase_rag/console_marks.py`.
- Print through the project's console in `codebase_rag/utils/terminal_console.py`. It honours `NO_COLOR`, `TERM=dumb` and redirection. A `Console` that forces terminal output does not.
- Give new tabular output a JSON form.
- Give every image and recording in the documentation alt text that says what it shows, and put the command it runs next to it as text.
- Keep headings in order, and write link text that makes sense out of context.

## Ownership

The lead maintainer named in [GOVERNANCE.md](GOVERNANCE.md) maintains this page.

Last reviewed: October 2026.
