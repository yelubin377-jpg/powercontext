# Distributable Skill packages

This directory holds the Skills that this project publishes to the community, one
directory per Skill. Every package is self-contained: `SKILL.md` at the root, the
reference documents it links to, and a README for the public page.

## Layout

```
skill-packages/
├── README.md                        # this runbook
└── powercontext-project-context/    # published as @<owner>/powercontext-project-context
    ├── SKILL.md                     # front matter + routing rules
    ├── README.md                    # public page: requirements, install, design notes
    └── references/                  # the detail documents SKILL.md links to
```

The publisher lives next to the other repository scripts in `scripts/publish_modelscope_skill.py`.

The `powercontext-project-context` package is the distributable copy of the Skill that the
Pi, Claude Code, Codex, and other host integrations ship under
`integrations/<host>/plugins/<host>/skills/powercontext-project-context/`. Keep the two in
sync when the routing rules change.

## Competition flow

The OceanBase 训练营 sub-question **上传并分享自己的 Skill** (question `600075`, sub-question `800512`,
verifier `MODELSCOPE_SKILL`) is auto-verified. Its work link is the only required submission field.

1. **Get the team verification line.** Open the submission dialog (提测弹窗) of that sub-question and copy
   the verification line of the team. The dialog serves it without a work link. The same value comes from
   `POST https://openwebapi.oceanbase.com/api/techcamp/testplatform/taskrecord/modelscope/verification`
   with `{"questionId": 600075}`, using the signed-in session of a registered team member.
2. **Append the line to the end of `SKILL.md`** and publish the public Skill, as described below.
3. **Submit the work link** in the submission dialog, in the form
   `https://modelscope.cn/skills/@<owner>/<name>`, before the verification line expires.
4. **Check the result** in the submission records. One Skill can only be claimed by one team, so publish the
   team's own Skill and refresh the published Skill instead of creating a second copy.

Steps 1 and 3 need a signed-in session of a registered team member; this repository only owns step 2.

## Publishing to ModelScope

`scripts/publish_modelscope_skill.py` validates the package, appends the team
verification line to the end of `SKILL.md`, packs the archive ModelScope expects
(`SKILL.md` at the archive root), and publishes it as a public Skill.

```bash
pip install modelscope

# 1. check the package and the archive without touching the network
python scripts/publish_modelscope_skill.py \
  --owner <modelscope-owner> \
  --name powercontext-project-context \
  --verification-line "<team verification line>" \
  --dry-run

# 2. publish (or update, when the Skill already exists)
export MODELSCOPE_API_TOKEN=<token from https://modelscope.cn/my/myaccesstoken>
python scripts/publish_modelscope_skill.py \
  --owner <modelscope-owner> \
  --name powercontext-project-context \
  --verification-line "<team verification line>" \
  --category developer-tools \
  --visibility public \
  --license Apache-2.0
```

The script prints the public work link:

```
https://modelscope.cn/skills/@<modelscope-owner>/powercontext-project-context
```

Pass that link to the competition submission form. The script never submits anything
itself, and it never rewrites an already published Skill without `--verification-line`
being explicit about the change.

### Notes

- The verification line belongs at the very end of the `SKILL.md` body, verbatim and
  complete. The script appends it once and keeps it idempotent, so re-publishing after a
  repair does not duplicate the line.
- ModelScope accepts a Skill archive of at most 5 MiB with exactly one `SKILL.md` at the
  root. The script enforces both rules before it uploads anything.
- A Skill can only be claimed by one team. Publish the team's own Skill, and update the
  published Skill rather than creating a second copy.
- `--repo-type skill` skills are created through `POST /skills`; an existing Skill is
  refreshed through `PATCH /skills/{owner}/{name}/settings`. The script tries the
  creation first and falls back to the update.
