# Package Control submission

This is a maintainer checklist, not a statement that the package is published
or ready for approval. No release tag or channel pull request is created by
these preparation changes.

## Prepared in the repository

- `.python-version` selects Python 3.8. The proposed release selector is
  Sublime Text `>=4107` on `windows-x64`, matching the distributed native DLL.
- `.no-sublime-package` requests an unpacked install for the DLL, generated
  resources, and broker scripts.
- `LICENSE` covers GhostShell. `terminal/GHOSTTY_LICENSE` reproduces Ghostty's
  license, with the pinned source revision and binary fingerprint recorded in
  `terminal/GHOSTTY_VT_PROVENANCE.md`.
- `.gitattributes` excludes tests, internal notes, and local editor state from
  Git source archives without deleting them from developer checkouts. It does
  **not** exclude `tools/`, which contains runtime dependencies.
- The command palette and Preferences menu expose the standard split settings
  editor. The README explains installation, dependencies, privacy, and offline
  DLL provisioning.
- `usage_scan_enabled: false` disables startup, periodic, and manual usage
  scans and cancels subsequent provider fetches in an active sweep. A provider
  already running can finish, including saving rotated tokens. The current
  default remains enabled to preserve existing installations.
- [package-control-entry.json](package-control-entry.json) contains the proposed
  channel entry. It is a template for the channel repository, **not** a
  `package-metadata.json` to ship with the plugin.

## Remaining release blockers and decisions

- [ ] **Separate personal configuration from package defaults.**
  `ai_terminal.sublime-settings` still includes absolute developer-machine paths
  (Antigravity, Junie, local source builds and test agents), local STLOGS
  environment variables, an unrelated prompt-wrapper path, and development
  commentary. Move local overrides to `Packages/User` before replacing these
  with portable examples. Existing local settings were deliberately preserved
  during the packaging pass. Choose a default shell that works without a paid
  CLI account, rather than assuming Claude is installed.
- [ ] **Decide safe public defaults.** Current defaults enable transcript and
  input/output recording, detachable brokers, and automatic credential-backed
  usage requests. Make recording and provider-network activity explicitly
  opt-in for a public release, or obtain reviewer agreement on the disclosed
  behavior. Use `usage_scan_enabled: false` for a complete usage-scanner opt-out:
  `usage_refresh_minutes: 0` only disables the timer. Audit diagnostic
  logs and Windows ACLs too, not just the two recording flags.
- [ ] **Review the download/install lifecycle.** First parser construction
  obtains the DLL synchronously. Test slow/offline networks, a failed checksum,
  a read-only package folder, and updates while the DLL is loaded on Windows.
  Check whether the reviewer wants the binary bundled as a release asset
  instead of a first-use download. If changing its distribution, retain the
  fingerprint/provenance and audit licenses for any compiled dependencies.
- [ ] **Verify a clean first install in real Sublime Text.** Existing-checkout
  tests and a preexisting DLL can conceal missing release files and first-run
  errors. Use the procedure below, including the proposed minimum build.
- [ ] **Publish a semantic-version package tag after validation.** The existing
  `ghostty-vt-634957c8` tag distributes the native dependency. It is not a
  semantic-version GhostShell release. Choose an initial package version
  (for example `0.1.0`) and record release notes before tagging it. Keep the
  native dependency release intact.
- [ ] **Submit the channel entry and pass channel tests.** Follow the official
  instructions below. Nothing in this repository alone lists the package in
  Package Control.

## Local verification

From a Git checkout with the intended changes committed:

```console
python -m pytest tests/ -q
git diff --check
git archive --format=zip --output=GhostShell-review.zip HEAD
```

Write the review archive outside the checkout, or remove it after inspection.
Check that it includes `.python-version`, `.no-sublime-package`, all Sublime
resources, `terminal/*.py`, `terminal/GHOSTTY_LICENSE`,
`tools/agent_broker.py`, `tools/recover_console.py`, and
`tools/spawn_outside_job.ps1`. It must not include `.pyc`, credential files,
`package-metadata.json`, local workspaces, or test/internal-note directories.
The DLL is intentionally absent and needs the documented download path.

GitHub's tag archive is the actual delivery boundary. Download and inspect the
archive of the final public tag too, rather than assuming every hosting method
honors `export-ignore`. Check README image and documentation links in that
archive. Do not upload archives that include your User settings or local DLL
overrides.

## Clean-install smoke test

Use a disposable portable Sublime installation and test account or isolated
home directories. Do not link your normal `Packages/User`, CLI credentials, or
live sessions into it. Before loading GhostShell, create
`Packages/User/ai_terminal.sublime-settings` with `"usage_scan_enabled": false`
to prevent startup credential reads. Keep the isolated environment too, since
commands launched from a terminal may read their own credentials independently.

1. Install the release archive under `Packages/GhostShell`, not a working-tree
   symlink. Confirm that Sublime uses Python 3.8 and loads without errors.
2. Open **GhostShell: Settings** and its Preferences menu equivalent. Confirm
   the defaults are on the left and edits save to `User` on the right.
3. Use the README's Command Prompt profile with recording and detach disabled.
   Start with no DLL. Confirm download success and matching SHA-256, then launch
   `cmd.exe`, type `echo GhostShell`, and verify output, input, resize,
   selection/copy, scrollback, and close behavior.
4. Repeat a launch offline with the verified DLL present. Separately verify a
   missing DLL offline produces a useful error, not a silent broken tab.
5. If detach is included in the release, install the external Python runtime
   and test task creation/cleanup, detach/reconnect, Sublime restart, and
   explicit session termination. Inspect the registry and launch-file cleanup.
6. Verify update/reload and uninstall behavior with no live user sessions.
   User settings must survive an update. Document persistent log/registry data
   and ensure no abandoned test broker or scheduled task remains.
7. Repeat against the minimum supported Sublime build, or raise
   `sublime_text` in the template and README to the oldest build actually
   supported. Python syntax checks alone do not validate the Sublime API.

## Channel submission

1. Review [similar terminal packages](https://packagecontrol.io/search/terminal)
   and explain GhostShell's distinct use case: libghostty-vt-backed terminal
   rendering and independent Windows broker sessions, aimed at AI CLIs.
   Recheck [name availability](https://packagecontrol.io/search/GhostShell).
2. Fork and clone
   [wbond/package_control_channel](https://github.com/wbond/package_control_channel).
3. Add the object in `docs/package-control-entry.json` to `repository/g.json`
   in alphabetical order, using its surrounding formatting. `tags: true`
   intentionally selects semantic-version releases, not the native-library tag.
4. Install **ChannelRepositoryTools** and run
   **ChannelRepositoryTools: Test Default Channel** on the channel checkout.
   Also run any checks required by the channel repository's current
   contribution instructions.
5. Open a pull request explaining the platform restriction, native dependency
   and verification, external Python requirement for detach, and relevant
   privacy/background behavior. Wait for review before changing the README
   to claim Package Control availability.

References:

- [Official submission guide](https://packagecontrol.io/docs/submitting_a_package)
- [Repository schema examples](https://github.com/wbond/package_control/blob/master/example-repository.json)
