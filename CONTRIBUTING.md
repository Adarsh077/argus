# Contributing

## Commit messages drive releases

Argus releases are fully automated by
[python-semantic-release](https://python-semantic-release.readthedocs.io/).
Every push to `main` is analyzed, and the version bump, changelog, git tag,
GitHub Release, and built artifacts (Arch package + Windows installer) are
produced from your **commit messages** — so they must follow
[Conventional Commits](https://www.conventionalcommits.org/).

Format: `type(optional-scope): summary`

| Type                                             | Release effect (while in `0.x`) |
| ------------------------------------------------ | ------------------------------- |
| `fix:`                                           | patch — `0.1.0` → `0.1.1`       |
| `feat:`                                          | minor — `0.1.0` → `0.2.0`       |
| `feat!:` / `fix!:` / `BREAKING CHANGE:` footer   | minor while `allow_zero_version` is set (bumps major once we cut 1.0) |
| `docs:` `ci:` `build:` `chore:` `refactor:` `test:` `style:` `perf:` | no release on their own |

Examples:

```
feat(tray): add "pause for 1 hour" menu item
fix(windows): dashboard failed to bind on the frozen build
feat!: drop support for X11 sessions
```

A push that contains only non-releasing types (e.g. a `docs:` fix) will run
the pipeline but cut no release — that's expected.

## Cutting 1.0

The version stays in `0.x` because `allow_zero_version = true` in
`pyproject.toml` (`[tool.semantic_release]`). When the API/UX is stable, set it
to `false` and land a `feat!:` (or a `BREAKING CHANGE:` footer) to release
`1.0.0`.

## Versioned files

`python-semantic-release` stamps the new version into `pyproject.toml`,
`packaging/arch/PKGBUILD`, and `packaging/arch/.SRCINFO` automatically. The
Windows installer (`packaging/windows/argus.iss`) receives its version from CI
via `ISCC /DMyAppVersion=...`. Don't bump versions by hand.
</content>
