# Repository Instructions

These instructions apply to all files in this repository.
They also apply to `graphify-codebase/` and all future subdirectories.

## Safety

- Do not delete a file.
- Move an obsolete file to a recoverable archive.
- Do not commit secrets, user memory, indexes, logs, or generated environments.
- Do not commit organization names, private domains, ticket prefixes, or user-specific paths.
- Put machine-specific and organization-specific configuration in the ignored `.env`.
- Use neutral names and identifiers in documentation, tests, and benchmark fixtures.
- Keep Markdown as the authority for distilled durable memory.
- Keep the artifact SQLite database as the authority for raw artifacts.
- Treat Markdown search SQLite, artifact burst indexes, and Graphify data as derived data.

## Public repository privacy

This repository is public.

- Commit only information that is necessary for this public project.
- Never commit information from a person, another project, or an organization.
- Never commit real memory records, ticket identifiers, customer data, private URLs, machine names, account names, or email addresses.
- Never commit user-specific paths, credentials, tokens, or private keys.
- Use neutral synthetic values in documentation, tests, and fixtures.
- Keep all local configuration in the ignored `.env` file.
- Apply these rules to files, paths, branches, tags, commit messages, commit metadata, and all reachable Git history.
- Apply these rules to every skill, script, example, fixture, and generated stub.
- Run the privacy tests and inspect the staged diff before each commit.
- Inspect the complete reachable Git history before each public push.
- Stop the push if the privacy status is uncertain.

## Documentation language

Write all new or changed technical documentation in ASD-STE100 Simplified Technical English.
Use Issue 9, dated January 2025.

Apply this requirement to these items:

- `README.md`
- Files in `docs/`
- Setup instructions
- Operational instructions
- User-facing command help
- User-facing error text

Use these rules:

- Use active voice.
- Use one topic in each sentence.
- Use one topic in each paragraph.
- Use a maximum of 20 words in each procedure sentence.
- Use a maximum of 25 words in each descriptive sentence.
- Use a maximum of six sentences in each paragraph.
- Use the same term for the same item.
- Do not use contractions.
- Put a required condition before its instruction.
- Use the imperative form for procedure steps.
- Do not use the imperative form in descriptive text.
- Keep exact commands, paths, identifiers, and API names unchanged.
- Treat project terms as technical nouns or technical verbs when necessary.

Read [docs/writing-standard.md](docs/writing-standard.md) before you change technical documentation.

## Source organization

- Keep user documentation in `docs/`.
- Keep the root `README.md` short.
- Link the root `README.md` to the applicable guide.
- Keep private historical documents in the ignored `docs/archive/` directory.
- Keep the canonical skill in `skill/ai-memory/`.
- Keep the canonical Graphify skill in `graphify-codebase/skill/graphify/`.
- Keep harness-local skill files as discovery stubs.
- Keep Graphify Codebase independent from AI Memory.
- Do not add memory storage, recall, feedback, or learning behavior to the Graphify skill.

## Verification

- Test each command that you add to the documentation.
- Check all changed Markdown links.
- Run the applicable tests before you finish.
- Review changed documentation against the STE checklist.
